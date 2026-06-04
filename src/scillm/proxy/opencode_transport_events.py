"""Normalize OpenCode ``GET /event`` bus events for transport monitors."""

from __future__ import annotations

import json
from typing import Any

TRANSPORT_EVENT_SCHEMA = "scillm.opencode_transport.event.v1"

_OPENCODE_LIVENESS_TYPES = frozenset(
    {
        "message.part.updated",
        "message.updated",
        "session.idle",
        "session.error",
        "permission.asked",
        "question.asked",
    }
)


def parse_sse_json_payload(raw: str) -> dict[str, Any] | None:
    """Parse one SSE ``data:`` line into an OpenCode bus event object."""
    text = raw.strip()
    if not text or text == "[DONE]":
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _part_dict(event: dict[str, Any]) -> dict[str, Any] | None:
    props = event.get("properties")
    if not isinstance(props, dict):
        return None
    part = props.get("part")
    return part if isinstance(part, dict) else None


def _event_session_id(event: dict[str, Any], part: dict[str, Any] | None = None) -> str:
    if part:
        for key in ("sessionID", "sessionId", "session_id"):
            value = part.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    props = event.get("properties")
    if isinstance(props, dict):
        info = props.get("info")
        if isinstance(info, dict):
            for key in ("sessionID", "sessionId", "session_id", "id"):
                value = info.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    for key in ("sessionID", "sessionId", "session_id"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _event_message_id(event: dict[str, Any], part: dict[str, Any] | None = None) -> str:
    if part:
        for key in ("messageID", "messageId", "message_id"):
            value = part.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    props = event.get("properties")
    if isinstance(props, dict):
        info = props.get("info")
        if isinstance(info, dict):
            for key in ("messageID", "messageId", "message_id", "id"):
                value = info.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def opencode_event_is_liveness(opencode_type: str) -> bool:
    return opencode_type in _OPENCODE_LIVENESS_TYPES


def opencode_event_is_terminal(opencode_type: str) -> bool:
    return opencode_type in {"session.idle", "session.error"}


def normalize_opencode_bus_event(
    event: dict[str, Any],
    *,
    watch_session_ids: frozenset[str] | set[str],
) -> dict[str, Any] | None:
    """Map a raw OpenCode bus event to a scillm transport monitor event."""
    opencode_type = str(event.get("type") or "").strip()
    if not opencode_type:
        return None

    part = _part_dict(event)
    session_id = _event_session_id(event, part)
    if watch_session_ids and (not session_id or session_id not in watch_session_ids):
        return None

    props = event.get("properties") if isinstance(event.get("properties"), dict) else {}
    message_id = _event_message_id(event, part)
    base: dict[str, Any] = {
        "schema": TRANSPORT_EVENT_SCHEMA,
        "opencode_type": opencode_type,
        "session_id": session_id,
        "message_id": message_id,
        "liveness": opencode_event_is_liveness(opencode_type),
        "terminal": opencode_event_is_terminal(opencode_type),
    }

    if opencode_type == "message.part.updated" and part:
        part_type = str(part.get("type") or "")
        delta = props.get("delta")
        delta_text = str(delta) if isinstance(delta, str) else ""
        text = str(part.get("text") or "")
        base["part_id"] = str(part.get("id") or "")
        base["part_type"] = part_type

        if part_type == "reasoning":
            base["event_type"] = "reasoning_delta"
            base["delta"] = delta_text
            base["text"] = text
            base["severity"] = "info"
            return base

        if part_type == "text":
            if part.get("synthetic") or part.get("ignored"):
                return None
            base["event_type"] = "transcript_delta"
            base["delta"] = delta_text
            base["text"] = text
            base["severity"] = "info"
            return base

        if part_type == "tool":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            status = str(state.get("status") or "")
            base["event_type"] = "tool_call"
            base["tool"] = str(part.get("tool") or "")
            base["status"] = status
            base["severity"] = "warn" if status in {"error", "failed"} else "info"
            if status == "completed":
                output = state.get("output")
                if output is not None:
                    base["output_excerpt"] = str(output)[:2000]
            return base

        if part_type in {"step-start", "step-finish"}:
            base["event_type"] = "step"
            base["step"] = part_type
            base["severity"] = "info"
            return base

        base["event_type"] = "part_updated"
        base["severity"] = "info"
        return base

    if opencode_type == "permission.asked":
        base["event_type"] = "permission_requested"
        base["needs_attention"] = True
        base["severity"] = "warn"
        base["permission"] = props
        permission_id = props.get("id") or props.get("permissionID") or props.get("requestID")
        if permission_id:
            base["permission_id"] = str(permission_id)
        return base

    if opencode_type == "question.asked":
        base["event_type"] = "question_requested"
        base["needs_attention"] = True
        base["severity"] = "warn"
        base["question"] = props
        return base

    if opencode_type == "session.error":
        base["event_type"] = "session_error"
        base["severity"] = "error"
        base["error"] = props.get("error") if isinstance(props.get("error"), dict) else props
        return base

    if opencode_type == "session.idle":
        base["event_type"] = "session_idle"
        base["severity"] = "info"
        return base

    if opencode_type == "message.updated":
        base["event_type"] = "message_updated"
        base["severity"] = "info"
        return base

    if opencode_type == "server.connected":
        return None

    base["event_type"] = "opencode_bus"
    base["severity"] = "info"
    base["properties"] = props
    return base


def merge_reasoning_excerpt(existing: str, event: dict[str, Any]) -> str:
    """Append reasoning delta/text for project-agent problem diagnosis."""
    delta = str(event.get("delta") or "")
    text = str(event.get("text") or "")
    if delta:
        return (existing + delta)[-8000:]
    if text and (not existing or len(text) >= len(existing)):
        return text[-8000:]
    return existing
