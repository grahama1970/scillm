"""Stream OpenCode bus events for transport message runs (reasoning, tools, permissions)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from scillm.proxy.errors import ProxyError
from scillm.proxy.opencode_serve import (
    OpenCodeServeClient,
    extract_assistant_text,
    session_is_busy,
    text_parts,
)
from scillm.proxy.opencode_transport import (
    DELIVERY_ACTED,
    DELIVERY_ABORTED,
    DELIVERY_BLOCKED,
    DELIVERY_COMPLETED,
    DELIVERY_DELIVERED,
    DELIVERY_FAILED,
    DELIVERY_IDLE_SEEN,
    DELIVERY_POSTED,
    DELIVERY_QUEUED,
    DELIVERY_SUPERSEDED,
    DELIVERY_TIMED_OUT,
    DELIVERY_WAITING_PERMISSION,
    ChildAttempt,
    TransportState,
    assistant_text_blocker,
    enrich_event,
    git_diff_empty,
    is_opencode_message_aborted_error,
    message_id_from_payload,
    opencode_message_error,
)
from scillm.proxy.opencode_transport_events import (
    merge_reasoning_excerpt,
    normalize_opencode_bus_event,
    parse_sse_json_payload,
)

OnBusEvent = Callable[[dict[str, Any], dict[str, Any]], Awaitable[None]]


async def parse_opencode_sse_stream(
    client: OpenCodeServeClient,
    *,
    watch_session_ids: frozenset[str] | set[str],
    on_event: OnBusEvent,
) -> None:
    buffer = ""
    async for chunk in client.iter_event_stream():
        if isinstance(chunk, bytes):
            buffer += chunk.decode("utf-8", errors="replace")
        else:
            buffer += str(chunk)
        while "\n" in buffer:
            line, _, buffer = buffer.partition("\n")
            line = line.strip()
            if not line.startswith("data:"):
                continue
            bus = parse_sse_json_payload(line[5:].strip())
            if bus is None:
                continue
            norm = normalize_opencode_bus_event(bus, watch_session_ids=watch_session_ids)
            if norm is not None:
                await on_event(norm, bus)


def transport_event_sse(row: dict[str, Any]) -> str:
    return f"data: {json.dumps(row, ensure_ascii=False)}\n\n"


class TransportMessageStream:
    """Relay OpenCode bus events while posting a sync transport message."""

    def __init__(
        self,
        transport: Any,
        client: OpenCodeServeClient,
        state: TransportState,
        child: ChildAttempt,
        *,
        prompt: str,
        system: str | None,
        model: str | None,
        timeout_s: float,
        heartbeat_s: float,
        wait_idle: bool,
    ) -> None:
        self.transport = transport
        self.client = client
        self.state = state
        self.child = child
        self.prompt = prompt
        self.system = system
        self.model = model
        self.deadline = time.monotonic() + timeout_s
        self.heartbeat_s = heartbeat_s
        self.wait_idle = wait_idle
        self.watch_session_ids = frozenset(
            sid for sid in (state.parent_session_id, child.child_session_id) if sid
        )
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.send_error: BaseException | None = None
        self.message_payload: dict[str, Any] | None = None
        self.message_id = ""
        self.assistant_text = ""
        self.reasoning_excerpt = ""
        self.idle_seen = False
        self.seen_permission_ids: set[str] = set()
        self._send_task: asyncio.Task[None] | None = None
        self._relay_task: asyncio.Task[None] | None = None

    def _enrich(self, event: dict[str, Any]) -> dict[str, Any]:
        return enrich_event(
            event,
            transport_run_id=self.state.transport_run_id,
            dag_node_id=self.state.dag_node_id,
            subagent_run_id=self.child.subagent_run_id,
            attempt_id=self.child.attempt_id,
            parent_session_id=self.state.parent_session_id,
            child_session_id=self.child.child_session_id,
            message_id=self.message_id,
            delivery_state=self.child.delivery_state,
            workspace=self.state.workspace,
            agent=self.child.agent,
            agent_id=self.child.agent_id,
            model=self.model or "",
        )

    async def _emit(self, event: dict[str, Any], *, bus: dict[str, Any] | None = None) -> None:
        if event.get("event_type") == "reasoning_delta":
            self.reasoning_excerpt = merge_reasoning_excerpt(self.reasoning_excerpt, event)
        row = self._enrich(event)
        if self.reasoning_excerpt:
            row["reasoning_excerpt"] = self.reasoning_excerpt
        if bus is not None:
            row["opencode_bus"] = bus
        self.transport.store.append_event(self.state.transport_run_id, row)
        await self.queue.put(row)

    async def _on_bus(self, norm: dict[str, Any], bus: dict[str, Any]) -> None:
        child_session = self.child.child_session_id
        session_id = str(norm.get("session_id") or "")
        affects_child = not session_id or session_id == child_session
        event_type = str(norm.get("event_type") or "")
        if affects_child and event_type == "permission_requested":
            permission_id = str(norm.get("permission_id") or "")
            if permission_id:
                self.seen_permission_ids.add(permission_id)
        if affects_child and event_type == "transcript_delta":
            delta = str(norm.get("delta") or "")
            text = str(norm.get("text") or "")
            if delta:
                self.assistant_text += delta
            elif text and len(text) >= len(self.assistant_text):
                self.assistant_text = text
        if affects_child and norm.get("needs_attention"):
            self.child.delivery_state = DELIVERY_WAITING_PERMISSION
            self.transport._update_child(self.state, self.child)
        if affects_child and event_type == "session_idle":
            self.idle_seen = True
        await self._emit(norm, bus=bus)

    async def _poll_pending_permissions(self) -> None:
        try:
            pending = await self.client.list_permissions(directory=self.state.workspace)
        except Exception:
            return
        for item in pending:
            if str(item.get("sessionID") or "") != self.child.child_session_id:
                continue
            permission_id = str(item.get("id") or item.get("permissionID") or item.get("requestID") or "")
            if not permission_id or permission_id in self.seen_permission_ids:
                continue
            self.seen_permission_ids.add(permission_id)
            self.child.delivery_state = DELIVERY_WAITING_PERMISSION
            self.transport._update_child(self.state, self.child)
            await self._emit(
                {
                    "event_type": "permission_requested",
                    "severity": "warn",
                    "needs_attention": True,
                    "permission_id": permission_id,
                    "permission": item,
                    "source_hint": "opencode_permission_poll",
                }
            )

    async def _send_message(self) -> None:
        try:
            if session_is_busy(
                await self.client.session_status_map(directory=self.state.workspace),
                self.child.child_session_id,
            ):
                await self.client.abort(self.child.child_session_id, directory=self.state.workspace)
                await self.transport.wait_idle(
                    self.client,
                    self.child.child_session_id,
                    deadline=self.deadline,
                    directory=self.state.workspace,
                )

            self.child.delivery_state = DELIVERY_POSTED
            self.transport._update_child(self.state, self.child)
            await self._emit({"event_type": "message.posted", "severity": "info"})

            payload = await self.client.send_message(
                self.child.child_session_id,
                agent=self.child.agent,
                model=self.model,
                parts=text_parts(self.prompt),
                system=self.system,
                directory=self.state.workspace,
            )
            self.message_payload = payload if isinstance(payload, dict) else {}
            self.message_id = message_id_from_payload(self.message_payload)
            self.child.last_message_id = self.message_id
            self.child.delivery_state = DELIVERY_DELIVERED
            self.transport._update_child(self.state, self.child)
            await self._emit(
                {
                    "event_type": "message.delivered",
                    "severity": "info",
                    "message_id": self.message_id,
                }
            )

            text = extract_assistant_text(self.message_payload)
            if text:
                self.assistant_text = text

            provider_error = opencode_message_error(self.message_payload)
            if provider_error:
                if is_opencode_message_aborted_error(provider_error):
                    terminal = self.transport._externally_terminal_child(self.state, self.child)
                    if terminal is None:
                        self.child.delivery_state = DELIVERY_ABORTED
                        self.child.active = False
                        self.transport._update_child(self.state, self.child)
                        self.state.active_subagent_run_id = ""
                        self.transport.store.save(self.state)
                        terminal = self.child
                    result = self.transport._terminal_message_result(
                        self.state,
                        terminal,
                        payload=self.message_payload or {},
                        message_id=self.message_id,
                        event_type="message.aborted",
                        effective_model=self.model or "",
                    )
                    await self._emit(
                        {
                            "event_type": "message.aborted",
                            "severity": "error",
                            "terminal": True,
                            "failure_type": "aborted",
                            "provider_error": provider_error,
                            "result": result,
                        }
                    )
                    return
                result = self.transport.mark_child_blocked(
                    self.state,
                    self.child,
                    reason=provider_error["message"],
                    message_id=self.message_id,
                    payload=self.message_payload or {},
                    effective_model=self.model or "",
                    provider_error=provider_error,
                )
                await self._emit(
                    {
                        "event_type": "message.blocked",
                        "severity": "error",
                        "terminal": True,
                        "failure_type": "provider_error",
                        "blocked_reason": provider_error["message"],
                        "provider_error": provider_error,
                        "result": result,
                    }
                )
                return

            if self.wait_idle and not self.idle_seen:
                try:
                    await self.transport.wait_idle(
                        self.client,
                        self.child.child_session_id,
                        deadline=self.deadline,
                        directory=self.state.workspace,
                    )
                except ProxyError as exc:
                    if exc.error_type != "timeout":
                        raise
                    await self.transport.mark_child_timed_out(
                        self.client,
                        self.state,
                        self.child,
                        reason=str(exc),
                        message_id=self.message_id,
                        payload=self.message_payload or {},
                        effective_model=self.model or "",
                    )
                    await self._emit(
                        {
                            "event_type": "message.timed_out",
                            "severity": "error",
                            "terminal": True,
                            "failure_type": "timeout",
                            "error": str(exc),
                        }
                    )
                    return
                self.idle_seen = True
                self.child.delivery_state = DELIVERY_IDLE_SEEN
                self.transport._update_child(self.state, self.child)
                await self._emit({"event_type": "session.idle_seen", "severity": "info"})
        except BaseException as exc:
            terminal = self.transport._externally_terminal_child(self.state, self.child)
            if terminal is not None:
                self.child = terminal
                return
            self.send_error = exc
            self.child.delivery_state = DELIVERY_FAILED
            self.transport._update_child(self.state, self.child)
            await self._emit(
                {
                    "event_type": "message.failed",
                    "severity": "error",
                    "error": str(exc),
                }
            )

    async def _relay(self) -> None:
        try:
            await parse_opencode_sse_stream(
                self.client,
                watch_session_ids=self.watch_session_ids,
                on_event=self._on_bus,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if self.send_error is None:
                self.send_error = exc
            await self._emit(
                {
                    "event_type": "relay.failed",
                    "severity": "error",
                    "error": str(exc),
                }
            )

    async def _finalize(self) -> dict[str, Any]:
        terminal = self.transport._externally_terminal_child(self.state, self.child)
        if terminal is not None:
            self.child = terminal
            event_type = "message.aborted"
            if terminal.delivery_state == DELIVERY_TIMED_OUT:
                event_type = "message.timed_out"
            elif terminal.delivery_state == DELIVERY_BLOCKED:
                event_type = "message.blocked"
            elif terminal.delivery_state == DELIVERY_SUPERSEDED:
                event_type = "message.superseded"
            return self.transport._terminal_message_result(
                self.state,
                terminal,
                payload=self.message_payload or {},
                message_id=self.message_id,
                event_type=event_type,
                effective_model=self.model or "",
            )

        if not self.assistant_text:
            text, _ = await self.transport._latest_assistant(
                self.client,
                self.child.child_session_id,
                self.state.workspace,
            )
            if text:
                self.assistant_text = text

        if self.child.mode == "workspace_write":
            blocker = assistant_text_blocker(self.assistant_text)
            if blocker is not None:
                return self.transport.mark_child_blocked(
                    self.state,
                    self.child,
                    reason=blocker["blocked_reason"],
                    message_id=self.message_id,
                    payload=self.message_payload or {},
                    effective_model=self.model or "",
                    extra={"receipt_classifier": blocker["receipt_classifier"]},
                )

        diff = await self.client.diff(self.child.child_session_id, directory=self.state.workspace)
        if self.assistant_text:
            self.child.delivery_state = DELIVERY_ACTED
            self.transport._update_child(self.state, self.child)

        if self.child.mode == "propose_patches" and not git_diff_empty(Path(self.state.workspace)):
            self.child.delivery_state = DELIVERY_FAILED
            self.transport._update_child(self.state, self.child)
            raise ProxyError(
                409,
                "read-only propose_patches mode requires empty git diff after message",
                "write_allowlist_violation",
            )

        if self.child.delivery_state == DELIVERY_ACTED:
            self.child.delivery_state = DELIVERY_COMPLETED
            self.transport._update_child(self.state, self.child)

        if self.child.delivery_state == DELIVERY_COMPLETED:
            await self.transport.mirror_worker_completed(
                self.client,
                self.state,
                self.child,
                model=self.model,
                assistant_text=self.assistant_text,
            )
            self.transport._append_attachment_events(
                self.state,
                subagent_run_id=self.child.subagent_run_id,
                text_chunks=[self.assistant_text] if self.assistant_text else None,
            )

        if self.send_error is not None and self.child.delivery_state != DELIVERY_COMPLETED:
            raise self.send_error

        return {
            "schema": "scillm.opencode_transport.message.v1",
            "transport_run_id": self.state.transport_run_id,
            "subagent_run_id": self.child.subagent_run_id,
            "child_session_id": self.child.child_session_id,
            "message_id": self.message_id,
            "delivery_state": self.child.delivery_state,
            "assistant_text": self.assistant_text,
            "reasoning_excerpt": self.reasoning_excerpt,
            "diff": diff,
            "message": self.message_payload,
            "events_stream": (
                f"/v1/scillm/opencode/transport/runs/{self.state.transport_run_id}/events/stream"
            ),
        }

    async def iter_events(self) -> AsyncIterator[dict[str, Any]]:
        effective_prompt, human_turns = await self.transport.prepare_worker_prompt(
            self.client, self.state, self.prompt
        )
        self.prompt = effective_prompt
        if human_turns:
            await self.transport.mirror_human_input_incorporated(
                self.client, self.state, human_turns
            )
            self.transport.acknowledge_human_turns(self.state, human_turns)
            self.transport.store.append_event(
                self.state.transport_run_id,
                enrich_event(
                    {
                        "event_type": "dialog.human_incorporated",
                        "human_message_ids": [t.message_id for t in human_turns if t.message_id],
                        "human_turn_count": len(human_turns),
                    },
                    transport_run_id=self.state.transport_run_id,
                    dag_node_id=self.state.dag_node_id,
                    parent_session_id=self.state.parent_session_id,
                    workspace=self.state.workspace,
                ),
            )
        self.child.delivery_state = DELIVERY_QUEUED
        self.transport._update_child(self.state, self.child)
        await self.transport.mirror_worker_dispatch(
            self.client,
            self.state,
            self.child,
            model=self.model,
            prompt=self.prompt,
        )
        await self._emit({"event_type": "message.queued", "severity": "info", "prompt": self.prompt})

        self._relay_task = asyncio.create_task(self._relay())
        self._send_task = asyncio.create_task(self._send_message())
        started = time.monotonic()
        next_heartbeat = started + self.heartbeat_s

        try:
            while time.monotonic() < self.deadline:
                timeout = min(self.heartbeat_s, max(0.05, self.deadline - time.monotonic()))
                try:
                    row = await asyncio.wait_for(self.queue.get(), timeout=timeout)
                    yield row
                    if row.get("terminal"):
                        break
                except asyncio.TimeoutError:
                    if self._send_task.done() and (
                        self.idle_seen
                        or not self.wait_idle
                        or self.child.delivery_state == DELIVERY_WAITING_PERMISSION
                        or self.send_error is not None
                        or self.child.delivery_state == DELIVERY_FAILED
                        or self.child.delivery_state == DELIVERY_TIMED_OUT
                        or self.child.delivery_state == DELIVERY_BLOCKED
                    ):
                        break
                    if time.monotonic() >= next_heartbeat:
                        next_heartbeat = time.monotonic() + self.heartbeat_s
                        await self._poll_pending_permissions()
                        yield self._enrich(
                            {
                                "event_type": "heartbeat",
                                "severity": "info",
                                "elapsed_s": round(time.monotonic() - started, 1),
                                "delivery_state": self.child.delivery_state,
                                "reasoning_excerpt": self.reasoning_excerpt[-500:],
                            }
                        )
        finally:
            if self._relay_task is not None:
                self._relay_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._relay_task
            if self._send_task is not None and not self._send_task.done():
                if time.monotonic() >= self.deadline:
                    await self.transport.mark_child_timed_out(
                        self.client,
                        self.state,
                        self.child,
                        reason=(
                            f"transport stream exceeded timeout budget "
                            f"for {self.child.child_session_id}"
                        ),
                        message_id=self.message_id,
                        payload=self.message_payload or {},
                        effective_model=self.model or "",
                    )
                    self._send_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._send_task
                else:
                    await self._send_task

        result = await self._finalize()
        final_event_type = "message.completed"
        final_severity = "info"
        if result.get("delivery_state") == DELIVERY_ABORTED:
            final_event_type = "message.aborted"
        elif result.get("delivery_state") == DELIVERY_TIMED_OUT:
            final_event_type = "message.timed_out"
            final_severity = "error"
        elif result.get("delivery_state") == DELIVERY_BLOCKED:
            final_event_type = "message.blocked"
            final_severity = "error"
        elif result.get("delivery_state") == DELIVERY_SUPERSEDED:
            final_event_type = "message.superseded"
        yield self._enrich(
            {
                "event_type": final_event_type,
                "severity": final_severity,
                "terminal": True,
                "result": result,
            }
        )


async def iter_transport_message_stream(
    transport: Any,
    client: OpenCodeServeClient,
    state: TransportState,
    child: ChildAttempt,
    *,
    prompt: str,
    system: str | None = None,
    model: str | None = None,
    timeout_s: float = 600.0,
    heartbeat_s: float = 15.0,
    wait_idle: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    run = TransportMessageStream(
        transport,
        client,
        state,
        child,
        prompt=prompt,
        system=system,
        model=model,
        timeout_s=timeout_s,
        heartbeat_s=heartbeat_s,
        wait_idle=wait_idle,
    )
    async for row in run.iter_events():
        yield row
