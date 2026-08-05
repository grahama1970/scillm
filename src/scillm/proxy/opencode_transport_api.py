"""HTTP API for scillm OpenCode transport v1."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Callable, Literal

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator

from scillm.proxy.errors import ProxyError
from scillm.proxy.opencode_serve import OpenCodeServeClient, load_opencode_serve_settings
from scillm.proxy.opencode_transport_stream import transport_event_sse
from scillm.proxy.streaming import SSE_HEADERS, sse_liveness_wrapper
from scillm.proxy.opencode_transport import (
    OpenCodeTransport,
    TransportState,
    TransportStore,
    build_transport_observation,
    build_dialog_collaboration_contract,
    list_transport_run_index,
    transport_output_base,
)

AuthCheck = Callable[[Request], Awaitable[str | None]]


from scillm.proxy.opencode_transport_events import (
    normalize_opencode_bus_event,
    parse_sse_json_payload,
)


def _is_opencode_session_not_found(exc: ProxyError) -> bool:
    message = str(exc)
    return "Session not found:" in message and "opencode serve" in message


def _stale_dialog_response(
    *,
    transport_run_id: str,
    state: TransportState,
    settings: Any,
) -> dict[str, Any]:
    observation = build_transport_observation(
        transport_run_id=transport_run_id,
        state=state,
        settings=settings,
    )
    observation["transcript_unavailable_reason"] = "parent_session_not_found"
    observation["transcript_unavailable_detail"] = (
        "The transport run metadata exists, but its OpenCode parent session is no longer available in the sidecar."
    )
    return {
        **build_dialog_collaboration_contract(
            transport_run_id=transport_run_id,
            state=state,
        ),
        "human_can_participate": False,
        "project_agent_can_participate": False,
        "turns": [],
        "pending_human": [],
        "observation": observation,
        "not_proven": [
            "OpenCode parent session was not found in the sidecar; transcript turns are unavailable for this historical run."
        ],
    }

def _watch_session_ids_for_state(state: TransportState) -> frozenset[str]:
    ids: set[str] = set()
    if state.parent_session_id:
        ids.add(state.parent_session_id)
    for row in state.children:
        if isinstance(row, dict):
            sid = str(row.get("child_session_id") or "").strip()
            if sid:
                ids.add(sid)
    return frozenset(ids)

class TransportCreateRequest(BaseModel):
    dag_node_id: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    title: str | None = None
    transport_run_id: str | None = None


class TransportChildRequest(BaseModel):
    agent_id: str | None = None
    role: str = ""
    agent: str = ""
    mode: str = ""
    title: str | None = None
    skills: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_role_or_agent_id(self) -> "TransportChildRequest":
        if not (self.agent_id or "").strip() and not (self.role or "").strip():
            raise ValueError("agent_id or role is required")
        return self


class TransportMessageRequest(BaseModel):
    prompt: str = Field(min_length=1)
    system: str | None = None
    model: str | None = None
    subagent_run_id: str | None = None
    agent_id: str | None = None
    role: str | None = None
    agent: str | None = None
    timeout_s: float = Field(default=600.0, ge=10.0, le=3600.0)
    wait_idle: bool = True
    stream: bool = True
    heartbeat_s: float = Field(default=15.0, ge=1.0, le=120.0)
    fork_supersede: bool = False
    fork_reason: str = ""
    skills: list[str] = Field(default_factory=list)


class TransportSkillCallRequest(BaseModel):
    skill: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    speaker: str = Field(default="Project agent", min_length=1)
    user_note: str = ""
    dry_run: bool | None = None
    turn_id: str | None = None


class TransportDialogPostRequest(BaseModel):
    speaker: str = Field(default="Project agent", min_length=1)
    body: str = Field(min_length=1)
    execute_skills: bool = True
    dry_run: bool | None = None


class TransportForkSupersedeRequest(BaseModel):
    role: str
    agent: str
    agent_id: str | None = None
    reason: str = Field(min_length=1)
    message_id: str | None = None
    mode: str = "propose_patches"


class TransportAbortRequest(BaseModel):
    reason: str = Field(default="operator_abort", min_length=1)


class TransportPermissionReplyRequest(BaseModel):
    permission_id: str = Field(min_length=1)
    response: Literal["once", "always", "reject"] = "reject"
    reason: str = Field(default="permission_rejected", min_length=1)
    subagent_run_id: str | None = None


def register_opencode_transport_routes(router: APIRouter, auth: AuthCheck) -> None:
    @router.get("/opencode/transport/run-index")
    async def transport_run_index(request: Request) -> JSONResponse:
        await auth(request)
        from scillm.proxy.opencode_transport import list_transport_run_index as _list_runs

        return JSONResponse(
            {
                "schema": "scillm.transport.run_index.v1",
                "runs": _list_runs(),
            }
        )

    @router.get("/opencode/transport/capabilities")
    async def transport_capabilities(request: Request) -> JSONResponse:
        await auth(request)
        settings = load_opencode_serve_settings()
        transport = OpenCodeTransport()
        async with OpenCodeServeClient(settings) as client:
            caps = await transport.probe_capabilities(client)
        return JSONResponse(caps)

    @router.get("/loop2/capabilities")
    async def loop2_capabilities(request: Request) -> JSONResponse:
        await auth(request)
        settings = load_opencode_serve_settings()
        transport = OpenCodeTransport()
        async with OpenCodeServeClient(settings) as client:
            transport_caps = await transport.probe_capabilities(client)
        return JSONResponse(
            {
                "schema": "scillm.loop2.capabilities.v1",
                "loop2_api": True,
                "transport_api": bool(transport_caps.get("transport_api", True)),
                "required_endpoints": [
                    "POST /v1/scillm/opencode/transport/runs",
                    "POST /v1/scillm/opencode/transport/runs/{transport_run_id}/children",
                    "POST /v1/scillm/opencode/transport/runs/{transport_run_id}/message",
                    "GET /v1/scillm/opencode/transport/runs/{transport_run_id}/events/stream",
                ],
                "caller_skill_header_required": True,
                "opencode": transport_caps,
            }
        )

    @router.post("/opencode/transport/runs")
    async def transport_create_run(spec: TransportCreateRequest, request: Request) -> JSONResponse:
        await auth(request)
        settings = load_opencode_serve_settings()
        transport = OpenCodeTransport()
        async with OpenCodeServeClient(settings) as client:
            state = await transport.create_transport_run(
                client,
                dag_node_id=spec.dag_node_id,
                workspace=spec.workspace,
                title=spec.title,
                transport_run_id=spec.transport_run_id,
            )
        return JSONResponse(
            {
                "schema": "scillm.opencode_transport.create.v1",
                "transport_run_id": state.transport_run_id,
                "parent_session_id": state.parent_session_id,
                "workspace": state.workspace,
                "opencode_url": state.opencode_url,
                "artifact_dir": str(transport_output_base() / state.transport_run_id),
                "observation": build_transport_observation(
                    transport_run_id=state.transport_run_id,
                    state=state,
                    settings=settings,
                ),
            }
        )

    @router.get("/opencode/transport/runs/{transport_run_id}")
    async def transport_get_run(transport_run_id: str, request: Request) -> JSONResponse:
        await auth(request)
        store = TransportStore(transport_output_base())
        state = store.load(transport_run_id)
        settings = load_opencode_serve_settings()
        from scillm.proxy.opencode_transport import project_transport_state

        return JSONResponse(
            {
                "schema": "scillm.opencode_transport.state.v1",
                "state": state.to_dict(),
                # Wrapper state projected over the active child's live serve
                # status — never shows terminal while the child is running.
                "projection": project_transport_state(state.to_dict()),
                "observation": build_transport_observation(
                    transport_run_id=transport_run_id,
                    state=state,
                    settings=settings,
                ),
            }
        )

    @router.post("/opencode/transport/runs/{transport_run_id}/children")
    async def transport_create_child(
        transport_run_id: str,
        spec: TransportChildRequest,
        request: Request,
    ) -> JSONResponse:
        await auth(request)
        settings = load_opencode_serve_settings()
        store = TransportStore(transport_output_base())
        state = store.load(transport_run_id)
        transport = OpenCodeTransport(store)
        async with OpenCodeServeClient(settings) as client:
            child = await transport.create_child(
                client,
                state,
                role=spec.role,
                agent=spec.agent,
                mode=spec.mode,
                title=spec.title,
                skills=spec.skills or None,
                agent_id=spec.agent_id,
            )
        state = store.load(transport_run_id)
        return JSONResponse(
            {
                "schema": "scillm.opencode_transport.child.v1",
                "transport_run_id": transport_run_id,
                "child": child.to_dict(),
                "observation": build_transport_observation(
                    transport_run_id=transport_run_id,
                    state=state,
                    settings=settings,
                ),
            }
        )

    @router.post("/opencode/transport/runs/{transport_run_id}/message")
    async def transport_post_message(
        transport_run_id: str,
        spec: TransportMessageRequest,
        request: Request,
    ):
        await auth(request)
        settings = load_opencode_serve_settings()
        store = TransportStore(transport_output_base())
        state = store.load(transport_run_id)
        transport = OpenCodeTransport(store)

        async def resolve_child(client):
            from scillm.proxy.opencode_transport import ChildAttempt

            nonlocal state
            child = state.active_child()
            if spec.fork_supersede and child is not None:
                role = spec.role or child.role
                agent = spec.agent or child.agent
                child = await transport.fork_supersede(
                    client,
                    state,
                    role=role,
                    agent=agent,
                    agent_id=spec.agent_id or child.agent_id,
                    reason=spec.fork_reason or "course correction",
                    mode=child.mode,
                )
                state = store.load(transport_run_id)
                child = state.active_child()
            elif child is None or (spec.role and spec.role != child.role):
                role = spec.role or ("" if spec.agent_id else "patch")
                agent = spec.agent or ("" if spec.agent_id else "explore")
                mode = "" if spec.agent_id else "propose_patches"
                child = await transport.create_child(
                    client,
                    state,
                    role=role,
                    agent=agent,
                    mode=mode,
                    skills=spec.skills or None,
                    agent_id=spec.agent_id,
                )
                state = store.load(transport_run_id)
                child = state.active_child()
            elif spec.subagent_run_id:
                state = store.load(transport_run_id)
                child = None
                for row in state.children:
                    if isinstance(row, dict) and row.get("subagent_run_id") == spec.subagent_run_id:
                        child = ChildAttempt(**row)
                        child.active = True
                        break
            if child is None:
                raise ProxyError(409, "no active child session for message", "invalid_state")
            return child

        if spec.stream:
            async def event_stream():
                async with OpenCodeServeClient(settings) as client:
                    child = await resolve_child(client)
                    async for row in transport.iter_transport_message_stream(
                        client,
                        state,
                        child,
                        prompt=spec.prompt,
                        system=spec.system,
                        model=spec.model,
                        timeout_s=spec.timeout_s,
                        heartbeat_s=spec.heartbeat_s,
                        wait_idle=spec.wait_idle,
                    ):
                        yield transport_event_sse(row)

            wrapped = sse_liveness_wrapper(
                event_stream(),
                overall_timeout_s=spec.timeout_s + 60.0,
                heartbeat_interval_s=spec.heartbeat_s,
                progress_events=True,
            )
            return StreamingResponse(wrapped, media_type="text/event-stream", headers=SSE_HEADERS)

        async with OpenCodeServeClient(settings) as client:
            child = await resolve_child(client)
            result = await transport.post_message_sync(
                client,
                state,
                child,
                prompt=spec.prompt,
                system=spec.system,
                model=spec.model,
                timeout_s=spec.timeout_s,
                wait_idle=spec.wait_idle,
            )
        return JSONResponse(result)


    @router.get("/opencode/transport/runs/{transport_run_id}/events/stream")
    async def transport_events_stream(
        transport_run_id: str,
        request: Request,
        after_line: int = 0,
        heartbeat_s: float = 15.0,
        timeout_s: float = 600.0,
    ) -> StreamingResponse:
        """Tail ``events.jsonl`` then follow live transport events (SSE)."""
        await auth(request)
        store = TransportStore(transport_output_base())
        transport = OpenCodeTransport(store)
        state = store.load(transport_run_id)
        watch_ids = _watch_session_ids_for_state(state)
        tail_rows, _tail_line = transport.tail_transport_events(
            transport_run_id, after_line=after_line
        )
        settings = load_opencode_serve_settings()

        async def tail_then_live():
            for row in tail_rows:
                yield transport_event_sse(row)
            buffer = ""
            async with OpenCodeServeClient(settings) as client:
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
                        norm = normalize_opencode_bus_event(
                            bus, watch_session_ids=watch_ids
                        )
                        if norm is not None:
                            yield transport_event_sse(norm)

        stream = sse_liveness_wrapper(
            tail_then_live(),
            overall_timeout_s=timeout_s,
            heartbeat_interval_s=heartbeat_s,
            progress_events=True,
        )
        return StreamingResponse(stream, media_type="text/event-stream", headers=SSE_HEADERS)



    @router.get("/opencode/transport/runs/{transport_run_id}/dialog")
    async def transport_get_dialog(transport_run_id: str, request: Request) -> JSONResponse:
        """Three-way collaboration transcript on the parent OpenCode session."""
        await auth(request)
        settings = load_opencode_serve_settings()
        store = TransportStore(transport_output_base())
        state = store.load(transport_run_id)
        transport = OpenCodeTransport(store)
        async with OpenCodeServeClient(settings) as client:
            try:
                turns = await transport.list_dialog_turns(client, state)
                pending = await transport.pending_human_turns(client, state)
            except ProxyError as exc:
                if _is_opencode_session_not_found(exc):
                    return JSONResponse(
                        _stale_dialog_response(
                            transport_run_id=transport_run_id,
                            state=state,
                            settings=settings,
                        )
                    )
                raise
        return JSONResponse(
            {
                **build_dialog_collaboration_contract(
                    transport_run_id=transport_run_id, state=state
                ),
                "turns": [t.to_dict() for t in turns],
                "pending_human": [t.to_dict() for t in pending],
                "observation": build_transport_observation(
                    transport_run_id=transport_run_id,
                    state=state,
                    settings=settings,
                ),
            }
        )

    @router.post("/opencode/transport/runs/{transport_run_id}/dialog")
    async def transport_post_dialog(
        transport_run_id: str,
        spec: TransportDialogPostRequest,
        request: Request,
    ) -> JSONResponse:
        """Project-agent (or labeled) post into the collaboration room."""
        await auth(request)
        settings = load_opencode_serve_settings()
        store = TransportStore(transport_output_base())
        state = store.load(transport_run_id)
        transport = OpenCodeTransport(store)
        async with OpenCodeServeClient(settings) as client:
            skill_result = await transport.post_dialog_message(
                client,
                state,
                speaker=spec.speaker,
                body=spec.body,
                execute_skills=spec.execute_skills,
                dry_run=spec.dry_run,
            )
        return JSONResponse(
            {
                "schema": "scillm.opencode_transport.dialog_post.v1",
                "transport_run_id": transport_run_id,
                "speaker": spec.speaker,
                "skill_call": skill_result,
                "observation": build_transport_observation(
                    transport_run_id=transport_run_id,
                    state=state,
                    settings=settings,
                ),
            }
        )


    @router.post("/opencode/transport/runs/{transport_run_id}/skill-call")
    async def transport_post_skill_call(
        transport_run_id: str,
        spec: TransportSkillCallRequest,
        request: Request,
    ) -> JSONResponse:
        """Execute a mediated harness skill_call and mirror the receipt into the collaboration dialog."""
        await auth(request)
        settings = load_opencode_serve_settings()
        store = TransportStore(transport_output_base())
        state = store.load(transport_run_id)
        transport = OpenCodeTransport(store)
        async with OpenCodeServeClient(settings) as client:
            result = await transport.execute_skill_call(
                client,
                state,
                skill=spec.skill,
                args=spec.args,
                speaker=spec.speaker,
                user_note=spec.user_note or "",
                dry_run=spec.dry_run,
                turn_id=spec.turn_id,
            )
        state = store.load(transport_run_id)
        return JSONResponse(
            {
                **result,
                "observation": build_transport_observation(
                    transport_run_id=transport_run_id,
                    state=state,
                    settings=settings,
                ),
            }
        )



    @router.get("/opencode/transport/runs/{transport_run_id}/artifacts/{artifact_name}")
    async def transport_get_artifact(
        transport_run_id: str,
        artifact_name: str,
        request: Request,
    ) -> FileResponse:
        """Serve a registered figure/file from the transport run artifacts directory."""
        import mimetypes

        from scillm.proxy.errors import ProxyError
        from scillm.proxy.opencode_transport_attachments import resolve_served_artifact_path

        await auth(request)
        try:
            path = resolve_served_artifact_path(transport_run_id, artifact_name)
        except FileNotFoundError as exc:
            raise ProxyError(404, str(exc), "artifact_not_found") from exc
        except ValueError as exc:
            raise ProxyError(400, str(exc), "invalid_artifact_name") from exc
        media_type, _ = mimetypes.guess_type(str(path))
        return FileResponse(path, media_type=media_type or "application/octet-stream", filename=path.name)

    @router.post("/opencode/transport/runs/{transport_run_id}/fork-supersede")
    async def transport_fork_supersede(
        transport_run_id: str,
        spec: TransportForkSupersedeRequest,
        request: Request,
    ) -> JSONResponse:
        await auth(request)
        settings = load_opencode_serve_settings()
        store = TransportStore(transport_output_base())
        state = store.load(transport_run_id)
        transport = OpenCodeTransport(store)
        async with OpenCodeServeClient(settings) as client:
            child = await transport.fork_supersede(
                client,
                state,
                role=spec.role,
                agent=spec.agent,
                agent_id=spec.agent_id,
                reason=spec.reason,
                message_id=spec.message_id,
                mode=spec.mode,
            )
        state = store.load(transport_run_id)
        return JSONResponse(
            {
                "schema": "scillm.opencode_transport.fork.v1",
                "transport_run_id": transport_run_id,
                "child": child.to_dict(),
                "observation": build_transport_observation(
                    transport_run_id=transport_run_id,
                    state=state,
                    settings=settings,
                ),
            }
        )

    @router.post("/opencode/transport/runs/{transport_run_id}/abort")
    async def transport_abort_active_child(
        transport_run_id: str,
        spec: TransportAbortRequest,
        request: Request,
    ) -> JSONResponse:
        await auth(request)
        settings = load_opencode_serve_settings()
        store = TransportStore(transport_output_base())
        state = store.load(transport_run_id)
        transport = OpenCodeTransport(store)
        async with OpenCodeServeClient(settings) as client:
            result = await transport.abort_active_child(
                client,
                state,
                reason=spec.reason,
            )
        state = store.load(transport_run_id)
        return JSONResponse(
            {
                **result,
                "observation": build_transport_observation(
                    transport_run_id=transport_run_id,
                    state=state,
                    settings=settings,
                ),
            }
        )

    @router.post("/opencode/transport/runs/{transport_run_id}/permission/reply")
    async def transport_reply_permission(
        transport_run_id: str,
        spec: TransportPermissionReplyRequest,
        request: Request,
    ) -> JSONResponse:
        await auth(request)
        settings = load_opencode_serve_settings()
        store = TransportStore(transport_output_base())
        state = store.load(transport_run_id)
        transport = OpenCodeTransport(store)
        async with OpenCodeServeClient(settings) as client:
            result = await transport.reply_child_permission(
                client,
                state,
                permission_id=spec.permission_id,
                response=spec.response,
                reason=spec.reason,
                subagent_run_id=spec.subagent_run_id,
            )
        state = store.load(transport_run_id)
        return JSONResponse(
            {
                **result,
                "observation": build_transport_observation(
                    transport_run_id=transport_run_id,
                    state=state,
                    settings=settings,
                ),
            }
        )
