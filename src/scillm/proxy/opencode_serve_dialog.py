"""Bridge OpenCode serve runs (oc-*) into transport-collaboration UI contracts."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from scillm.proxy.opencode_serve import OpenCodeServeSettings, load_opencode_serve_settings
from scillm.proxy.opencode_serve import session_is_busy
from scillm.proxy.opencode_transport import format_dialog_message
from scillm.proxy.errors import ProxyError
from scillm.proxy import opencode_serve_api as serve_api
from scillm.proxy.opencode_serve_api import (
    OpenCodeServeRun,
    _format_message_for_dialog,
    _message_role,
)


SERVE_DIALOG_JSONL = "transport_dialog.jsonl"


def _serve_dialog_path(run: OpenCodeServeRun) -> Path:
    return run.run_dir / SERVE_DIALOG_JSONL


def _read_serve_dialog_turns(run: OpenCodeServeRun) -> list[dict[str, Any]]:
    path = _serve_dialog_path(run)
    if not path.is_file():
        return []
    turns: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            turns.append(row)
    return turns


def append_serve_dialog_turn(run: OpenCodeServeRun, turn: dict[str, Any]) -> None:
    path = _serve_dialog_path(run)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(turn, ensure_ascii=False) + "\n")
    run.emit("dialog.posted", speaker=turn.get("speaker"), message_id=turn.get("message_id"))


def _pending_dialog_path(run: OpenCodeServeRun) -> Path:
    return run.run_dir / "pending_dialog.jsonl"


def queue_pending_dialog_turn(run: OpenCodeServeRun, turn: dict[str, Any]) -> None:
    """Queue a dialog turn posted while the child turn was active (issue #13).

    The serve poll loop replays queued turns as a real prompt the moment the
    session goes idle, so a nudge actually reaches the child instead of
    silently landing as a side-channel note.
    """
    path = _pending_dialog_path(run)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(turn, ensure_ascii=False) + "\n")
    run.emit("dialog.queued_for_next_turn", speaker=turn.get("speaker"), message_id=turn.get("message_id"))


def drain_pending_dialog(run: OpenCodeServeRun) -> list[dict[str, Any]]:
    """Atomically consume queued dialog turns; empty list when none."""
    path = _pending_dialog_path(run)
    if not path.is_file():
        return []
    consumed = path.with_suffix(".consuming")
    try:
        path.rename(consumed)
    except OSError:
        return []
    turns: list[dict[str, Any]] = []
    try:
        for line in consumed.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                turns.append(row)
    finally:
        try:
            consumed.unlink()
        except OSError:
            pass
    return turns



def _serve_index_bases() -> list[Path]:
    base = serve_api._artifact_root()
    if base.is_dir():
        return [base]
    return []


def list_serve_run_index() -> list[dict[str, object]]:
    """Scan serve artifact dirs for the run picker.

    Membership is "has a status.json", not a run-id naming convention:
    caller-supplied run_ids (issue #10) must be just as visible as the
    default ``oc-*`` ids.
    """
    rows: list[dict[str, object]] = []
    for root in _serve_index_bases():
        for ent in root.iterdir():
            if not ent.is_dir():
                continue
            status_path = ent / "status.json"
            if not status_path.is_file():
                continue
            try:
                status = json.loads(status_path.read_text(encoding="utf-8"))
                st = status_path.stat()
                case_id = ""
                req_path = ent / "request.json"
                if req_path.is_file():
                    req = json.loads(req_path.read_text(encoding="utf-8"))
                    meta = req.get("scillm_metadata") if isinstance(req.get("scillm_metadata"), dict) else {}
                    case_id = str(meta.get("case_id") or meta.get("item_id") or req.get("batch_id") or "")
                monitor = status.get("human_monitor") if isinstance(status.get("human_monitor"), dict) else {}
                run_id = str(status.get("run_id") or ent.name)
                monitor_url = (
                    monitor.get("human_monitor_url")
                    or monitor.get("scillm_chat_monitor_url")
                    or status.get("human_monitor_url")
                )
                title = (
                    status.get("title")
                    or monitor.get("session_title")
                    or status.get("session_title")
                    or case_id
                    or run_id
                )
                rows.append(
                    {
                        "run_id": run_id,
                        "id": run_id,
                        "transport_run_id": run_id,
                        "title": title,
                        "dag_node_id": case_id or status.get("caller_skill"),
                        "mtime_ms": int(st.st_mtime * 1000),
                        "updated_at": status.get("updated_at"),
                        "run_kind": "opencode_serve",
                        "state": status.get("state"),
                        "phase": status.get("phase"),
                        "session_id": status.get("session_id"),
                        "human_monitor_url": monitor_url,
                        "caller_skill": status.get("caller_skill"),
                        "agent": status.get("agent"),
                    }
                )
            except (OSError, json.JSONDecodeError, TypeError):
                continue
    rows.sort(key=lambda r: int(r.get("mtime_ms") or 0), reverse=True)
    return rows


def _load_messages(run: OpenCodeServeRun) -> list[dict[str, Any]]:
    snapshot_path = run.run_dir / "messages_snapshot.json"
    if snapshot_path.is_file():
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            messages = snapshot.get("messages")
            if isinstance(messages, list):
                return [m for m in messages if isinstance(m, dict)]
        except json.JSONDecodeError:
            pass
    result_path = run.run_dir / "opencode_result.json"
    if result_path.is_file():
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            message = result.get("message")
            if isinstance(message, dict):
                return [message]
        except json.JSONDecodeError:
            pass
    return []


def _delivery_state_from_status(status: dict[str, Any]) -> str:
    state = str(status.get("state") or "").strip().lower()
    phase = str(status.get("phase") or "").strip().lower()
    if state in {"completed", "success", "ok"}:
        return "completed"
    if state == "timeout" or phase == "timed_out":
        return "timed_out"
    if state == "failed":
        return "failed"
    if state == "running":
        return "running"
    return state or "unknown"


def _message_to_turn(
    message: dict[str, Any],
    *,
    run: OpenCodeServeRun,
    index: int,
) -> dict[str, Any]:
    role = _message_role(message) or "assistant"
    info = message.get("info") if isinstance(message.get("info"), dict) else {}
    message_id = str(info.get("id") or f"{run.run_id}-msg-{index}")
    body = _format_message_for_dialog(message)
    if role == "user":
        collaborator = "project_agent"
        speaker = str(run.caller_skill or "Project agent")
    else:
        collaborator = "worker"
        speaker = str(info.get("agent") or run.agent or "OpenCode worker")
    return {
        "message_id": message_id,
        "collaborator": collaborator,
        "speaker": speaker,
        "text": format_dialog_message(speaker, body),
        "role": role,
        "subagent_run_id": run.run_id,
        "subagent_kind": "opencode_serve",
        "subagent_label": "Serve child",
        "agent": info.get("agent") or run.agent,
        "child_session_id": run.session_id,
    }


def build_serve_observation(
    run: OpenCodeServeRun,
    *,
    settings: OpenCodeServeSettings | None = None,
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = settings or load_opencode_serve_settings()
    status = status or {}
    human_monitor = run.human_monitor if isinstance(run.human_monitor, dict) else status.get("human_monitor")
    if not isinstance(human_monitor, dict):
        human_monitor = {}
    scillm_base = str(human_monitor.get("scillm_base_url") or "http://127.0.0.1:4001").rstrip("/")
    monitor_url = human_monitor.get("scillm_chat_monitor_url") or human_monitor.get("human_monitor_url")
    return {
        "transport_run_id": run.run_id,
        "opencode_url": settings.base_url,
        "parent_session_id": run.session_id,
        "active_child_session_id": run.session_id,
        "browser_dialog_url": monitor_url,
        "browser_worker_url": monitor_url,
        "scillm_dialog_api": f"{scillm_base}/v1/scillm/opencode/runs/{run.run_id}/dialog",
        "scillm_events_stream": f"{scillm_base}/v1/scillm/opencode/runs/{run.run_id}/events/stream",
        "collaboration_mode": "opencode_serve_child",
        "parent_ui_model_note": "Serve child session (pdf-lab / POST /opencode/runs). Human chat via scillm monitor URL.",
    }


async def _load_messages_for_dialog(run: OpenCodeServeRun, *, status: dict[str, Any]) -> list[dict[str, Any]]:
    state = str(status.get("state") or "").strip().lower()
    active = run.run_id in serve_api._ACTIVE_RUNS or state == "running"
    if active and run.session_id:
        try:
            from scillm.proxy.opencode_serve import OpenCodeServeClient

            async with OpenCodeServeClient() as client:
                live = await client.list_messages(
                    run.session_id, limit=200, directory=run.directory
                )
            if isinstance(live, list) and live:
                return [m for m in live if isinstance(m, dict)]
        except Exception:
            pass
    return _load_messages(run)


async def build_serve_dialog_response_async(run: OpenCodeServeRun) -> dict[str, Any]:
    status: dict[str, Any] = {}
    if run.status_path.is_file():
        try:
            status = json.loads(run.status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            status = {}
    messages = await _load_messages_for_dialog(run, status=status)
    turns = [_message_to_turn(m, run=run, index=i) for i, m in enumerate(messages)]
    for row in _read_serve_dialog_turns(run):
        turns.append(row)
    turns.sort(key=lambda row: str(row.get("message_id") or ""))
    delivery = _delivery_state_from_status(status)
    child = {
        "subagent_run_id": run.run_id,
        "role": "patch",
        "subagent_kind": "opencode_serve",
        "subagent_label": "Serve child",
        "agent": run.agent,
        "agent_id": run.agent,
        "child_session_id": run.session_id,
        "delivery_state": delivery,
        "active": delivery in {"running", "queued", "posted", "delivered"},
    }
    observation = build_serve_observation(run, status=status)
    pending: list[dict[str, Any]] = []
    if status.get("state") == "timeout":
        blocker = status.get("terminal_blocker")
        if isinstance(blocker, dict):
            excerpt = str(blocker.get("last_assistant_excerpt") or "").strip()
            if excerpt:
                pending.append(
                    {
                        "message_id": f"{run.run_id}-timeout-blocker",
                        "collaborator": "worker",
                        "speaker": "Timeout summary",
                        "text": format_dialog_message("Timeout summary", excerpt),
                        "role": "assistant",
                        "subagent_run_id": run.run_id,
                    }
                )
    return {
        "schema": "scillm.opencode_serve.dialog.v1",
        "transport_run_id": run.run_id,
        "run_kind": "opencode_serve",
        "collaborators": ["human", "project_agent", "worker"],
        "human_can_participate": False,
        "project_agent_can_participate": True,
        "dialog_session_id": run.session_id,
        "children": [child],
        "active_subagent": child,
        "turns": turns,
        "pending_human": pending,
        "observation": observation,
        "status": status,
    }


def build_serve_dialog_response(run: OpenCodeServeRun) -> dict[str, Any]:
    """Sync fallback (snapshot only). Prefer build_serve_dialog_response_async in routes."""
    status: dict[str, Any] = {}
    if run.status_path.is_file():
        try:
            status = json.loads(run.status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            status = {}
    messages = _load_messages(run)
    turns = [_message_to_turn(m, run=run, index=i) for i, m in enumerate(messages)]
    turns.extend(_read_serve_dialog_turns(run))
    delivery = _delivery_state_from_status(status)
    child = {
        "subagent_run_id": run.run_id,
        "role": "patch",
        "subagent_kind": "opencode_serve",
        "subagent_label": "Serve child",
        "agent": run.agent,
        "agent_id": run.agent,
        "child_session_id": run.session_id,
        "delivery_state": delivery,
        "active": delivery in {"running", "queued", "posted", "delivered"},
    }
    return {
        "schema": "scillm.opencode_serve.dialog.v1",
        "transport_run_id": run.run_id,
        "run_kind": "opencode_serve",
        "collaborators": ["human", "project_agent", "worker"],
        "human_can_participate": False,
        "project_agent_can_participate": True,
        "dialog_session_id": run.session_id,
        "children": [child],
        "active_subagent": child,
        "turns": turns,
        "pending_human": [],
        "observation": build_serve_observation(run, status=status),
        "status": status,
    }

def build_serve_run_response(run: OpenCodeServeRun) -> dict[str, Any]:
    status: dict[str, Any] = {}
    if run.status_path.is_file():
        try:
            status = json.loads(run.status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            status = {}
    req = run.request_payload if isinstance(run.request_payload, dict) else {}
    meta = req.get("scillm_metadata") if isinstance(req.get("scillm_metadata"), dict) else {}
    dag_node_id = str(meta.get("case_id") or meta.get("item_id") or "")
    delivery = _delivery_state_from_status(status)
    child = {
        "subagent_run_id": run.run_id,
        "role": "patch",
        "agent": run.agent,
        "agent_id": run.agent,
        "subagent_kind": "opencode_serve",
        "subagent_label": "Serve child",
        "child_session_id": run.session_id,
        "delivery_state": delivery,
        "active": delivery in {"running", "queued", "posted", "delivered"},
    }
    return {
        "schema": "scillm.opencode_serve.transport_bridge.v1",
        "state": {
            "transport_run_id": run.run_id,
            "dag_node_id": dag_node_id or run.caller_skill,
            "parent_session_id": run.session_id,
            "workspace": run.directory,
            "children": [child],
        },
        "observation": build_serve_observation(run, status=status),
    }


def load_serve_run(run_id: str) -> OpenCodeServeRun:
    """Load oc-* run from active registry or artifact dir (for transport-room bridge)."""
    safe = serve_api._safe_id(run_id)
    active = serve_api._ACTIVE_RUNS.get(safe)
    if active is not None:
        return active
    status_path = serve_api._artifact_root() / safe / "status.json"
    if not status_path.is_file():
        raise ProxyError(404, f"opencode run not found: {safe}", "not_found")
    status = json.loads(status_path.read_text(encoding="utf-8"))
    session_id = str(status.get("session_id") or "")
    agent = str(status.get("agent") or serve_api.debugger_agent_name())
    restored = OpenCodeServeRun(
        run_id=safe,
        artifact_root=serve_api._artifact_root(),
        caller_skill=str(status.get("caller_skill") or "unknown"),
        agent=agent,
        session_id=session_id,
        request_payload={},
        directory=(
            status.get("directory")
            if isinstance(status.get("directory"), str)
            else status.get("cwd")
            if isinstance(status.get("cwd"), str)
            else None
        ),
    )
    request_path = restored.run_dir / "request.json"
    if request_path.is_file():
        try:
            restored.request_payload = json.loads(request_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            restored.request_payload = {}
    monitor = status.get("human_monitor")
    if monitor is None and restored.result_path.is_file():
        try:
            result = json.loads(restored.result_path.read_text(encoding="utf-8"))
            monitor = result.get("human_monitor") if isinstance(result, dict) else None
        except json.JSONDecodeError:
            monitor = None
    if isinstance(monitor, dict):
        restored.human_monitor = monitor
    return restored


def read_serve_events(
    run: OpenCodeServeRun, *, after_line: int = 0
) -> tuple[list[dict[str, Any]], int]:
    if not run.events_path.is_file():
        return [], after_line
    lines = run.events_path.read_text(errors="replace").splitlines()
    out: list[dict[str, Any]] = []
    status_payload: dict[str, Any] = {}
    if run.status_path.is_file():
        try:
            status_payload = json.loads(run.status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            status_payload = {}
    delivery_state = _delivery_state_from_status(status_payload)
    for offset, line in enumerate(lines):
        if offset < after_line:
            continue
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        event_name = str(row.get("event") or "event")
        out.append(
            {
                "schema": "scillm.opencode_transport.event.v1",
                "event_type": event_name,
                "transport_run_id": run.run_id,
                "subagent_run_id": run.run_id,
                "ts": row.get("ts"),
                "delivery_state": delivery_state,
                **{k: v for k, v in row.items() if k not in {"event"}},
            }
        )
    return out, len(lines)


def serve_stream_event_sse(row: dict[str, Any]) -> str:
    return f"data: {json.dumps(row, ensure_ascii=False)}\n\n"

async def post_serve_dialog_message(
    run: OpenCodeServeRun,
    *,
    speaker: str,
    body: str,
) -> dict[str, Any]:
    """Project-agent collaboration post mirrored into the serve child session."""
    trimmed = body.strip()
    if not trimmed:
        raise ProxyError(400, "dialog body is required", "invalid_request")
    message_id = f"{run.run_id}-dialog-{int(time.time() * 1000)}"
    collaborator = "project_agent"
    if speaker.strip().casefold().startswith("human"):
        collaborator = "human"
    turn = {
        "message_id": message_id,
        "collaborator": collaborator,
        "speaker": speaker.strip() or "Project agent",
        "text": format_dialog_message(speaker.strip() or "Project agent", trimmed),
        "role": "user",
        "subagent_run_id": run.run_id,
        "subagent_kind": "opencode_serve",
        "created_at": time.time(),
    }
    append_serve_dialog_turn(run, turn)
    status_payload: dict[str, Any] = {}
    if run.status_path.is_file():
        try:
            status_payload = json.loads(run.status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            status_payload = {}
    run_state = str(status_payload.get("state") or "").strip().lower()
    run_phase = str(status_payload.get("phase") or "").strip().lower()
    active_turn = run_state == "running" or run_phase in {"prompting", "delivering"}
    delivery_mode = "recorded_only"
    steering_payload: dict[str, Any] | None = None
    session_status: dict[str, Any] | None = None
    if run.session_id:
        from scillm.proxy.opencode_serve import OpenCodeServeClient, text_parts

        async with OpenCodeServeClient() as client:
            try:
                status_map = await client.session_status_map(directory=run.directory)
            except Exception:
                status_map = {}
            busy = session_is_busy(status_map, run.session_id)
            session_status = status_map.get(run.session_id) if isinstance(status_map, dict) else None
            no_reply = active_turn or busy
            steering_payload = await client.send_message(
                run.session_id,
                parts=text_parts(turn["text"]),
                no_reply=no_reply,
                directory=run.directory,
                agent=run.agent or None,
            )
            if no_reply:
                # The active turn cannot be interrupted; queue the turn so the
                # serve poll loop replays it as a real prompt when the session
                # goes idle (issue #13 — nudges must actually reach the child).
                queue_pending_dialog_turn(run, turn)
                delivery_mode = "queued_for_next_turn"
            else:
                delivery_mode = "steering_turn_sent"
    run.emit(
        "dialog.delivery",
        message_id=message_id,
        delivery_mode=delivery_mode,
        active_turn=active_turn,
        session_status=session_status,
    )
    return {
        "schema": "scillm.opencode_serve.dialog_post.v1",
        "turn": turn,
        "delivery_mode": delivery_mode,
        "steering_supported": delivery_mode == "steering_turn_sent",
        "active_turn_interrupt_supported": False,
        "active_turn": active_turn,
        "session_status": session_status,
        "opencode_message": steering_payload,
        "project_agent_message": (
            "Dialog post was appended as a side-channel note and QUEUED: it does not "
            "interrupt the in-flight model turn, but it will be replayed as a real "
            "prompt to the child the moment the current turn ends."
            if delivery_mode == "queued_for_next_turn"
            else "Dialog post was sent as a new OpenCode message because the child session was idle."
        ),
    }
