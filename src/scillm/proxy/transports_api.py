"""Normalized model-turn / provider-session transport API for Tau (issue #28).

Contracts:
- ``scillm.transport_request.v1``  — create a transport session
- ``scillm.transport_handle.v1``   — returned handle + correlation
- ``scillm.transport_event.v1``    — typed event stream entries
- ``scillm.transport_result.v1``   — terminal (per-turn) provider result

Default mode is one Tau-controlled model turn on a resumable provider
session: Tau sends messages + tool schemas, SciLLM carries the request and
returns assistant output and/or tool-call requests, Tau executes any tool
effect and posts the tool result as the next turn. SciLLM never executes
tools and never continues an autonomous agent loop in this mode.

A transport terminal state means only that the provider request/session turn
ended; it never implies Tau node/artifact/DAG completion.

``opaque_agent_compat`` profiles (OpenCode serve etc.) are not re-wrapped
here: creating a normalized transport against one returns a typed
``fork_required`` response pointing at the native surface, so the compat
transport keeps its native run/session/event references and reduced
capabilities stay visible instead of being laundered through this API.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, Field

from scillm.proxy.errors import ProxyError
from scillm.proxy.transport_profiles import (
    TRANSPORT_CAPABILITIES,
    TransportProfile,
    get_registry,
)

REQUEST_SCHEMA = "scillm.transport_request.v1"
HANDLE_SCHEMA = "scillm.transport_handle.v1"
EVENT_SCHEMA = "scillm.transport_event.v1"
RESULT_SCHEMA = "scillm.transport_result.v1"

# Terminal states describe the provider turn/session only — not Tau semantics.
TERMINAL_STATES = {"turn_completed", "awaiting_tool_result", "cancelled", "failed"}


class TransportCorrelation(BaseModel):
    tau_run_id: str | None = None
    node_id: str | None = None
    attempt: int | None = None
    goal_hash: str | None = None


class TransportLimits(BaseModel):
    timeout_sec: int = 300
    max_tokens: int | None = None


class TransportCreateRequest(BaseModel):
    schema_version: str = Field(default=REQUEST_SCHEMA, alias="schema")
    profile: str
    correlation: TransportCorrelation = Field(default_factory=TransportCorrelation)
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    required_capabilities: list[str] = Field(default_factory=list)
    limits: TransportLimits = Field(default_factory=TransportLimits)
    stream: bool = False

    model_config = {"populate_by_name": True}


class TransportTurnRequest(BaseModel):
    """Next Tau-supplied turn: tool results and/or additional messages."""

    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    messages: list[dict[str, Any]] = Field(default_factory=list)


class TransportRecord:
    def __init__(self, req: TransportCreateRequest, profile: TransportProfile):
        self.transport_id = f"tr_{uuid.uuid4().hex[:16]}"
        self.created_at = time.time()
        self.request = req
        self.profile = profile
        self.state = "created"
        self.state_reason: str | None = None
        self.turn = 0
        self.messages: list[dict[str, Any]] = list(req.messages)
        self.events: list[dict[str, Any]] = []
        self.result: dict[str, Any] | None = None
        self.upstream: dict[str, Any] = {}
        self.task: asyncio.Task | None = None
        self.event_signal = asyncio.Event()

    def emit(self, event_type: str, data: dict[str, Any]) -> None:
        self.events.append({
            "schema": EVENT_SCHEMA,
            "seq": len(self.events),
            "transport_id": self.transport_id,
            "turn": self.turn,
            "type": event_type,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "data": data,
        })
        self.event_signal.set()
        self.event_signal = asyncio.Event()

    def handle(self) -> dict[str, Any]:
        return {
            "schema": HANDLE_SCHEMA,
            "transport_id": self.transport_id,
            "profile": self.profile.id,
            "mode": self.profile.mode,
            "provider": self.profile.provider,
            "model": self.profile.model,
            "state": self.state,
            "state_reason": self.state_reason,
            "turn": self.turn,
            "correlation": self.request.correlation.model_dump(),
            "upstream": self.upstream,
            "note": (
                "terminal state describes the provider turn only; it does not "
                "imply Tau node, artifact, test, review, or DAG completion"
            ),
        }


_TRANSPORTS: dict[str, TransportRecord] = {}

# Carrier signature: (record) -> chat-completions-style response dict.
Carrier = Callable[[TransportRecord], Awaitable[dict[str, Any]]]


async def _default_carrier(record: TransportRecord) -> dict[str, Any]:
    """Carry one provider turn through the existing chat surface, in-process."""
    import httpx

    from scillm.proxy import app as app_module

    master_key = app_module._config.general.master_key if app_module._config else ""
    body: dict[str, Any] = {
        "model": record.profile.model,
        "messages": record.messages,
        "scillm_metadata": {
            "transport_id": record.transport_id,
            **{k: v for k, v in record.request.correlation.model_dump().items() if v is not None},
        },
    }
    if record.request.tools:
        body["tools"] = record.request.tools
    if record.profile.reasoning_effort:
        body["reasoning_effort"] = record.profile.reasoning_effort
    transport = httpx.ASGITransport(app=app_module.app)
    timeout = record.request.limits.timeout_sec
    async with httpx.AsyncClient(transport=transport, base_url="http://scillm.local", timeout=timeout) as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {master_key}",
                "X-Caller-Skill": "scillm-transports",
            },
            json=body,
        )
    if resp.status_code != 200:
        raise ProxyError(
            502,
            f"provider turn failed with HTTP {resp.status_code}",
            "transport_provider_error",
            details={"body": resp.text[:500]},
        )
    return resp.json()


def _summarize_choice(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Classify the provider turn honestly.

    Reasoning-only output, empty text, or pending tool calls must never be
    normalized into a successful final answer.
    """
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    tool_calls = message.get("tool_calls") or []
    content = message.get("content") or ""
    reasoning = message.get("reasoning_content") or ""
    if tool_calls:
        return "awaiting_tool_result", {"tool_calls": tool_calls, "content": content}
    if content.strip():
        return "turn_completed", {"content": content}
    if reasoning.strip():
        return "failed", {
            "reason": "reasoning_only_output",
            "detail": "provider returned reasoning with no visible assistant text",
        }
    return "failed", {"reason": "empty_terminal_text", "detail": "provider returned no visible output"}


async def _run_turn(record: TransportRecord, carrier: Carrier) -> None:
    record.state = "in_flight"
    record.emit("turn_started", {"turn": record.turn, "message_count": len(record.messages)})
    try:
        payload = await carrier(record)
    except asyncio.CancelledError:
        record.state = "cancelled"
        record.state_reason = "cancelled by caller"
        record.emit("cancelled", {"turn": record.turn})
        record.result = _result(record, ok=False)
        raise
    except ProxyError as exc:
        record.state = "failed"
        record.state_reason = exc.message
        record.emit("provider_error", {"type": exc.error_type, "message": exc.message, "details": exc.details})
        record.result = _result(record, ok=False)
        return
    except Exception as exc:  # noqa: BLE001
        record.state = "failed"
        record.state_reason = str(exc)
        record.emit("provider_error", {"type": "transport_internal_error", "message": str(exc)})
        record.result = _result(record, ok=False)
        return

    record.upstream = {
        "id": payload.get("id"),
        "model": payload.get("model"),
        "provider_profile": record.profile.id,
    }
    if payload.get("usage"):
        record.emit("usage", payload["usage"])
    state, data = _summarize_choice(payload)
    record.state = state
    if state == "awaiting_tool_result":
        # Preserve the assistant tool-call message so the next turn is a
        # valid continuation once Tau posts the tool results.
        record.messages.append((payload.get("choices") or [{}])[0].get("message") or {})
        record.emit("tool_call_request", data)
    elif state == "turn_completed":
        record.messages.append({"role": "assistant", "content": data["content"]})
        record.emit("assistant_message", data)
    else:
        record.state_reason = data.get("reason")
        record.emit("provider_error", data)
    record.result = _result(record, ok=state in ("turn_completed", "awaiting_tool_result"), payload=payload)


def _result(record: TransportRecord, ok: bool, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema": RESULT_SCHEMA,
        "transport_id": record.transport_id,
        "ok": ok,
        "state": record.state,
        "state_reason": record.state_reason,
        "turn": record.turn,
        "profile": record.profile.id,
        "provider": record.profile.provider,
        "model": record.profile.model,
        "auth_source": record.profile.auth_source,
        "upstream": record.upstream,
        "usage": (payload or {}).get("usage"),
        "correlation": record.request.correlation.model_dump(),
        "tau_completion": None,
        "note": "transport result only; Tau owns tools, evidence, and completion",
    }


def _get_record(transport_id: str) -> TransportRecord:
    record = _TRANSPORTS.get(transport_id)
    if record is None:
        raise ProxyError(404, f"unknown transport {transport_id!r}", "unknown_transport")
    return record


AuthCheck = Callable[[Request], str | None]


def create_transports_router(check_auth: AuthCheck, carrier: Carrier | None = None) -> APIRouter:
    router = APIRouter()
    carry = carrier or _default_carrier

    def auth(request: Request) -> None:
        err = check_auth(request)
        if err:
            raise ProxyError(401, err, "authentication_error")

    @router.post("/transports")
    async def create_transport(spec: TransportCreateRequest, request: Request) -> JSONResponse:
        auth(request)
        if spec.schema_version != REQUEST_SCHEMA:
            raise ProxyError(
                422,
                f"unsupported request schema {spec.schema_version!r}",
                "unsupported_transport_schema",
                details={"expected": REQUEST_SCHEMA},
            )
        registry = get_registry()
        profile = registry.resolve(spec.profile, spec.required_capabilities)
        if profile.mode == "opaque_agent_compat":
            return JSONResponse(
                status_code=409,
                content={
                    "schema": HANDLE_SCHEMA,
                    "state": "fork_required",
                    "error": {
                        "type": "fork_required",
                        "message": (
                            f"profile {profile.id!r} is an opaque agent compatibility "
                            "transport; it cannot be driven as a Tau-native model turn. "
                            "Use its native surface, which preserves the native "
                            "run/session/event references."
                        ),
                        "native_surface": "/v1/scillm/opencode",
                        "reduced_capabilities": profile.capabilities,
                    },
                },
            )
        if spec.tools and "tool_calling" not in profile.capabilities:
            raise ProxyError(
                422,
                f"profile {profile.id!r} does not advertise tool_calling",
                "transport_capability_unsatisfied",
                details={"capabilities": profile.capabilities},
            )
        record = TransportRecord(spec, profile)
        _TRANSPORTS[record.transport_id] = record
        record.task = asyncio.create_task(_run_turn(record, carry))
        return JSONResponse(status_code=201, content=record.handle())

    @router.get("/transports/{transport_id}")
    async def get_transport(transport_id: str, request: Request) -> JSONResponse:
        auth(request)
        return JSONResponse(_get_record(transport_id).handle())

    @router.get("/transports/{transport_id}/events")
    async def get_events(transport_id: str, request: Request, since: int = 0) -> JSONResponse:
        auth(request)
        record = _get_record(transport_id)
        return JSONResponse({
            "schema": EVENT_SCHEMA,
            "transport_id": transport_id,
            "state": record.state,
            "events": record.events[since:],
        })

    @router.post("/transports/{transport_id}/turns")
    async def next_turn(transport_id: str, turn: TransportTurnRequest, request: Request) -> JSONResponse:
        auth(request)
        record = _get_record(transport_id)
        if record.state == "in_flight":
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "type": "queued_for_next_turn_unsupported",
                        "message": "a provider turn is in flight; wait for its terminal state",
                        "state": record.state,
                    }
                },
            )
        if record.state not in ("awaiting_tool_result", "turn_completed"):
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "type": "unsupported",
                        "message": f"transport in state {record.state!r} cannot accept a next turn",
                        "state": record.state,
                    }
                },
            )
        if record.state == "awaiting_tool_result" and not turn.tool_results:
            raise ProxyError(
                422,
                "transport awaits tool results; supply tool_results with tool_call_id",
                "tool_results_required",
            )
        if record.turn + 1 >= record.profile.limits.max_turns_per_session:
            raise ProxyError(429, "max turns per session reached", "transport_turn_limit")
        for tr in turn.tool_results:
            if not tr.get("tool_call_id"):
                raise ProxyError(422, "each tool result needs tool_call_id", "tool_results_required")
            record.messages.append({
                "role": "tool",
                "tool_call_id": tr["tool_call_id"],
                "content": tr.get("content", ""),
            })
        record.messages.extend(turn.messages)
        record.turn += 1
        record.result = None  # a fresh provider turn owns the next result
        record.state = "in_flight"
        record.task = asyncio.create_task(_run_turn(record, carry))
        return JSONResponse(status_code=202, content=record.handle())

    @router.post("/transports/{transport_id}/cancel")
    async def cancel_transport(transport_id: str, request: Request) -> JSONResponse:
        auth(request)
        record = _get_record(transport_id)
        if record.task and not record.task.done():
            record.task.cancel()
            try:
                await record.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            return JSONResponse(record.handle())
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "type": "unsupported",
                    "message": f"no in-flight provider turn to cancel (state {record.state!r})",
                    "state": record.state,
                }
            },
        )

    @router.get("/transports/{transport_id}/result")
    async def get_result(transport_id: str, request: Request, wait_sec: float = 0) -> JSONResponse:
        auth(request)
        record = _get_record(transport_id)
        deadline = time.monotonic() + max(0.0, min(wait_sec, 600.0))
        while record.result is None and record.task and not record.task.done() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if record.result is None:
            return JSONResponse(
                status_code=202,
                content={"schema": RESULT_SCHEMA, "transport_id": transport_id, "state": record.state, "ok": None},
            )
        return JSONResponse(record.result)

    logger.info("normalized transport routes registered under /v1/scillm/transports")
    return router
