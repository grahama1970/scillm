"""HTTP client for a local ``opencode serve`` control plane.

scillm wraps this API; callers should use ``/v1/scillm/opencode/*`` instead of
hitting ``http://127.0.0.1:4096`` directly.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from scillm.proxy.errors import ProxyError
from scillm.proxy.providers.opencode_go import OPENCODE_SERVER_DEFAULT_URL

DEFAULT_DEBUGGER_AGENT = "scillm-debugger"


@dataclass(frozen=True)
class OpenCodeServeSettings:
    base_url: str
    username: str | None
    password: str | None
    timeout_s: float


def load_opencode_serve_settings() -> OpenCodeServeSettings:
    # Managed compose sidecar URL wins over .env OPENCODE_SERVER_URL (often host :4097).
    base = (
        os.environ.get("SCILLM_OPENCODE_SERVE_URL")
        or os.environ.get("OPENCODE_SERVER_URL")
        or OPENCODE_SERVER_DEFAULT_URL
    ).rstrip("/")
    password = os.environ.get("OPENCODE_SERVER_PASSWORD")
    username = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
    timeout_s = float(os.environ.get("SCILLM_OPENCODE_SERVE_TIMEOUT_S", "600"))
    return OpenCodeServeSettings(
        base_url=base,
        username=username if password else None,
        password=password,
        timeout_s=timeout_s,
    )



def load_debugger_system_prompt() -> str | None:
    """Load scillm-debugger instructions from the workspace agent markdown file."""
    candidates = [
        Path(os.environ.get("SCILLM_OPENCODE_DEBUGGER_PROMPT_FILE", "")).expanduser(),
        Path("/workspace/scillm/.opencode/agents/scillm-debugger.md"),
        Path("/home/graham/workspace/experiments/scillm/.opencode/agents/scillm-debugger.md"),
    ]
    for path in candidates:
        if not str(path) or not path.is_file():
            continue
        raw = path.read_text(encoding="utf-8")
        if raw.startswith("---"):
            end = raw.find("\n---", 3)
            body = raw[end + 4 :].lstrip() if end != -1 else raw
        else:
            body = raw
        body = body.strip()
        if body:
            return body
    return None


def debugger_runtime_agent(*, available_agents: list[str] | None = None) -> str:
    """Agent name sent to OpenCode for debugger runs (may differ from profile name)."""
    preferred = debugger_agent_name()
    runtime = os.environ.get("SCILLM_OPENCODE_DEBUGGER_RUNTIME_AGENT", "").strip()
    if runtime:
        return runtime
    names = set(available_agents or [])
    if preferred in names:
        return preferred
    for fallback in ("explore", "general", "plan", "build"):
        if fallback in names:
            return fallback
    return preferred


def debugger_agent_name() -> str:
    return os.environ.get("SCILLM_OPENCODE_DEBUGGER_AGENT", DEFAULT_DEBUGGER_AGENT).strip() or DEFAULT_DEBUGGER_AGENT


def text_parts(prompt: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": prompt}]


def extract_text_from_parts(parts: Any) -> str:
    if not isinstance(parts, list):
        return ""
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and part.get("text"):
            chunks.append(str(part["text"]))
    return "\n".join(chunks).strip()


def extract_assistant_text(message_payload: dict[str, Any]) -> str:
    parts = message_payload.get("parts")
    if isinstance(parts, list):
        text = extract_text_from_parts(parts)
        if text:
            return text
    info = message_payload.get("info")
    if isinstance(info, dict):
        for key in ("content", "text", "body"):
            value = info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""




def _parse_model_ref(model: str) -> tuple[str, str]:
    """Return ``(provider_id, model_id)`` for an OpenCode model reference."""
    raw = model.strip()
    if not raw:
        return "", ""
    if "/" in raw:
        provider_id, model_id = raw.split("/", 1)
        return provider_id.strip(), model_id.strip()
    lowered = raw.casefold()
    if lowered.startswith("gpt-") or lowered.startswith("codex-"):
        return "openai", raw
    if lowered.startswith("oc-"):
        return "opencode", raw
    return "opencode", raw


def _model_body_value(model: str | None) -> dict[str, str] | None:
    """OpenCode message API expects ``{providerID, modelID}`` or null (1.15.x)."""
    if not model:
        return None
    provider_id, model_id = _parse_model_ref(model)
    if not provider_id or not model_id:
        return None
    return {"providerID": provider_id, "modelID": model_id}


def _session_create_model(model: str | None) -> dict[str, str] | None:
    """OpenCode ``POST /session`` expects ``{id, providerID}`` (not modelID)."""
    if not model:
        return None
    provider_id, model_id = _parse_model_ref(model)
    if not provider_id or not model_id:
        return None
    return {"id": model_id, "providerID": provider_id}


def _directory_params(directory: str | None) -> dict[str, str] | None:
    """OpenCode serve scopes tool cwd via ``?directory=`` on session routes."""
    if not directory:
        return None
    return {"directory": directory}


class OpenCodeServeClient:
    """Thin async wrapper around OpenCode serve session/message APIs."""

    def __init__(self, settings: OpenCodeServeSettings | None = None) -> None:
        self.settings = settings or load_opencode_serve_settings()
        auth = None
        if self.settings.password:
            auth = (self.settings.username or "opencode", self.settings.password)

        self._client = httpx.AsyncClient(
            base_url=self.settings.base_url,
            auth=auth,
            timeout=httpx.Timeout(self.settings.timeout_s, connect=5.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> OpenCodeServeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> Any:
        try:
            resp = await self._client.request(
                method,
                path,
                json=json_body,
                params=params,
                timeout=timeout_s or self.settings.timeout_s,
            )
        except httpx.ConnectError as exc:
            raise ProxyError(
                503,
                f"opencode serve unreachable at {self.settings.base_url}: {exc}",
                "service_unavailable",
                advice="Start opencode-serve (docker compose) or opencode serve on :4097; set OPENCODE_SERVER_URL if needed.",
            ) from exc
        except httpx.TimeoutException as exc:
            raise ProxyError(
                504,
                f"opencode serve timed out calling {path}",
                "timeout",
            ) from exc

        if resp.status_code == 401:
            raise ProxyError(
                502,
                "opencode serve rejected credentials",
                "provider_auth_failed",
                advice="Set OPENCODE_SERVER_PASSWORD (and OPENCODE_SERVER_USERNAME if not opencode).",
            )
        if resp.status_code >= 400:
            detail = resp.text[:2000]
            raise ProxyError(
                502,
                f"opencode serve {method} {path} failed: HTTP {resp.status_code}: {detail}",
                "provider_error",
            )

        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    async def health(self) -> dict[str, Any]:
        data = await self._request("GET", "/global/health", timeout_s=5.0)
        return data if isinstance(data, dict) else {"raw": data}

    async def list_agents(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/agent", timeout_s=10.0)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    async def create_session(
        self,
        *,
        title: str | None = None,
        parent_id: str | None = None,
        directory: str | None = None,
        model: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if title:
            body["title"] = title
        if parent_id:
            body["parentID"] = parent_id
        session_model = _session_create_model(model)
        if session_model is not None:
            body["model"] = session_model
        if agent:
            body["agent"] = agent.strip()
        params: dict[str, Any] | None = None
        if directory:
            params = {"directory": directory}
        data = await self._request("POST", "/session", json_body=body or None, params=params)
        if not isinstance(data, dict):
            raise ProxyError(502, "opencode serve returned invalid session payload", "provider_error")
        return data

    async def get_session(self, session_id: str) -> dict[str, Any]:
        data = await self._request("GET", f"/session/{session_id}")
        if not isinstance(data, dict):
            raise ProxyError(502, "opencode serve returned invalid session payload", "provider_error")
        return data

    async def session_status_map(self, *, directory: str | None = None) -> dict[str, Any]:
        data = await self._request(
            "GET",
            "/session/status",
            params=_directory_params(directory),
            timeout_s=10.0,
        )
        return data if isinstance(data, dict) else {}

    async def send_message(
        self,
        session_id: str,
        *,
        agent: str | None = None,
        model: str | None = None,
        parts: list[dict[str, Any]],
        system: str | None = None,
        no_reply: bool = False,
        directory: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"parts": parts}
        if agent:
            body["agent"] = agent
        model_value = _model_body_value(model)
        if model_value is not None:
            body["model"] = model_value
        if system:
            body["system"] = system
        if no_reply:
            body["noReply"] = True
        data = await self._request(
            "POST",
            f"/session/{session_id}/message",
            json_body=body,
            params=_directory_params(directory),
        )
        if not isinstance(data, dict):
            raise ProxyError(502, "opencode serve returned invalid message payload", "provider_error")
        return data

    async def send_prompt_async(
        self,
        session_id: str,
        *,
        agent: str | None = None,
        model: str | None = None,
        parts: list[dict[str, Any]],
        system: str | None = None,
        directory: str | None = None,
    ) -> None:
        body: dict[str, Any] = {"parts": parts}
        if agent:
            body["agent"] = agent
        model_value = _model_body_value(model)
        if model_value is not None:
            body["model"] = model_value
        if system:
            body["system"] = system
        await self._request(
            "POST",
            f"/session/{session_id}/prompt_async",
            json_body=body,
            params=_directory_params(directory),
        )

    async def list_messages(
        self,
        session_id: str,
        *,
        limit: int | None = None,
        directory: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if directory:
            params["directory"] = directory
        data = await self._request(
            "GET",
            f"/session/{session_id}/message",
            params=params or None,
        )
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    async def list_permissions(self, *, directory: str | None = None) -> list[dict[str, Any]]:
        data = await self._request(
            "GET",
            "/permission",
            params=_directory_params(directory),
            timeout_s=10.0,
        )
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    async def abort(self, session_id: str, *, directory: str | None = None) -> bool:
        data = await self._request(
            "POST",
            f"/session/{session_id}/abort",
            params=_directory_params(directory),
        )
        return bool(data)

    async def reply_permission(
        self,
        session_id: str,
        *,
        permission_id: str,
        response: str,
        directory: str | None = None,
    ) -> bool:
        if response not in {"once", "always", "reject"}:
            raise ProxyError(400, f"unsupported permission response: {response}", "invalid_request")
        data = await self._request(
            "POST",
            f"/session/{session_id}/permissions/{permission_id}",
            json_body={"response": response},
            params=_directory_params(directory),
        )
        return bool(data)

    async def delete_session(self, session_id: str) -> bool:
        data = await self._request("DELETE", f"/session/{session_id}")
        return bool(data)

    async def fork_session(
        self,
        session_id: str,
        *,
        message_id: str | None = None,
        directory: str | None = None,
    ) -> dict[str, Any]:
        """Fork an existing session at an optional message boundary."""
        body: dict[str, Any] = {}
        if message_id:
            body["messageID"] = message_id
        params: dict[str, Any] | None = None
        if directory:
            params = {"directory": directory}
        data = await self._request(
            "POST",
            f"/session/{session_id}/fork",
            json_body=body or None,
            params=params,
        )
        if not isinstance(data, dict):
            raise ProxyError(502, "opencode serve returned invalid fork payload", "provider_error")
        return data

    async def list_session_children(self, session_id: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/session/{session_id}/children")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    async def summarize(self, session_id: str, *, provider_id: str, model_id: str) -> bool:
        data = await self._request(
            "POST",
            f"/session/{session_id}/summarize",
            json_body={"providerID": provider_id, "modelID": model_id},
        )
        return bool(data)

    async def revert(
        self,
        session_id: str,
        *,
        message_id: str,
        part_id: str | None = None,
    ) -> bool:
        body: dict[str, Any] = {"messageID": message_id}
        if part_id:
            body["partID"] = part_id
        data = await self._request(
            "POST",
            f"/session/{session_id}/revert",
            json_body=body,
        )
        return bool(data)

    async def unrevert(self, session_id: str) -> bool:
        data = await self._request("POST", f"/session/{session_id}/unrevert")
        return bool(data)

    async def iter_event_stream(self) -> AsyncIterator[bytes]:
        """Stream raw SSE bytes from OpenCode ``GET /event``."""
        stream_timeout = httpx.Timeout(connect=5.0, read=None, write=30.0, pool=30.0)
        try:
            async with self._client.stream("GET", "/event", timeout=stream_timeout) as resp:
                if resp.status_code == 401:
                    raise ProxyError(
                        502,
                        "opencode serve rejected credentials",
                        "provider_auth_failed",
                        advice="Set OPENCODE_SERVER_PASSWORD (and OPENCODE_SERVER_USERNAME if not opencode).",
                    )
                if resp.status_code >= 400:
                    detail = (await resp.aread())[:2000].decode("utf-8", errors="replace")
                    raise ProxyError(
                        502,
                        f"opencode serve GET /event failed: HTTP {resp.status_code}: {detail}",
                        "provider_error",
                    )
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        yield chunk
        except httpx.ConnectError as exc:
            raise ProxyError(
                503,
                f"opencode serve unreachable at {self.settings.base_url}: {exc}",
                "service_unavailable",
                advice="Start opencode serve and set OPENCODE_SERVER_URL if needed.",
            ) from exc
        except httpx.TimeoutException as exc:
            raise ProxyError(
                504,
                "opencode serve timed out opening /event stream",
                "timeout",
            ) from exc

    async def diff(
        self,
        session_id: str,
        *,
        message_id: str | None = None,
        directory: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if message_id:
            params["messageID"] = message_id
        if directory:
            params["directory"] = directory
        data = await self._request("GET", f"/session/{session_id}/diff", params=params or None)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    async def list_sessions(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/session", timeout_s=30.0)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []


    async def register_mcp(self, name: str) -> dict[str, Any]:
        """Register an MCP server by name if OpenCode serve exposes /mcp."""
        data = await self._request("POST", "/mcp", json_body={"name": name})
        return data if isinstance(data, dict) else {"name": name, "ok": bool(data)}

    async def kill_session(self, session_id: str, *, abort_first: bool = True) -> dict[str, Any]:
        """Abort (best-effort) then delete an OpenCode session."""
        aborted = False
        if abort_first:
            try:
                aborted = await self.abort(session_id)
            except ProxyError:
                aborted = False
        deleted = await self.delete_session(session_id)
        return {"session_id": session_id, "aborted": aborted, "deleted": deleted}


def session_id_from_payload(session_payload: dict[str, Any]) -> str | None:
    """Return OpenCode session id from a session list/detail payload."""
    for key in ("id", "sessionID", "sessionId"):
        value = session_payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def session_epoch_s(session_payload: dict[str, Any]) -> float | None:
    """Best-effort session age anchor (updated/created) as unix seconds."""

    def _coerce(value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 1e12:
                ts /= 1000.0
            return ts
        if isinstance(value, str) and value.strip():
            text = value.strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                from datetime import datetime

                return datetime.fromisoformat(text).timestamp()
            except ValueError:
                return None
        return None

    for key in ("updated", "created", "started", "timeUpdated", "timeCreated"):
        ts = _coerce(session_payload.get(key))
        if ts is not None:
            return ts
    nested = session_payload.get("time")
    if isinstance(nested, dict):
        for key in ("updated", "created", "started"):
            ts = _coerce(nested.get(key))
            if ts is not None:
                return ts
    return None


def session_is_busy(status_map: dict[str, Any], session_id: str) -> bool:
    entry = status_map.get(session_id)
    if isinstance(entry, dict):
        for key in ("status", "state", "phase"):
            value = entry.get(key)
            if isinstance(value, str):
                lowered = value.lower()
                if lowered in {"busy", "running", "working", "in_progress", "pending"}:
                    return True
                if lowered in {"idle", "completed", "done", "stopped", "aborted"}:
                    return False
        if entry.get("busy") is True:
            return True
        if entry.get("busy") is False:
            return False
    if isinstance(entry, str):
        return entry.lower() in {"busy", "running", "working", "in_progress", "pending"}
    return False


def classify_zombie_session(
    session_payload: dict[str, Any],
    status_map: dict[str, Any],
    *,
    protected_ids: set[str],
    stale_busy_s: float,
    max_idle_age_s: float,
    kill_idle: bool,
    now_epoch: float | None = None,
) -> str | None:
    """Return a purge reason string, or None if the session should be kept."""
    session_id = session_id_from_payload(session_payload)
    if not session_id:
        return None
    if session_id in protected_ids:
        return None

    now = now_epoch if now_epoch is not None else time.time()
    busy = session_is_busy(status_map, session_id)
    anchor = session_epoch_s(session_payload)
    age_s = (now - anchor) if anchor is not None else None

    if busy:
        if age_s is None or age_s >= stale_busy_s:
            return "stuck_busy"
        return None

    if kill_idle and age_s is not None and age_s >= max_idle_age_s:
        return "stale_idle"
    if age_s is not None and age_s >= max_idle_age_s:
        return "stale_orphan"
    return None
