"""scillm HTTP API for OpenCode ``serve`` session workers.

This is Tier-2 orchestration over ``opencode serve`` (default :4096).  It is
distinct from:

- ``/v1/chat/completions`` with ``opencode-go/*`` (one-shot HTTP models)
- ``/v1/scillm/exec`` with ``opencode_exec`` (``opencode run`` one-shot CLI)
- ``/v1/scillm/agents/*`` (Codex app-server standing workers)

Callers pass an OpenCode **agent profile** name (for example ``scillm-debugger``)
in the JSON body.  scillm creates a fresh OpenCode session per run, sends the
prompt, persists artifacts, and returns a normalized result envelope.
"""

from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.responses import StreamingResponse
import html as html_module
import secrets
from pydantic import BaseModel, Field

from scillm.proxy.errors import ProxyError
from scillm.proxy.streaming import SSE_HEADERS, sse_liveness_wrapper
from scillm.harness.patch_delegate_receipt import (
  PATCH_DELEGATE_BLOCKED,
  classify_patch_delegate_result,
)
from scillm.proxy.opencode_skill_view import (
  SkillViewReceipt,
  build_skills_system_overlay,
  cleanup_skill_view,
  materialize_skill_view,
  merge_system_prompt,
)
from scillm.proxy.opencode_serve_runtime import (
    inspect_opencode_serve_runtime,
    restart_opencode_serve_runtime,
)
from scillm.proxy.opencode_transport_api import register_opencode_transport_routes
from scillm.proxy.opencode_serve import (
    OpenCodeServeClient,
    classify_zombie_session,
    debugger_agent_name,
    debugger_runtime_agent,
    load_debugger_system_prompt,
    extract_assistant_text,
    load_opencode_serve_settings,
    session_epoch_s,
    session_id_from_payload,
    session_is_busy,
    text_parts,
)

AuthCheck = Callable[[Request], str | None]

DEFAULT_OPENCODE_AGENT_NAMES = [
  "build",
  "compaction",
  "explore",
  "general",
  "plan",
  "summary",
  "title",
]

_ACTIVE_RUNS: dict[str, "OpenCodeServeRun"] = {}
_ACTIVE_LOCK = asyncio.Lock()
_SNAPSHOT_MAX_FILES = 2_000
_SNAPSHOT_MAX_BYTES = 512_000
_SNAPSHOT_IGNORED_DIRS = {
  ".git",
  ".hg",
  ".svn",
  "__pycache__",
  ".pytest_cache",
  ".mypy_cache",
  ".ruff_cache",
  ".venv",
  "venv",
  "node_modules",
}


class OpenCodeRunRequest(BaseModel):
  """Start one bounded OpenCode serve attempt."""

  prompt: str = Field(min_length=1)
  agent: str | None = None
  model: str | None = None
  system: str | None = None
  title: str | None = None
  cwd: str | None = None
  run_id: str | None = None
  wait: bool = True
  timeout_s: float | None = Field(default=None, ge=10.0, le=3600.0)
  patch_mode: str | None = None
  batch_id: str | None = None
  case_id: str | None = None
  page_number: int | None = None
  candidate_id: str | None = None
  scillm_metadata: dict[str, Any] = Field(default_factory=dict)
  parts: list[dict[str, Any]] | None = None
  skills: list[str] = Field(default_factory=list)
  mcp: list[str] = Field(default_factory=list)
  cleanup_session: bool = True
  cleanup_skill_view: bool = True
  fork_from_session_id: str | None = None
  fork_at_message_id: str | None = None


class OpenCodeForkRequest(BaseModel):
  """Fork an OpenCode serve session at an optional message boundary."""

  message_id: str | None = None
  title: str | None = None


class OpenCodeSummarizeRequest(BaseModel):
  """Summarize an OpenCode session (compacts context on the serve instance)."""

  provider_id: str
  model_id: str


class OpenCodeRevertRequest(BaseModel):
  """Revert a message (and optional part) inside a session."""

  message_id: str
  part_id: str | None = None


class OpenCodeSessionPurgeRequest(BaseModel):
  """Purge stale/zombie OpenCode serve sessions (abort + delete)."""

  dry_run: bool = True
  force: bool = False
  stale_busy_s: float = Field(default=600.0, ge=30.0, le=86400.0)
  max_idle_age_s: float = Field(default=86400.0, ge=300.0, le=604800.0)
  kill_idle: bool = False
  session_ids: list[str] | None = None


class OpenCodeServeRun:
  """Artifacted state for one scillm-managed OpenCode session run."""

  def __init__(
    self,
    *,
    run_id: str,
    artifact_root: Path,
    caller_skill: str,
    agent: str,
    session_id: str,
    request_payload: dict[str, Any],
    directory: str | None = None,
  ) -> None:
    self.run_id = _safe_id(run_id)
    self.agent = agent
    self.session_id = session_id
    self.caller_skill = caller_skill
    self.request_payload = request_payload
    self.directory = directory
    self.run_dir = artifact_root / self.run_id
    self.events_path = self.run_dir / "events.jsonl"
    self.status_path = self.run_dir / "status.json"
    self.result_path = self.run_dir / "opencode_result.json"
    self.human_monitor: dict[str, Any] | None = None
    self.run_dir.mkdir(parents=True, exist_ok=True)
    request_path = self.run_dir / "request.json"
    if request_payload or not request_path.exists():
      request_path.write_text(json.dumps(request_payload, indent=2), encoding="utf-8")

  def emit(self, event: str, **fields: Any) -> None:
    row = {"ts": _now(), "event": event, "run_id": self.run_id, "session_id": self.session_id, **fields}
    with self.events_path.open("a", encoding="utf-8") as handle:
      handle.write(json.dumps(row, ensure_ascii=False) + "\n")

  def write_status(self, **fields: Any) -> dict[str, Any]:
    payload = {
      "schema": "scillm.opencode_run.status.v1",
      "run_id": self.run_id,
      "agent": self.agent,
      "session_id": self.session_id,
      "caller_skill": self.caller_skill,
      "updated_at": _now(),
      **fields,
    }
    if self.human_monitor is not None and "human_monitor" not in payload:
      payload["human_monitor"] = self.human_monitor
    self.status_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload

  def write_result(self, result: dict[str, Any]) -> dict[str, Any]:
    self.result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result

  def artifact_summary(self) -> dict[str, str]:
    out: dict[str, str] = {
      "run_dir": str(self.run_dir),
      "events_jsonl": str(self.events_path),
      "status_json": str(self.status_path),
      "opencode_result_json": str(self.result_path),
      "request_json": str(self.run_dir / "request.json"),
    }
    messages_path = self.run_dir / "messages_snapshot.json"
    if messages_path.exists():
      out["messages_snapshot_json"] = str(messages_path)
    host_root = _artifact_host_root()
    if host_root is not None:
      host_run = host_root / self.run_id
      out["host_run_dir"] = str(host_run)
      out["host_events_jsonl"] = str(host_run / "events.jsonl")
      out["host_status_json"] = str(host_run / "status.json")
      out["host_opencode_result_json"] = str(host_run / "opencode_result.json")
    return out

def _artifact_host_root() -> Path | None:
  raw = os.environ.get("SCILLM_OPENCODE_SERVE_OUTPUT_HOST_DIR", "").strip()
  if not raw:
    return None
  path = Path(raw).expanduser()
  path.mkdir(parents=True, exist_ok=True)
  return path


def _artifact_root() -> Path:
  root = Path(
    os.environ.get(
      "SCILLM_OPENCODE_SERVE_OUTPUT_DIR",
      os.path.join(os.environ.get("SCILLM_EXEC_OUTPUT_DIR", "/tmp/scillm-exec"), "opencode-serve"),
    )
  ).expanduser()
  root.mkdir(parents=True, exist_ok=True)
  return root


def _workspace_route_token(workspace_path: str | None) -> str | None:
  if not workspace_path:
    return None
  return base64.b64encode(str(workspace_path).encode("utf-8")).decode("ascii").rstrip("=")


def _scillm_public_base_url(request: Request | None) -> str:
  configured = os.environ.get("SCILLM_PUBLIC_BASE_URL", "").strip() or os.environ.get("SCILLM_API_BASE", "").strip()
  if configured:
    return configured.rstrip("/")
  if request is not None:
    return str(request.base_url).rstrip("/")
  return "http://localhost:4001"


def build_human_monitor(
  *,
  run: OpenCodeServeRun,
  scillm_base_url: str,
  opencode_settings: Any,
  session_title: str | None = None,
  session_slug: str | None = None,
) -> dict[str, Any]:
  """Human/API URL split for scillm-managed OpenCode serve runs."""
  opencode_base = str(getattr(opencode_settings, "base_url", "") or "http://127.0.0.1:4098").rstrip("/")
  scillm_base = scillm_base_url.rstrip("/")
  session = str(run.session_id or "").strip()
  monitor_token = secrets.token_urlsafe(24)
  title = (session_title or run.request_payload.get("title") or "").strip() or None
  slug = (session_slug or "").strip() or None
  monitor: dict[str, Any] = {
    "schema": "scillm.opencode_run.human_monitor.v1",
    "run_id": run.run_id,
    "opencode_base_url": opencode_base,
    "scillm_base_url": scillm_base,
    "session_id": session or None,
    "session_title": title,
    "session_slug": slug,
    "workspace_path": run.directory,
    "monitor_token": monitor_token,
    "auth": {
      "type": "basic",
      "username": os.environ.get("OPENCODE_SERVER_USERNAME", "opencode"),
      "password_env": "OPENCODE_SERVER_PASSWORD",
    },
    "human_instruction": (
      "Open scillm_chat_monitor_url in a browser to watch the live child chat. "
      "OpenCode serve workspace/session SPA routes often stay blank for API-created sessions."
    ),
    "known_bad_urls": [
      "/session/<session_id> is a JSON API endpoint, not a browser UI route.",
      "/<workspace-token>/session/<session_id> may crash the OpenCode SPA in current serve builds.",
      "opencode_workspace_url alone usually does not show the active API-created session chat.",
    ],
    "scillm_run_url": f"{scillm_base}/v1/scillm/opencode/runs/{run.run_id}",
    "scillm_status_url": f"{scillm_base}/v1/scillm/opencode/runs/{run.run_id}/status",
    "scillm_events_url": f"{scillm_base}/v1/scillm/opencode/runs/{run.run_id}/events?tail=200",
    "scillm_diff_url": f"{scillm_base}/v1/scillm/opencode/runs/{run.run_id}/diff",
    "scillm_chat_monitor_url": (
      f"{scillm_base}/v1/scillm/opencode/runs/{run.run_id}/monitor?token={monitor_token}"
    ),
    "scillm_chat_monitor_reliability": "supported",
    "opencode_workspace_monitor_reliability": "unreliable_for_api_sessions",
  }
  route_token = _workspace_route_token(run.directory)
  if route_token:
    monitor["opencode_workspace_url"] = f"{opencode_base}/{route_token}"
  if session:
    monitor["opencode_session_api_url"] = f"{opencode_base}/session/{session}"
    monitor["opencode_messages_api_url"] = f"{opencode_base}/session/{session}/message"
    monitor["opencode_diff_api_url"] = f"{opencode_base}/session/{session}/diff"
    monitor["opencode_abort_api_url"] = f"{opencode_base}/session/{session}/abort"
    if route_token:
      monitor["opencode_chat_url_unsafe"] = f"{opencode_base}/{route_token}/session/{session}"
      monitor["opencode_chat_monitor_reliability"] = "broken_in_serve_1.15"
  if slug and title:
    monitor["opencode_session_picker_hint"] = (
      f"In OpenCode workspace UI, select session titled {title!r} (slug {slug!r})."
    )
  elif title:
    monitor["opencode_session_picker_hint"] = f"In OpenCode workspace UI, select session titled {title!r}."
  chat_monitor_url = monitor["scillm_chat_monitor_url"]
  monitor["human_monitor_url"] = chat_monitor_url
  return monitor


def _monitor_token_from_run(run: OpenCodeServeRun) -> str | None:
  monitor = run.human_monitor
  if isinstance(monitor, dict):
    token = monitor.get("monitor_token")
    if isinstance(token, str) and token.strip():
      return token.strip()
  if run.status_path.exists():
    try:
      status = json.loads(run.status_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
      return None
    nested = status.get("human_monitor")
    if isinstance(nested, dict):
      token = nested.get("monitor_token")
      if isinstance(token, str) and token.strip():
        return token.strip()
  return None


def _monitor_auth_ok(request: Request, run: OpenCodeServeRun, query_token: str | None) -> bool:
  expected = _monitor_token_from_run(run)
  if not expected or not query_token:
    return False
  return secrets.compare_digest(expected, query_token.strip())


def _part_html(part: dict[str, Any]) -> str:
  part_type = str(part.get("type") or "unknown")
  if part_type == "text":
    body = html_module.escape(str(part.get("text") or ""))
    return f'<pre class="part-text">{body}</pre>'
  if part_type in {"tool", "tool-call", "tool_call", "tool-invocation"}:
    name = html_module.escape(str(part.get("tool") or part.get("name") or part.get("toolName") or "tool"))
    state = html_module.escape(str(part.get("state") or part.get("status") or ""))
    detail = part.get("input") or part.get("arguments") or part.get("output") or part.get("result")
    detail_html = ""
    if detail is not None:
      detail_text = detail if isinstance(detail, str) else json.dumps(detail, indent=2, ensure_ascii=False)
      detail_html = f'<pre class="part-tool-detail">{html_module.escape(detail_text)}</pre>'
    state_html = f' <span class="part-tool-state">({state})</span>' if state else ""
    return f'<div class="part-tool"><strong>{name}</strong>{state_html}{detail_html}</div>'
  if part_type == "reasoning":
    body = html_module.escape(str(part.get("text") or part.get("reasoning") or ""))
    return f'<pre class="part-reasoning">{body}</pre>'
  fallback = html_module.escape(json.dumps(part, indent=2, ensure_ascii=False))
  return f'<pre class="part-unknown">{fallback}</pre>'


def _message_html(message: dict[str, Any]) -> str:
  info = message.get("info") if isinstance(message.get("info"), dict) else {}
  role = html_module.escape(str(info.get("role") or "unknown"))
  agent = info.get("agent")
  model = info.get("model")
  meta_bits: list[str] = []
  if agent:
    meta_bits.append(html_module.escape(str(agent)))
  if model:
    meta_bits.append(html_module.escape(str(model)))
  meta = f' <span class="msg-meta">{" · ".join(meta_bits)}</span>' if meta_bits else ""
  parts = message.get("parts")
  part_blocks = "".join(_part_html(part) for part in parts if isinstance(part, dict)) if isinstance(parts, list) else ""
  if not part_blocks:
    text = extract_assistant_text(message)
    if text:
      part_blocks = f'<pre class="part-text">{html_module.escape(text)}</pre>'
  return f'<article class="message message-{role}"><header><span class="msg-role">{role}</span>{meta}</header>{part_blocks}</article>'


def _render_chat_monitor_page(
  *,
  run: OpenCodeServeRun,
  messages: list[dict[str, Any]],
  status: dict[str, Any],
  refresh_s: int,
) -> str:
  monitor = run.human_monitor if isinstance(run.human_monitor, dict) else {}
  title = html_module.escape(str(monitor.get("session_title") or run.run_id))
  session_id = html_module.escape(str(run.session_id or ""))
  phase = html_module.escape(str(status.get("phase") or status.get("state") or "unknown"))
  workspace = html_module.escape(str(run.directory or ""))
  timeout_banner = ""
  terminal_blocker = status.get("terminal_blocker")
  if isinstance(terminal_blocker, dict):
    reason = html_module.escape(str(terminal_blocker.get("primary_reason") or "timeout"))
    excerpt = html_module.escape(str(terminal_blocker.get("last_assistant_excerpt") or "")[:500])
    timeout_banner = (
      f'<p class="timeout-banner">Timed out · {reason}'
      + (f"<br/><span>{excerpt}</span>" if excerpt else "")
      + "</p>"
    )
  message_blocks = "".join(_message_html(msg) for msg in messages if isinstance(msg, dict))
  if not message_blocks:
    message_blocks = '<p class="empty">No messages yet. This page auto-refreshes while the run is active.</p>'
  refresh = max(2, min(refresh_s, 30))
  events_url = html_module.escape(str(monitor.get("scillm_events_url") or ""))
  return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta http-equiv="refresh" content="{refresh}" />
  <title>scillm OpenCode monitor · {title}</title>
  <style>
    body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 1rem 1.25rem; background: #0b1020; color: #e8ecff; }}
    h1 {{ font-size: 1.1rem; margin: 0 0 0.25rem; }}
    .meta {{ color: #9aa7d7; font-size: 0.9rem; margin-bottom: 1rem; }}
    .message {{ border: 1px solid #243056; border-radius: 8px; padding: 0.75rem; margin: 0 0 0.75rem; background: #121933; }}
    .message-user {{ border-color: #3a4f9f; }}
    .message-assistant {{ border-color: #2f6f4f; }}
    header {{ font-weight: 600; margin-bottom: 0.5rem; }}
    .msg-meta {{ color: #9aa7d7; font-weight: 400; }}
    pre {{ white-space: pre-wrap; word-break: break-word; margin: 0.35rem 0 0; font-size: 0.9rem; }}
    .part-tool {{ background: #0d152e; border-radius: 6px; padding: 0.5rem; margin-top: 0.35rem; }}
    .part-tool-state {{ color: #9aa7d7; }}
    .empty {{ color: #9aa7d7; }}
    .timeout-banner {{ background: #3a2418; border: 1px solid #8a4b2c; padding: 0.75rem; border-radius: 8px; margin-bottom: 1rem; }}
    footer {{ margin-top: 1rem; color: #9aa7d7; font-size: 0.85rem; }}
    a {{ color: #8cb4ff; }}
  </style>
</head>
<body>
  <h1>OpenCode child chat · {title}</h1>
  <p class="meta">run <code>{html_module.escape(run.run_id)}</code> · session <code>{session_id}</code> · phase {phase}<br/>workspace <code>{workspace}</code></p>
  {timeout_banner}
  <section id="messages">{message_blocks}</section>
  <footer>Auto-refresh every {refresh}s. <a href="{events_url}">events JSON</a></footer>
</body>
</html>"""


async def _fetch_live_messages(run: OpenCodeServeRun, *, limit: int = 200) -> list[dict[str, Any]]:
  if not run.session_id:
    return []
  settings = load_opencode_serve_settings()
  async with OpenCodeServeClient(settings) as client:
    messages = await client.list_messages(run.session_id, limit=limit, directory=run.directory)
  return messages if isinstance(messages, list) else []


def _messages_from_snapshot(run: OpenCodeServeRun) -> list[dict[str, Any]]:
  snapshot_path = run.run_dir / "messages_snapshot.json"
  if not snapshot_path.exists():
    return []
  try:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
  except json.JSONDecodeError:
    return []
  messages = snapshot.get("messages")
  return messages if isinstance(messages, list) else []


async def _load_run_messages_for_monitor(run: OpenCodeServeRun, *, limit: int = 200) -> list[dict[str, Any]]:
  """Prefer live OpenCode messages for active runs; fall back to persisted snapshot."""
  if run.run_id in _ACTIVE_RUNS:
    try:
      messages = await _fetch_live_messages(run, limit=limit)
      if messages:
        return messages
    except Exception:
      pass
  snapshot_messages = _messages_from_snapshot(run)
  if snapshot_messages:
    return snapshot_messages[-limit:]
  if run.session_id:
    return await _fetch_live_messages(run, limit=limit)
  return []


def _enrich_run_response(payload: dict[str, Any]) -> dict[str, Any]:
  """Add top-level human_monitor_url alias for project-agent copy/paste."""
  monitor = payload.get("human_monitor")
  if isinstance(monitor, dict):
    for key in ("human_monitor_url", "scillm_chat_monitor_url"):
      url = monitor.get(key)
      if isinstance(url, str) and url.strip():
        payload["human_monitor_url"] = url.strip()
        break
  return payload


def _run_receipt(
  run: OpenCodeServeRun,
  spec: OpenCodeRunRequest,
  *,
  timeout_s: float,
  skills: SkillViewReceipt,
) -> dict[str, Any]:
  lineage = {
    "parent_session_id": spec.fork_from_session_id,
    "fork_at_message_id": spec.fork_at_message_id,
  }
  return _enrich_run_response(
    {
      "schema": "scillm.opencode_run.receipt.v1",
      "run_id": run.run_id,
      "agent": run.agent,
      "logical_agent": spec.agent or None,
      "session_id": run.session_id,
      "session_lineage": lineage if any(lineage.values()) else None,
      "status": "running",
      "state": "running",
      "phase": _read_run_phase(run) or "created",
      "wait": False,
      "timeout_s": timeout_s,
      "assistant_text": "",
      "assistant_len": 0,
      "scillm_metadata": spec.scillm_metadata,
      "skills": skills.as_dict(),
      "mcp_requested": list(spec.mcp),
      "artifacts": run.artifact_summary(),
      "human_monitor": run.human_monitor,
      "project_agent_message": (
        "Run created and prompt delivery is continuing in the background. "
        "Poll status/events; terminal result will be written to artifacts."
      ),
    }
  )


def _safe_id(value: str) -> str:
  safe = re.sub(r"[^A-Za-z0-9._:-]+", "-", value.strip())
  return safe[:160] or f"oc-{uuid.uuid4().hex[:12]}"


def _now() -> str:
  return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _collaboration_item(
  *,
  agent: str,
  logical_agent: str | None,
  model: str | None,
  response: str,
  status: str,
  thread_type: str = "subagent",
) -> dict[str, Any]:
  """Human-facing Collaboration pane row; proof details remain in artifacts."""
  persona_name = logical_agent or agent
  return {
    "schema": "scillm.collaboration_item.v1",
    "thread_type": thread_type,
    "icon": "debugger" if "debug" in persona_name.casefold() else "agent",
    "person_or_persona_name": persona_name,
    "model": model or "",
    "response": response,
    "status": status,
  }


def _metadata_str(spec: OpenCodeRunRequest, key: str) -> str:
  value = getattr(spec, key, None)
  if isinstance(value, str) and value.strip():
    return value.strip()
  meta_value = spec.scillm_metadata.get(key) if isinstance(spec.scillm_metadata, dict) else None
  return meta_value.strip() if isinstance(meta_value, str) else str(meta_value).strip() if meta_value is not None else ""


def _patch_mode(spec: OpenCodeRunRequest) -> str:
  mode = _metadata_str(spec, "patch_mode").lower()
  return mode if mode in {"dry_run", "live"} else ""


def _patch_delegate_reason_code(reason: str) -> str:
  value = re.sub(r"[^a-z0-9]+", "_", reason.strip().casefold()).strip("_")
  return value[:80] or "blocked_substrate"


def _is_patch_delegate(spec: OpenCodeRunRequest, caller_skill: str) -> bool:
  if _patch_mode(spec):
    return True
  if caller_skill in {"pdf-lab", "pdf_oxide"}:
    prompt = spec.prompt.casefold()
    return "patch delegate" in prompt or "patch_backend=scillm" in prompt or "opencode debugger/patcher" in prompt
  return False


def _changed_paths_from_opencode_diff(diff: list[dict[str, Any]]) -> list[str]:
  out: list[str] = []
  seen: set[str] = set()
  for row in diff:
    if not isinstance(row, dict):
      continue
    for key in ("path", "file", "filename", "new_path", "old_path"):
      value = row.get(key)
      if isinstance(value, str) and value.strip():
        path = value.strip()
        if _ignore_diff_path(path) or path in seen:
          continue
        seen.add(path)
        out.append(path)
  return out


def _ignore_diff_path(path: str) -> bool:
  normalized = path.replace("\\", "/").strip("/")
  parts = normalized.split("/")
  return "__pycache__" in parts or normalized.endswith((".pyc", ".pyo"))


def _git_diff_fallback(directory: str | None) -> tuple[list[dict[str, Any]], str, str]:
  """Return deterministic worktree diff evidence when OpenCode's diff API is empty."""
  if not directory:
    return [], "", "directory_missing"
  root = Path(directory).expanduser()
  if not root.is_dir():
    return [], "", "directory_not_found"
  try:
    git_base = ["git", "-c", f"safe.directory={root}"]
    inside = subprocess.run(
      [*git_base, "-C", str(root), "rev-parse", "--is-inside-work-tree"],
      check=False,
      capture_output=True,
      text=True,
      timeout=5,
    )
  except (OSError, subprocess.SubprocessError) as exc:
    return [], "", f"git_probe_failed:{exc}"
  if inside.returncode != 0 or inside.stdout.strip() != "true":
    detail = (inside.stderr or inside.stdout or "").strip()
    return [], "", f"not_git_worktree:{detail[:300]}" if detail else "not_git_worktree"
  try:
    status = subprocess.run(
      [*git_base, "-C", str(root), "status", "--porcelain=v1"],
      check=False,
      capture_output=True,
      text=True,
      timeout=10,
    )
    patch = subprocess.run(
      [*git_base, "-C", str(root), "diff", "--binary"],
      check=False,
      capture_output=True,
      text=True,
      timeout=30,
    )
  except (OSError, subprocess.SubprocessError) as exc:
    return [], "", f"git_diff_failed:{exc}"
  if status.returncode != 0:
    return [], "", f"git_status_failed:{status.stderr[:300]}"
  rows: list[dict[str, Any]] = []
  for line in status.stdout.splitlines():
    if not line.strip() or len(line) < 4:
      continue
    code = line[:2].strip() or "modified"
    path = line[3:].strip()
    if " -> " in path:
      path = path.split(" -> ", 1)[1].strip()
    if _ignore_diff_path(path):
      continue
    rows.append({"path": path, "status": code, "source": "git_status_porcelain"})
  patch_text = patch.stdout if patch.returncode == 0 else ""
  return rows, patch_text, "" if rows else "empty_git_status"


def _write_diff_artifact(run: OpenCodeServeRun, patch_text: str) -> str:
  if not patch_text:
    return ""
  path = run.run_dir / "diff.patch"
  path.write_text(patch_text, encoding="utf-8", errors="replace")
  return str(path)


def _snapshot_skipped(path: Path) -> bool:
  return any(part in _SNAPSHOT_IGNORED_DIRS for part in path.parts)


def _file_digest(data: bytes) -> str:
  return hashlib.sha256(data).hexdigest()


def _filesystem_snapshot(directory: str | None) -> dict[str, dict[str, Any]]:
  if not directory:
    return {}
  root = Path(directory).expanduser()
  if not root.is_dir():
    return {}
  snapshot: dict[str, dict[str, Any]] = {}
  for path in sorted(root.rglob("*")):
    if len(snapshot) >= _SNAPSHOT_MAX_FILES:
      break
    rel_path = path.relative_to(root)
    if _snapshot_skipped(rel_path):
      continue
    if not path.is_file() or path.is_symlink():
      continue
    try:
      stat = path.stat()
    except OSError:
      continue
    if stat.st_size > _SNAPSHOT_MAX_BYTES:
      continue
    try:
      data = path.read_bytes()
    except OSError:
      continue
    rel = rel_path.as_posix()
    if _ignore_diff_path(rel):
      continue
    try:
      text = data.decode("utf-8")
    except UnicodeDecodeError:
      text = None
    snapshot[rel] = {
      "sha256": _file_digest(data),
      "size": stat.st_size,
      "text": text,
    }
  return snapshot


def _filesystem_diff_from_snapshots(
  before: dict[str, dict[str, Any]] | None,
  after: dict[str, dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], str, str]:
  if not before and not after:
    return [], "", "filesystem_snapshot_missing"
  before = before or {}
  after = after or {}
  rows: list[dict[str, Any]] = []
  patch_lines: list[str] = []
  paths = sorted(set(before) | set(after))
  for rel in paths:
    old = before.get(rel)
    new = after.get(rel)
    if old and new and old.get("sha256") == new.get("sha256"):
      continue
    if old is None:
      status = "added"
      old_lines: list[str] = []
      new_lines = str(new.get("text") or "").splitlines(keepends=True) if new else []
      fromfile = "/dev/null"
      tofile = f"b/{rel}"
    elif new is None:
      status = "deleted"
      old_lines = str(old.get("text") or "").splitlines(keepends=True)
      new_lines = []
      fromfile = f"a/{rel}"
      tofile = "/dev/null"
    else:
      status = "modified"
      old_lines = str(old.get("text") or "").splitlines(keepends=True)
      new_lines = str(new.get("text") or "").splitlines(keepends=True)
      fromfile = f"a/{rel}"
      tofile = f"b/{rel}"
    rows.append({"path": rel, "status": status, "source": "filesystem_snapshot"})
    if (old is None or old.get("text") is not None) and (new is None or new.get("text") is not None):
      patch_lines.extend(
        difflib.unified_diff(
          old_lines,
          new_lines,
          fromfile=fromfile,
          tofile=tofile,
          lineterm="",
        )
      )
      if patch_lines and patch_lines[-1] != "":
        patch_lines.append("")
  patch_text = "\n".join(patch_lines).rstrip() + "\n" if patch_lines else ""
  return rows, patch_text, "" if rows else "empty_filesystem_snapshot"


async def _diff_with_fallback(
  client: OpenCodeServeClient,
  run: OpenCodeServeRun,
  *,
  before_snapshot: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
  diff_error = ""
  try:
    diff = await client.diff(run.session_id, directory=run.directory)
    if not isinstance(diff, list):
      diff = []
  except Exception as exc:
    diff = []
    diff_error = str(exc)
    run.emit("diff_snapshot_failed", error=diff_error)
  changed_paths = _changed_paths_from_opencode_diff(diff)
  patch_path = ""
  fallback_reason = ""
  fallback_used = False
  fallback_source = ""
  if not changed_paths:
    fallback_diff, patch_text, fallback_reason = _git_diff_fallback(run.directory)
    if fallback_diff:
      diff = fallback_diff
      changed_paths = _changed_paths_from_opencode_diff(diff)
      patch_path = _write_diff_artifact(run, patch_text)
      fallback_used = True
      fallback_source = "git_fallback"
      run.emit("diff_snapshot_fallback", diff_count=len(diff), patch_path=patch_path)
  if not changed_paths and before_snapshot is not None:
    after_snapshot = _filesystem_snapshot(run.directory)
    fallback_diff, patch_text, fallback_reason = _filesystem_diff_from_snapshots(before_snapshot, after_snapshot)
    if fallback_diff:
      diff = fallback_diff
      changed_paths = _changed_paths_from_opencode_diff(diff)
      patch_path = _write_diff_artifact(run, patch_text)
      fallback_used = True
      fallback_source = "filesystem_snapshot"
      run.emit("diff_snapshot_filesystem_fallback", diff_count=len(diff), patch_path=patch_path)
  evidence = {
    "diff_count": len(diff),
    "changed_paths": changed_paths,
    "diff_source": fallback_source if fallback_used else "opencode_diff",
    "diff_artifact": patch_path,
    "diff_error": diff_error,
    "fallback_reason": fallback_reason,
  }
  return diff, evidence


def _apply_patch_delegate_status(
  result: dict[str, Any],
  *,
  spec: OpenCodeRunRequest,
  run: OpenCodeServeRun,
  diff_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
  if not _is_patch_delegate(spec, run.caller_skill):
    return result
  mode = _patch_mode(spec) or "live"
  assistant_text = str(result.get("assistant_text") or "").strip()
  diff = result.get("diff") if isinstance(result.get("diff"), list) else []
  evidence = diff_evidence or {}
  changed_paths = evidence.get("changed_paths")
  if not isinstance(changed_paths, list):
    changed_paths = _changed_paths_from_opencode_diff(diff)
  reason = ""
  receipt = classify_patch_delegate_result(assistant_text)
  if result.get("status") == "timeout":
    reason = "timeout_before_terminal_event"
  elif mode == "dry_run":
    reason = "patch_delegate_dry_run"
  elif not assistant_text:
    reason = "empty_assistant_text"
  elif receipt.get("status") == PATCH_DELEGATE_BLOCKED and receipt.get("has_concrete_blocker"):
    reason = _patch_delegate_reason_code(str(receipt.get("reason") or "blocked_substrate"))
  elif not diff:
    reason = "no_patch_delta"
  elif not changed_paths:
    reason = "no_patch_delta"

  status = "PATCH_DELEGATE_BLOCKED" if reason else "PATCH_APPLIED"
  result["patch_delegate_status"] = status
  result["patch_delegate_reason"] = reason
  result["patch_delegate"] = {
    "schema": "scillm.pdf_lab.patch_delegate_terminal.v1",
    "status": status,
    "reason": reason,
    "patch_mode": mode,
    "batch_id": _metadata_str(spec, "batch_id"),
    "case_id": _metadata_str(spec, "case_id") or _metadata_str(spec, "item_id"),
    "page_number": spec.page_number if spec.page_number is not None else spec.scillm_metadata.get("page_number"),
    "candidate_id": _metadata_str(spec, "candidate_id"),
    "changed_paths": changed_paths,
    "diff_count": len(diff),
    "diff_source": evidence.get("diff_source", "opencode_diff"),
    "diff_artifact": evidence.get("diff_artifact", ""),
    "substrate_reason": reason
    or f"patch_applied_with_{len(diff)}_diff_entries",
    "receipt_classifier": receipt,
  }
  if status == "PATCH_DELEGATE_BLOCKED":
    result["project_agent_message"] = (
      f"PATCH_DELEGATE_BLOCKED: {reason}. Inspect artifacts; do not treat this OpenCode run as applied."
    )
  else:
    result["project_agent_message"] = (
      "PATCH_APPLIED: OpenCode returned assistant evidence and non-empty diff evidence. "
      "pdf-lab must still validate scope, tests, and commit gate."
    )
  return result


def _session_id(session_payload: dict[str, Any]) -> str:
  for key in ("id", "sessionID", "sessionId"):
    value = session_payload.get(key)
    if isinstance(value, str) and value.strip():
      return value.strip()
  raise ProxyError(502, "opencode serve session response missing id", "provider_error")




def _resolve_run_directory(cwd: str | None) -> str | None:
  """Resolve and validate caller workspace for OpenCode serve (``?directory=``)."""
  if cwd is None:
    return None
  raw = str(cwd).strip()
  if not raw:
    return None
  path = Path(raw).expanduser().resolve()
  if not path.is_dir():
    raise ProxyError(400, f"cwd is not a directory: {cwd}", "invalid_request_error")
  return str(path)


def _protected_session_ids() -> set[str]:
  """Sessions actively executing inside this process (not stale artifact rows)."""
  return {run.session_id for run in _ACTIVE_RUNS.values() if run.session_id}


def _session_owned_by_run(session_id: str, run_id: str | None) -> bool:
  """True when ``run_id`` names the active or artifact owner of ``session_id``."""
  if not run_id or not session_id:
    return False
  safe_run = _safe_id(run_id)
  active = _ACTIVE_RUNS.get(safe_run)
  if active is not None and active.session_id == session_id:
    return True
  status_path = _artifact_root() / safe_run / "status.json"
  if not status_path.exists():
    return False
  try:
    data = json.loads(status_path.read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError):
    return False
  return str(data.get("session_id") or "").strip() == session_id




def _message_role(message: dict[str, Any]) -> str:
  info = message.get("info") if isinstance(message.get("info"), dict) else {}
  role = info.get("role")
  return str(role).strip() if isinstance(role, str) else ""


def _extract_message_excerpt(message: dict[str, Any], *, max_chars: int = 4000) -> str:
  """Collect assistant-visible text including reasoning when plain text is absent."""
  parts = message.get("parts")
  chunks: list[str] = []
  if isinstance(parts, list):
    for part in parts:
      if not isinstance(part, dict):
        continue
      part_type = str(part.get("type") or "")
      if part_type == "text" and part.get("text"):
        chunks.append(str(part["text"]))
      elif part_type == "reasoning":
        for key in ("text", "reasoning", "content"):
          value = part.get(key)
          if isinstance(value, str) and value.strip():
            chunks.append(value.strip())
            break
  text = "\n".join(chunks).strip()
  if text:
    return text[:max_chars]
  return extract_assistant_text(message)[:max_chars]


def _extract_message_text_parts(message: dict[str, Any], *, max_chars: int = 4000) -> str:
  """Collect final assistant text parts, excluding reasoning-only preambles."""
  parts = message.get("parts")
  chunks: list[str] = []
  if isinstance(parts, list):
    for part in parts:
      if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
        chunks.append(str(part["text"]))
  text = "\n".join(chunks).strip()
  return text[:max_chars]


def _extract_message_reasoning_parts(message: dict[str, Any], *, max_chars: int = 4000) -> str:
  """Collect reasoning text for diagnostics; never use it as terminal assistant text."""
  parts = message.get("parts")
  chunks: list[str] = []
  if isinstance(parts, list):
    for part in parts:
      if not isinstance(part, dict) or part.get("type") != "reasoning":
        continue
      for key in ("text", "reasoning", "content"):
        value = part.get(key)
        if isinstance(value, str) and value.strip():
          chunks.append(value.strip())
          break
  text = "\n".join(chunks).strip()
  return text[:max_chars]


def _format_message_for_dialog(message: dict[str, Any]) -> str:
  """Render OpenCode message parts as markdown for transport-style dialog."""
  chunks: list[str] = []
  parts = message.get("parts")
  if isinstance(parts, list):
    for part in parts:
      if not isinstance(part, dict):
        continue
      part_type = str(part.get("type") or "")
      if part_type == "text" and part.get("text"):
        chunks.append(str(part["text"]))
      elif part_type == "reasoning":
        for key in ("text", "reasoning", "content"):
          value = part.get(key)
          if isinstance(value, str) and value.strip():
            chunks.append(f"_Reasoning:_ {value.strip()}")
            break
      elif part_type in {"tool", "tool-call", "tool_call", "tool-invocation"}:
        name = part.get("tool") or part.get("name") or "tool"
        state = part.get("state") or part.get("status") or ""
        err = part.get("error") or part.get("errorMessage") or ""
        line = f"- **{name}** ({state})"
        if err:
          line += f": {err}"
        chunks.append(line)
  if chunks:
    return "\n\n".join(chunks)
  return _extract_message_excerpt(message)


def _summarize_message_parts(message: dict[str, Any]) -> list[dict[str, Any]]:
  parts = message.get("parts")
  if not isinstance(parts, list):
    return []
  rows: list[dict[str, Any]] = []
  for part in parts:
    if not isinstance(part, dict):
      continue
    part_type = str(part.get("type") or "unknown")
    if part_type not in {"tool", "tool-call", "tool_call", "tool-invocation", "reasoning", "text"}:
      continue
    row: dict[str, Any] = {"type": part_type}
    if part_type in {"tool", "tool-call", "tool_call", "tool-invocation"}:
      row["tool"] = part.get("tool") or part.get("name") or part.get("toolName")
      row["state"] = part.get("state") or part.get("status")
      err = part.get("error") or part.get("errorMessage") or part.get("message")
      if err:
        row["error"] = str(err)[:500]
    elif part_type == "reasoning":
      row["excerpt"] = _extract_message_excerpt({"parts": [part]}, max_chars=500)
    elif part.get("text"):
      row["excerpt"] = str(part.get("text"))[:500]
    rows.append(row)
  return rows


_TOOL_PART_TYPES = {"tool", "tool-call", "tool_call", "tool-invocation"}
_PENDING_TOOL_STATUSES = {"", "pending", "queued", "running", "working", "in_progress", "started"}
_TERMINAL_TOOL_STATUSES = {
  "cancelled",
  "canceled",
  "complete",
  "completed",
  "done",
  "error",
  "failed",
  "failure",
  "rejected",
  "success",
}


def _tool_state_status(row: dict[str, Any]) -> str:
  state = row.get("state")
  if isinstance(state, dict):
    for key in ("status", "state", "phase"):
      value = state.get(key)
      if isinstance(value, str):
        return value.strip().casefold()
    return ""
  if isinstance(state, str):
    return state.strip().casefold()
  status = row.get("status")
  return status.strip().casefold() if isinstance(status, str) else ""


def _tool_rows(message: dict[str, Any] | None) -> list[dict[str, Any]]:
  if not isinstance(message, dict):
    return []
  return [row for row in _summarize_message_parts(message) if row.get("type") in _TOOL_PART_TYPES]


def _message_tool_call_finish(message: dict[str, Any] | None) -> bool:
  if not isinstance(message, dict):
    return False
  info = message.get("info") if isinstance(message.get("info"), dict) else {}
  finish = info.get("finish")
  if isinstance(finish, str) and finish.strip().casefold() in {"tool-calls", "tool_calls", "tool-call", "tool_call"}:
    return True
  parts = message.get("parts")
  if not isinstance(parts, list):
    return False
  for part in parts:
    if not isinstance(part, dict) or str(part.get("type") or "") != "step-finish":
      continue
    reason = part.get("reason")
    if isinstance(reason, str) and reason.strip().casefold() in {"tool-calls", "tool_calls", "tool-call", "tool_call"}:
      return True
  return False


def _pending_tool_rows(message: dict[str, Any] | None) -> list[dict[str, Any]]:
  pending: list[dict[str, Any]] = []
  for row in _tool_rows(message):
    status = _tool_state_status(row)
    if row.get("error"):
      continue
    if status in _TERMINAL_TOOL_STATUSES:
      continue
    if status in _PENDING_TOOL_STATUSES or not status:
      pending.append(row)
  return pending


_TOOL_PATH_INPUT_KEYS = ("path", "filePath", "filepath", "cwd", "dir", "directory", "root")


def _resolve_tool_input_path(raw: str, *, run_directory: str) -> Path | None:
  text = raw.strip()
  if not text:
    return None
  base = Path(run_directory).resolve()
  candidate = Path(text)
  if not candidate.is_absolute():
    candidate = base / candidate
  return candidate.resolve()


def _path_is_relative_to(path: Path, base: Path) -> bool:
  try:
    path.relative_to(base)
    return True
  except ValueError:
    return False


def _tool_scope_violation_rows(
  pending_tools: list[dict[str, Any]],
  *,
  run_directory: str | None,
) -> list[dict[str, Any]]:
  if not run_directory:
    return []
  base = Path(run_directory).resolve()
  violations: list[dict[str, Any]] = []
  for row in pending_tools:
    state = row.get("state")
    if not isinstance(state, dict):
      continue
    inputs = state.get("input")
    if not isinstance(inputs, dict):
      continue
    for key in _TOOL_PATH_INPUT_KEYS:
      raw = inputs.get(key)
      if not isinstance(raw, str):
        continue
      resolved = _resolve_tool_input_path(raw, run_directory=run_directory)
      if resolved is None or _path_is_relative_to(resolved, base):
        continue
      violation = dict(row)
      violation["scope_violation"] = {
        "input_key": key,
        "input_path": raw,
        "resolved_path": str(resolved),
        "allowed_root": str(base),
      }
      violations.append(violation)
  return violations


def _awaiting_terminal_text_after_tools(message: dict[str, Any] | None) -> bool:
  return bool(_tool_rows(message)) and (
    not bool(_extract_message_text_parts(message)) or _message_tool_call_finish(message)
  )


def _message_provider_error(message: dict[str, Any] | None) -> dict[str, Any] | None:
  if not isinstance(message, dict):
    return None
  info = message.get("info") if isinstance(message.get("info"), dict) else {}
  raw = info.get("error")
  if not isinstance(raw, dict):
    return None
  out: dict[str, Any] = {}
  if isinstance(raw.get("name"), str) and raw.get("name"):
    out["name"] = raw.get("name")
  data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
  provider_id = data.get("providerID") or data.get("providerId") or data.get("provider")
  if isinstance(provider_id, str) and provider_id:
    out["provider_id"] = provider_id
  message_text = raw.get("message") or data.get("message")
  if isinstance(message_text, str) and message_text:
    out["message"] = message_text[:1000]
  if not out:
    out["raw"] = str(raw)[:1000]
  return out


def _message_info_fields(message: dict[str, Any] | None) -> dict[str, Any]:
  if not isinstance(message, dict):
    return {}
  info = message.get("info") if isinstance(message.get("info"), dict) else {}
  out: dict[str, Any] = {}
  if isinstance(info.get("id"), str) and info.get("id"):
    out["last_message_id"] = info.get("id")
  if isinstance(info.get("role"), str) and info.get("role"):
    out["last_message_role"] = info.get("role")
  if info.get("completed") is not None:
    out["last_message_completed"] = info.get("completed")
  if info.get("finish") is not None:
    out["last_finish_reason"] = info.get("finish")
  provider_error = _message_provider_error(message)
  if provider_error:
    out["last_provider_error"] = provider_error
  return out


def _summarize_messages_thread(
  messages: list[dict[str, Any]],
  *,
  run_directory: str | None = None,
) -> dict[str, Any]:
  user_count = sum(1 for item in messages if _message_role(item) == "user")
  assistant_count = sum(1 for item in messages if _message_role(item) in {"", "assistant"})
  last_message = messages[-1] if messages else None
  last_assistant: dict[str, Any] | None = None
  for item in reversed(messages):
    if _message_role(item) in {"", "assistant"}:
      last_assistant = item
      break
  last_tool_assistant: dict[str, Any] | None = None
  for item in reversed(messages):
    if _message_role(item) in {"", "assistant"} and _tool_rows(item):
      last_tool_assistant = item
      break
  tool_context = last_tool_assistant or last_assistant
  info_fields = _message_info_fields(last_assistant or last_message)
  tool_calls: list[dict[str, Any]] = []
  tool_errors: list[dict[str, Any]] = []
  pending_tools: list[dict[str, Any]] = []
  if tool_context:
    for row in _tool_rows(tool_context):
      tool_calls.append(row)
      state = _tool_state_status(row)
      if state in {"error", "failed", "failure"} or row.get("error"):
        tool_errors.append(row)
    pending_tools = _pending_tool_rows(tool_context)
  scope_violations = _tool_scope_violation_rows(pending_tools, run_directory=run_directory)
  terminal_text = _extract_message_text_parts(last_assistant) if last_assistant else ""
  reasoning_text = _extract_message_reasoning_parts(last_assistant) if last_assistant else ""
  excerpt = _extract_message_excerpt(last_assistant) if last_assistant else ""
  tool_call_finish = _message_tool_call_finish(last_assistant)
  return {
    "schema": "scillm.opencode_run.timeout_summary.v1",
    "message_count": len(messages),
    "user_count": user_count,
    "assistant_count": assistant_count,
    "last_assistant_excerpt": excerpt,
    "last_tool_calls": tool_calls[-8:],
    "last_tool_errors": tool_errors[-8:],
    "last_pending_tools": pending_tools[-8:],
    "last_pending_tool_count": len(pending_tools),
    "last_tool_scope_violations": scope_violations[-8:],
    "last_tool_scope_violation_count": len(scope_violations),
    "last_assistant_has_tool_calls": bool(tool_calls),
    "last_assistant_tool_call_finish": tool_call_finish,
    "last_assistant_terminal_text_chars": len(terminal_text),
    "last_assistant_reasoning_chars": len(reasoning_text),
    "last_assistant_waiting_terminal_text": bool(tool_calls) and (not terminal_text or tool_call_finish),
    "last_assistant_reasoning_only": bool(reasoning_text) and not terminal_text and not tool_calls,
    "last_tool_message": _message_info_fields(last_tool_assistant) if last_tool_assistant else None,
    **info_fields,
  }


def _first_assistant_or_tool_delta(messages: list[dict[str, Any]]) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
  """Return first observable assistant text or tool activity in a thread."""
  for item in messages:
    if _message_role(item) not in {"", "assistant"}:
      continue
    text = _extract_message_excerpt(item)
    tool_calls = _tool_rows(item)
    pending_tools = _pending_tool_rows(item)
    if text or tool_calls:
      return text, item, {
        "assistant_chars": len(text),
        "tool_count": len(tool_calls),
        "pending_tool_count": len(pending_tools),
        "first_tool": tool_calls[0] if tool_calls else None,
        **_message_info_fields(item),
      }
  return "", None, {}


def _patch_delegate_terminal_sentinel(text: str) -> bool:
  upper = text.upper()
  return "PATCH_APPLIED" in upper or "PATCH_DELEGATE_BLOCKED" in upper


def _patch_delegate_followup_prompt() -> str:
  return (
    "Based on the work already done in this session, reply with exactly one terminal line: "
    "either PATCH_APPLIED <short evidence> or PATCH_DELEGATE_BLOCKED reason=<concrete blocker>. "
    "Do not call more tools."
  )


def _build_terminal_blocker(
  *,
  cause: str,
  timeout_s: float | None,
  timeout_summary: dict[str, Any],
  diff_evidence: dict[str, Any],
  sentinel_required: bool,
  assistant_text: str,
) -> dict[str, Any]:
  sentinel_observed = _patch_delegate_terminal_sentinel(assistant_text)
  blocker: dict[str, Any] = {
    "schema": "scillm.opencode_run.terminal_blocker.v1",
    "cause": cause,
    "sentinel_required": sentinel_required,
    "sentinel_observed": sentinel_observed,
    "timeout_s": timeout_s,
    "messages_snapshot_count": timeout_summary.get("message_count", 0),
    "last_message_id": timeout_summary.get("last_message_id"),
    "last_message_completed": timeout_summary.get("last_completed"),
    "last_finish_reason": timeout_summary.get("last_finish"),
    "last_assistant_excerpt": timeout_summary.get("last_assistant_excerpt") or "",
    "last_tool_calls": timeout_summary.get("last_tool_calls") or [],
    "last_tool_errors": timeout_summary.get("last_tool_errors") or [],
    "last_pending_tools": timeout_summary.get("last_pending_tools") or [],
    "last_pending_tool_count": timeout_summary.get("last_pending_tool_count", 0),
    "last_tool_scope_violations": timeout_summary.get("last_tool_scope_violations") or [],
    "last_tool_scope_violation_count": timeout_summary.get("last_tool_scope_violation_count", 0),
    "last_assistant_tool_call_finish": bool(timeout_summary.get("last_assistant_tool_call_finish")),
    "last_assistant_terminal_text_chars": timeout_summary.get("last_assistant_terminal_text_chars", 0),
    "diff_count": diff_evidence.get("diff_count", 0),
    "changed_paths": diff_evidence.get("changed_paths") or [],
    "diff_source": diff_evidence.get("diff_source", ""),
    "git_fallback_reason": diff_evidence.get("fallback_reason", ""),
  }
  if timeout_summary.get("last_provider_error"):
    blocker["provider_error"] = timeout_summary.get("last_provider_error")
    blocker["primary_reason"] = "provider_auth_error"
  elif timeout_summary.get("last_tool_errors"):
    blocker["primary_reason"] = "tool_error"
  elif timeout_summary.get("last_tool_scope_violation_count", 0):
    blocker["primary_reason"] = "tool_scope_violation"
  elif timeout_summary.get("last_pending_tool_count", 0):
    blocker["primary_reason"] = "pending_tool_unresolved"
  elif timeout_summary.get("last_assistant_tool_call_finish"):
    blocker["primary_reason"] = "tool_call_turn_without_terminal_text"
  elif timeout_summary.get("last_assistant_waiting_terminal_text"):
    blocker["primary_reason"] = "tool_completed_without_terminal_text"
  elif timeout_summary.get("last_assistant_reasoning_only"):
    blocker["primary_reason"] = "reasoning_only_no_terminal_text"
  elif timeout_summary.get("message_count", 0) > 1 and not sentinel_observed:
    blocker["primary_reason"] = "timeout_before_terminal_sentinel"
  elif not timeout_summary.get("last_assistant_excerpt"):
    blocker["primary_reason"] = "no_assistant_excerpt"
  else:
    blocker["primary_reason"] = cause
  return blocker


def _timeout_project_agent_message(
  *,
  timeout_s: float,
  timeout_summary: dict[str, Any],
  terminal_blocker: dict[str, Any],
) -> str:
  excerpt = str(timeout_summary.get("last_assistant_excerpt") or "").strip()
  tool_errors = terminal_blocker.get("last_tool_errors") or []
  provider_error = terminal_blocker.get("provider_error") if isinstance(terminal_blocker.get("provider_error"), dict) else None
  if provider_error:
    headline = "OpenCode provider returned a terminal error; session aborted with provider diagnostics."
  else:
    headline = f"OpenCode run exceeded timeout_s={timeout_s}. Session aborted; inspect artifacts and retry."
  parts = [
    headline,
    f"messages={timeout_summary.get('message_count', 0)} assistant={timeout_summary.get('assistant_count', 0)}.",
  ]
  if provider_error:
    parts.append(
      "Provider error: "
      f"{provider_error.get('name', 'provider_error')} "
      f"provider={provider_error.get('provider_id', 'unknown')} "
      f"{provider_error.get('message', '')}".strip()
    )
  if excerpt:
    short = excerpt if len(excerpt) <= 400 else excerpt[:400] + "…"
    parts.append(f"Latest assistant excerpt: {short}")
  if tool_errors:
    first = tool_errors[0]
    parts.append(
      f"Latest tool error: {first.get('tool')} state={first.get('state')} {first.get('error', '')}".strip()
    )
  parts.append(f"terminal_blocker.primary_reason={terminal_blocker.get('primary_reason')}")
  return " ".join(parts)


async def _snapshot_run_thread(
  client: OpenCodeServeClient,
  run: OpenCodeServeRun,
  *,
  limit: int = 50,
) -> tuple[list[dict[str, Any]], str, dict[str, Any] | None]:
  """Persist the current OpenCode message thread and return latest assistant text."""
  messages = await client.list_messages(run.session_id, limit=limit, directory=run.directory)
  snapshot = {
    "schema": "scillm.opencode_run.messages_snapshot.v1",
    "run_id": run.run_id,
    "session_id": run.session_id,
    "directory": run.directory,
    "messages": messages,
  }
  (run.run_dir / "messages_snapshot.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
  for item in reversed(messages):
    if _message_role(item) in {"", "assistant"}:
      text = _extract_message_excerpt(item)
      if text:
        return messages, text, item
  return messages, "", None


def _enrich_session_row(
  session_payload: dict[str, Any],
  status_map: dict[str, Any],
  *,
  protected_ids: set[str],
  stale_busy_s: float,
  max_idle_age_s: float,
  kill_idle: bool,
) -> dict[str, Any]:
  session_id = session_id_from_payload(session_payload) or ""
  anchor = session_epoch_s(session_payload)
  now = time.time()
  age_s = (now - anchor) if anchor is not None else None
  busy = session_is_busy(status_map, session_id) if session_id else False
  protected = session_id in protected_ids
  zombie_reason = None
  if session_id and not protected:
    zombie_reason = classify_zombie_session(
      session_payload,
      status_map,
      protected_ids=protected_ids,
      stale_busy_s=stale_busy_s,
      max_idle_age_s=max_idle_age_s,
      kill_idle=kill_idle,
    )
  return {
    "session_id": session_id,
    "title": session_payload.get("title"),
    "busy": busy,
    "status": status_map.get(session_id),
    "age_s": round(age_s, 1) if age_s is not None else None,
    "protected": protected,
    "zombie_reason": zombie_reason,
    "session": session_payload,
  }


async def _poll_until_idle(
  client: OpenCodeServeClient,
  session_id: str,
  *,
  deadline: float,
  directory: str | None = None,
) -> dict[str, Any]:
  """Wait until OpenCode reports the session idle (global status map).

  ``GET /session/status?directory=`` often returns an empty map; use the global
  status endpoint for busy/idle detection, then scope message reads by directory.
  """
  last_status: dict[str, Any] = {}
  while time.monotonic() < deadline:
    last_status = await client.session_status_map()
    if not session_is_busy(last_status, session_id):
      return last_status
    await asyncio.sleep(1.0)
  raise ProxyError(504, f"opencode session {session_id} did not become idle before timeout", "timeout")


async def _latest_assistant_message(
  client: OpenCodeServeClient,
  session_id: str,
  *,
  directory: str | None = None,
) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
  messages = await client.list_messages(session_id, limit=50, directory=directory)
  for item in reversed(messages):
    if _message_role(item) in {"", "assistant"}:
      text = _extract_message_text_parts(item)
      pending_tools = _pending_tool_rows(item)
      if text and not pending_tools and not _message_tool_call_finish(item):
        return text, item, []
      if pending_tools:
        return "", item, pending_tools
      if _tool_rows(item):
        return "", item, []
  return "", None, []


async def _wait_for_first_assistant_or_tool_delta(
  client: OpenCodeServeClient,
  run: OpenCodeServeRun,
  *,
  deadline: float,
) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
  while time.monotonic() < deadline:
    messages = await client.list_messages(run.session_id, limit=50, directory=run.directory)
    text, message, delta = _first_assistant_or_tool_delta(messages)
    if message is not None:
      return text, message, delta
    await asyncio.sleep(1.0)
  return "", None, {}




async def _maybe_patch_delegate_followup(
  client: OpenCodeServeClient,
  run: OpenCodeServeRun,
  spec: OpenCodeRunRequest,
  *,
  model: str | None,
  assistant_text: str,
  deadline: float,
) -> tuple[str, dict[str, Any] | None]:
  """One bounded follow-up when patch delegate prompts lack a terminal sentinel."""
  if not _is_patch_delegate(spec, run.caller_skill):
    return assistant_text, None
  if _patch_delegate_terminal_sentinel(assistant_text):
    return assistant_text, None
  remaining = deadline - time.monotonic()
  if remaining < 15:
    return assistant_text, None
  budget = min(60.0, max(10.0, remaining - 5.0))
  try:
    payload = await asyncio.wait_for(
      client.send_message(
        run.session_id,
        agent=run.agent,
        model=model,
        parts=text_parts(_patch_delegate_followup_prompt()),
        directory=run.directory,
      ),
      timeout=budget,
    )
    run.emit("patch_delegate_followup_sent", budget_s=budget)
  except Exception as exc:
    run.emit("patch_delegate_followup_failed", error=str(exc))
    return assistant_text, None
  follow_deadline = time.monotonic() + budget
  try:
    await _poll_until_idle(client, run.session_id, deadline=follow_deadline, directory=run.directory)
  except ProxyError:
    pass
  text, message, pending_tools = await _latest_assistant_message(
    client,
    run.session_id,
    directory=run.directory,
  )
  if pending_tools:
    run.emit("patch_delegate_followup_pending_tools", pending_tool_count=len(pending_tools))
    return assistant_text, message
  if text:
    return text, message
  if isinstance(payload, dict):
    text = _extract_message_excerpt(payload)
    if text:
      return text, payload
  return assistant_text, message



def _count_run_events(run: OpenCodeServeRun) -> int:
  if not run.events_path.is_file():
    return 0
  return sum(1 for line in run.events_path.read_text(errors="replace").splitlines() if line.strip())


def _last_run_event(run: OpenCodeServeRun) -> dict[str, Any] | None:
  if not run.events_path.is_file():
    return None
  last: dict[str, Any] | None = None
  for line in run.events_path.read_text(errors="replace").splitlines():
    line = line.strip()
    if not line:
      continue
    try:
      row = json.loads(line)
    except json.JSONDecodeError:
      continue
    if isinstance(row, dict):
      last = row
  return last


def _read_run_phase(run: OpenCodeServeRun) -> str:
  if not run.status_path.is_file():
    return ""
  try:
    status = json.loads(run.status_path.read_text(encoding="utf-8"))
  except json.JSONDecodeError:
    return ""
  return str(status.get("phase") or "")


def _run_is_terminal(status_payload: dict[str, Any]) -> bool:
  state = str(status_payload.get("state") or "").strip().lower()
  return state in {"completed", "timeout", "failed", "disconnected", "aborted", "killed", "deleted"}





async def _maybe_finalize_orphan_run(run: OpenCodeServeRun) -> None:
  """Recover runs left running when the POST worker died without writing a result."""
  if run.run_id in _ACTIVE_RUNS:
    return
  if run.result_path.is_file():
    return
  status_payload: dict[str, Any] = {}
  if run.status_path.is_file():
    try:
      status_payload = json.loads(run.status_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
      status_payload = {}
  if _run_is_terminal(status_payload):
    return
  req = run.request_payload if isinstance(run.request_payload, dict) and run.request_payload else {}
  request_path = run.run_dir / "request.json"
  if not req and request_path.is_file():
    try:
      req = json.loads(request_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
      req = {}
  try:
    spec = OpenCodeRunRequest.model_validate(req)
  except Exception:
    spec = OpenCodeRunRequest(prompt=str(req.get("prompt") or "orphan run"), agent=run.agent)
  skills = status_payload.get("skills") if isinstance(status_payload.get("skills"), dict) else {}
  receipt = SkillViewReceipt(
    skills_requested=tuple(),
    skills_materialized=tuple(skills.get("skills_materialized") or ()),
    skills_missing=tuple(skills.get("skills_missing") or ()),
    skill_view_dir=skills.get("skill_view_dir"),
  )
  settings = load_opencode_serve_settings()
  timeout_s = float(spec.timeout_s or settings.timeout_s)
  await _disconnect_run_result(
    run,
    spec,
    receipt=receipt,
    timeout_s=timeout_s,
    http_error="run orphaned without terminal result (recovered on read)",
    phase=str(status_payload.get("phase") or ""),
    cause="orphan_run_recovered",
  )

async def _disconnect_run_result(
  run: OpenCodeServeRun,
  spec: OpenCodeRunRequest,
  *,
  receipt: SkillViewReceipt,
  timeout_s: float,
  http_error: str | None = None,
  phase: str | None = None,
  cause: str = "opencode_serve_response_disconnected",
) -> dict[str, Any]:
  """Finalize a run when the HTTP worker disconnects or cannot complete normally."""
  settings = load_opencode_serve_settings()
  message: dict[str, Any] | None = None
  messages_snapshot: list[dict[str, Any]] = []
  diff: list[dict[str, Any]] = []
  diff_evidence: dict[str, Any] = {}
  status_map: dict[str, Any] = {}
  async with OpenCodeServeClient(settings) as client:
    try:
      messages_snapshot, recovered_text, message = await _snapshot_run_thread(client, run)
      run.emit(
        "messages_snapshot",
        message_count=len(messages_snapshot),
        assistant_chars=len(recovered_text),
        reason="disconnect",
      )
    except Exception as exc:
      run.emit("messages_snapshot_failed", error=str(exc), reason="disconnect")
    try:
      diff, diff_evidence = await _diff_with_fallback(client, run)
      run.emit("diff_snapshot", diff_count=len(diff), source=diff_evidence.get("diff_source"))
    except Exception as exc:
      diff = []
      diff_evidence = {"diff_count": 0, "changed_paths": [], "diff_error": str(exc)}
      run.emit("diff_snapshot_failed", error=str(exc), reason="disconnect")
    try:
      await client.abort(run.session_id, directory=run.directory)
      run.emit("session_aborted", reason="disconnect")
    except Exception as exc:
      run.emit("session_abort_failed", error=str(exc), reason="disconnect")
    try:
      status_map = await client.session_status_map(directory=run.directory)
    except Exception as exc:
      status_map = {}
      run.emit("disconnect_status_snapshot_failed", error=str(exc))

  lineage = {
    "parent_session_id": spec.fork_from_session_id,
    "fork_at_message_id": spec.fork_at_message_id,
  }
  last_event = _last_run_event(run)
  timeout_summary = _summarize_messages_thread(messages_snapshot, run_directory=run.directory)
  partial_assistant_text = str(timeout_summary.get("last_assistant_excerpt") or "")
  if not partial_assistant_text and message is not None:
    partial_assistant_text = _extract_message_excerpt(message)
  sentinel_required = _is_patch_delegate(spec, run.caller_skill)
  terminal_blocker = _build_terminal_blocker(
    cause=cause,
    timeout_s=timeout_s,
    timeout_summary=timeout_summary,
    diff_evidence=diff_evidence,
    sentinel_required=sentinel_required,
    assistant_text=partial_assistant_text,
  )
  terminal_blocker["type"] = "opencode_serve_response_disconnected"
  terminal_blocker["message"] = (
    "OpenCode serve request disconnected before terminal assistant response"
  )
  if phase:
    terminal_blocker["phase"] = phase
  disconnect_summary = {
    "http_error": http_error or "unknown disconnect",
    "last_event": last_event.get("event") if last_event else None,
    "last_event_ts": last_event.get("ts") if last_event else None,
    "event_count": _count_run_events(run),
    "phase": phase or _read_run_phase(run),
  }
  project_agent_message = _timeout_project_agent_message(
    timeout_s=timeout_s,
    timeout_summary=timeout_summary,
    terminal_blocker=terminal_blocker,
  )
  project_agent_message = (
    f"OpenCode serve disconnected ({disconnect_summary.get('http_error')}). "
    f"Inspect artifacts and human_monitor.scillm_chat_monitor_url. "
    + project_agent_message
  )
  result = {
    "schema": "scillm.opencode_run.result.v1",
    "run_id": run.run_id,
    "agent": run.agent,
    "logical_agent": spec.agent or None,
    "session_id": run.session_id,
    "session_lineage": lineage if any(lineage.values()) else None,
    "status": "disconnected",
    "timeout_s": timeout_s,
    "assistant_text": partial_assistant_text,
    "collaboration_item": _collaboration_item(
      agent=run.agent,
      logical_agent=spec.agent or None,
      model=spec.model,
      response=partial_assistant_text,
      status="disconnected",
    ),
    "message": message,
    "messages_snapshot_count": len(messages_snapshot),
    "timeout_summary": timeout_summary,
    "disconnect_summary": disconnect_summary,
    "terminal_blocker": terminal_blocker,
    "session_status": status_map.get(run.session_id) if isinstance(status_map, dict) else None,
    "diff": diff,
    "diff_evidence": diff_evidence,
    "scillm_metadata": spec.scillm_metadata,
    "skills": receipt.as_dict(),
    "mcp_requested": list(spec.mcp),
    "artifacts": run.artifact_summary(),
    "human_monitor": run.human_monitor,
    "project_agent_message": project_agent_message,
  }
  result = _apply_patch_delegate_status(result, spec=spec, run=run, diff_evidence=diff_evidence)
  run.write_result(result)
  run.write_status(
    state="disconnected",
    phase="disconnected",
    timeout_s=timeout_s,
    timeout_summary=timeout_summary,
    disconnect_summary=disconnect_summary,
    terminal_blocker=terminal_blocker,
    patch_delegate_status=result.get("patch_delegate_status"),
    patch_delegate_reason=result.get("patch_delegate_reason"),
    error=http_error,
  )
  run.emit(
    "run_disconnected",
    http_error=disconnect_summary.get("http_error"),
    last_event=disconnect_summary.get("last_event"),
    event_count=disconnect_summary.get("event_count"),
    primary_reason=terminal_blocker.get("primary_reason"),
  )
  return result


async def _timeout_run_result(
  run: OpenCodeServeRun,
  spec: OpenCodeRunRequest,
  *,
  receipt: SkillViewReceipt,
  timeout_s: float,
  partial_assistant_text: str = "",
) -> dict[str, Any]:
  settings = load_opencode_serve_settings()
  message: dict[str, Any] | None = None
  messages_snapshot: list[dict[str, Any]] = []
  diff: list[dict[str, Any]] = []
  diff_evidence: dict[str, Any] = {}
  status_map: dict[str, Any] = {}
  async with OpenCodeServeClient(settings) as client:
    try:
      messages_snapshot, recovered_text, message = await _snapshot_run_thread(client, run)
      if recovered_text:
        partial_assistant_text = recovered_text
      run.emit(
        "messages_snapshot",
        message_count=len(messages_snapshot),
        assistant_chars=len(partial_assistant_text),
      )
    except Exception as exc:
      run.emit("messages_snapshot_failed", error=str(exc))
    try:
      diff, diff_evidence = await _diff_with_fallback(client, run)
      run.emit("diff_snapshot", diff_count=len(diff), source=diff_evidence.get("diff_source"))
    except Exception as exc:
      diff = []
      diff_evidence = {"diff_count": 0, "changed_paths": [], "diff_error": str(exc)}
      run.emit("diff_snapshot_failed", error=str(exc))
    try:
      await client.abort(run.session_id, directory=run.directory)
      run.emit("session_aborted", reason="timeout")
    except Exception as exc:
      run.emit("session_abort_failed", error=str(exc))
    try:
      status_map = await client.session_status_map(directory=run.directory)
    except Exception as exc:
      status_map = {}
      run.emit("timeout_status_snapshot_failed", error=str(exc))

  lineage = {
    "parent_session_id": spec.fork_from_session_id,
    "fork_at_message_id": spec.fork_at_message_id,
  }
  timeout_summary = _summarize_messages_thread(messages_snapshot, run_directory=run.directory)
  sentinel_required = _is_patch_delegate(spec, run.caller_skill)
  if not partial_assistant_text:
    partial_assistant_text = str(timeout_summary.get("last_assistant_excerpt") or "")
  terminal_blocker = _build_terminal_blocker(
    cause="scillm_timeout",
    timeout_s=timeout_s,
    timeout_summary=timeout_summary,
    diff_evidence=diff_evidence,
    sentinel_required=sentinel_required,
    assistant_text=partial_assistant_text,
  )
  project_agent_message = _timeout_project_agent_message(
    timeout_s=timeout_s,
    timeout_summary=timeout_summary,
    terminal_blocker=terminal_blocker,
  )
  provider_error = terminal_blocker.get("provider_error") if isinstance(terminal_blocker.get("provider_error"), dict) else None
  result_status = "provider_error" if provider_error else "timeout"
  result = {
    "schema": "scillm.opencode_run.result.v1",
    "run_id": run.run_id,
    "agent": run.agent,
    "logical_agent": spec.agent or None,
    "session_id": run.session_id,
    "session_lineage": lineage if any(lineage.values()) else None,
    "status": result_status,
    "timeout_s": timeout_s,
    "assistant_text": partial_assistant_text,
    "collaboration_item": _collaboration_item(
      agent=run.agent,
      logical_agent=spec.agent or None,
      model=spec.model,
      response=partial_assistant_text,
      status=result_status,
    ),
    "message": message,
    "messages_snapshot_count": len(messages_snapshot),
    "timeout_summary": timeout_summary,
    "terminal_blocker": terminal_blocker,
    "session_status": status_map.get(run.session_id) if isinstance(status_map, dict) else None,
    "diff": diff,
    "diff_evidence": diff_evidence,
    "scillm_metadata": spec.scillm_metadata,
    "skills": receipt.as_dict(),
    "mcp_requested": list(spec.mcp),
    "artifacts": run.artifact_summary(),
    "human_monitor": run.human_monitor,
    "project_agent_message": project_agent_message,
  }
  result = _apply_patch_delegate_status(result, spec=spec, run=run, diff_evidence=diff_evidence)
  run.write_result(result)
  run.write_status(
    state=result_status,
    phase="provider_error" if provider_error else "timed_out",
    timeout_s=timeout_s,
    timeout_summary=timeout_summary,
    terminal_blocker=terminal_blocker,
    patch_delegate_status=result.get("patch_delegate_status"),
    patch_delegate_reason=result.get("patch_delegate_reason"),
  )
  run.emit(
    "run_provider_error" if provider_error else "run_timeout",
    timeout_s=timeout_s,
    primary_reason=terminal_blocker.get("primary_reason"),
    message_count=timeout_summary.get("message_count", 0),
    assistant_excerpt_chars=len(partial_assistant_text),
  )
  return result


async def _execute_run(
  run: OpenCodeServeRun,
  spec: OpenCodeRunRequest,
  *,
  skill_receipt: SkillViewReceipt | None = None,
) -> dict[str, Any]:
  settings = load_opencode_serve_settings()
  timeout_s = float(spec.timeout_s or settings.timeout_s)
  default_model = os.environ.get("SCILLM_OPENCODE_SERVE_DEFAULT_MODEL", "").strip() or None
  model = spec.model or default_model
  deadline = time.monotonic() + timeout_s
  parts = spec.parts if spec.parts is not None else text_parts(spec.prompt)
  receipt = skill_receipt or SkillViewReceipt(
    skills_requested=tuple(),
    skills_materialized=tuple(),
    skills_missing=tuple(),
    skill_view_dir=None,
  )
  system = merge_system_prompt(spec.system, build_skills_system_overlay(receipt))

  try:
    async with OpenCodeServeClient(settings) as client:
      before_snapshot = _filesystem_snapshot(run.directory)
      run.emit("filesystem_snapshot_before", file_count=len(before_snapshot))
      for mcp_name in spec.mcp:
        name = str(mcp_name).strip()
        if name:
          try:
            await client.register_mcp(name)
            run.emit("mcp_registered", mcp=name)
          except ProxyError as exc:
            run.emit("mcp_register_failed", mcp=name, error=str(exc))
      if run.human_monitor is None:
        run.human_monitor = build_human_monitor(
          run=run,
          scillm_base_url=_scillm_public_base_url(None),
          opencode_settings=settings,
        )
      run.emit(
        "session_ready",
        opencode_url=settings.base_url,
        cwd=spec.cwd,
        opencode_workspace_url=run.human_monitor.get("opencode_workspace_url"),
      )
      run.write_status(state="running", phase="prompting", cwd=spec.cwd, human_monitor=run.human_monitor)

      remaining = deadline - time.monotonic()
      if remaining <= 0:
        return await _timeout_run_result(run, spec, receipt=receipt, timeout_s=timeout_s)

      sync_message: dict[str, Any] | None = None
      first_delta_emitted = False

      def _emit_first_delta_once(message: dict[str, Any] | None, text: str = "") -> None:
        nonlocal first_delta_emitted
        if first_delta_emitted or message is None:
          return
        delta_text, _delta_message, delta = _first_assistant_or_tool_delta([message])
        if not delta_text and text:
          delta_text = text
          delta["assistant_chars"] = len(text)
        if not delta_text and not delta.get("tool_count"):
          return
        first_delta_emitted = True
        run.emit("first_assistant_or_tool_delta", **delta)

      def _emit_scope_violation_once(pending_tools: list[dict[str, Any]]) -> bool:
        violations = _tool_scope_violation_rows(pending_tools, run_directory=run.directory)
        if not violations:
          return False
        first = violations[0].get("scope_violation") if isinstance(violations[0], dict) else {}
        run.emit(
          "assistant_tool_scope_violation",
          pending_tool_count=len(pending_tools),
          violation_count=len(violations),
          tool=violations[0].get("tool") if isinstance(violations[0], dict) else None,
          input_key=first.get("input_key") if isinstance(first, dict) else None,
          input_path=first.get("input_path") if isinstance(first, dict) else None,
          resolved_path=first.get("resolved_path") if isinstance(first, dict) else None,
          allowed_root=first.get("allowed_root") if isinstance(first, dict) else None,
        )
        return True

      async def _deliver_prompt() -> dict[str, Any] | None:
        run.emit("prompt_delivery_started", delivery="sync" if spec.wait else "async")
        if spec.wait:
          payload = await client.send_message(
            run.session_id,
            agent=run.agent,
            model=model,
            parts=parts,
            system=system,
            directory=run.directory,
          )
          run.emit("prompt_submitted_to_opencode", delivery="sync", response_returned=True)
          run.emit("message_completed", delivery="sync")
          return payload
        await client.send_prompt_async(
          run.session_id,
          agent=run.agent,
          model=model,
          parts=parts,
          system=system,
          directory=run.directory,
        )
        run.emit("prompt_submitted_to_opencode", delivery="async", response_returned=False)
        run.emit("prompt_async_sent", experimental=False)
        return None

      sync_message = await asyncio.wait_for(_deliver_prompt(), timeout=remaining)

      remaining = deadline - time.monotonic()
      if remaining <= 0:
        return await _timeout_run_result(run, spec, receipt=receipt, timeout_s=timeout_s)

      assistant_text, message = ("", None)
      if isinstance(sync_message, dict):
        sync_excerpt = _extract_message_excerpt(sync_message)
        assistant_text = _extract_message_text_parts(sync_message) or extract_assistant_text(sync_message)
        if assistant_text or sync_excerpt or _tool_rows(sync_message):
          message = sync_message
          _emit_first_delta_once(message, assistant_text or sync_excerpt)
          pending_tools = _pending_tool_rows(message)
          if pending_tools and _emit_scope_violation_once(pending_tools):
            return await _timeout_run_result(run, spec, receipt=receipt, timeout_s=timeout_s)
          if pending_tools or _awaiting_terminal_text_after_tools(message):
            assistant_text = ""
          elif not assistant_text and _extract_message_excerpt(message):
            run.emit("assistant_waiting_for_terminal_text", reason="non_terminal_assistant_delta")
      if not spec.wait:
        delta_text, message, delta = await _wait_for_first_assistant_or_tool_delta(
          client,
          run,
          deadline=deadline,
        )
        if message is None:
          run.emit("prompt_delivery_stalled", reason="no_assistant_or_tool_delta_before_timeout")
          return await _timeout_run_result(run, spec, receipt=receipt, timeout_s=timeout_s)
        first_delta_emitted = True
        run.emit("first_assistant_or_tool_delta", **delta)
        assistant_text = _extract_message_text_parts(message)
        pending_tools = _pending_tool_rows(message)
        if pending_tools and _emit_scope_violation_once(pending_tools):
          return await _timeout_run_result(run, spec, receipt=receipt, timeout_s=timeout_s)
        if pending_tools or _awaiting_terminal_text_after_tools(message):
          assistant_text = ""
        elif not assistant_text and delta_text:
          run.emit("assistant_waiting_for_terminal_text", reason="non_terminal_assistant_delta")

      status_map = await _poll_until_idle(client, run.session_id, deadline=deadline, directory=run.directory)

      from scillm.proxy.opencode_serve_dialog import drain_pending_dialog

      while True:
        if not assistant_text:
          while time.monotonic() < deadline:
            assistant_text, message, pending_tools = await _latest_assistant_message(
              client, run.session_id, directory=run.directory
            )
            if pending_tools:
              if _emit_scope_violation_once(pending_tools):
                return await _timeout_run_result(run, spec, receipt=receipt, timeout_s=timeout_s)
              run.emit("assistant_tool_pending", pending_tool_count=len(pending_tools))
              await asyncio.sleep(1.0)
              status_map = await client.session_status_map()
              continue
            if _awaiting_terminal_text_after_tools(message):
              run.emit("assistant_waiting_for_terminal_text")
              await asyncio.sleep(1.0)
              status_map = await client.session_status_map()
              continue
            if assistant_text:
              _emit_first_delta_once(message, assistant_text)
              break
            if not session_is_busy(status_map, run.session_id):
              status_map = await client.session_status_map()
              if not session_is_busy(status_map, run.session_id):
                break
            await asyncio.sleep(1.0)
            status_map = await client.session_status_map()
        if not assistant_text:
          return await _timeout_run_result(run, spec, receipt=receipt, timeout_s=timeout_s)
        # A nudge queued while the turn was active gates terminal acceptance:
        # replay it as a real prompt and collect the child's reaction (issue #13).
        queued_turns = drain_pending_dialog(run)
        combined = "\n\n".join(
          str(t.get("text") or "") for t in queued_turns if str(t.get("text") or "").strip()
        )
        if combined.strip() and time.monotonic() < deadline:
          await client.send_message(
            run.session_id,
            parts=text_parts(combined),
            no_reply=False,
            directory=run.directory,
            agent=run.agent or None,
          )
          run.emit("dialog.queued_turn_delivered", turn_count=len(queued_turns))
          assistant_text = ""
          await asyncio.sleep(0.2)
          status_map = await client.session_status_map()
          continue
        break
      if not _patch_delegate_terminal_sentinel(assistant_text):
        assistant_text, follow_message = await _maybe_patch_delegate_followup(
          client,
          run,
          spec,
          model=model,
          assistant_text=assistant_text,
          deadline=deadline,
        )
        if follow_message is not None:
          message = follow_message
      try:
        _latest_messages, _latest_text, latest_message = await _snapshot_run_thread(client, run)
      except Exception:
        latest_message = message
      if _awaiting_terminal_text_after_tools(latest_message):
        run.emit("assistant_waiting_for_terminal_text", reason="final_snapshot_tool_call_finish")
        return await _timeout_run_result(
          run,
          spec,
          receipt=receipt,
          timeout_s=timeout_s,
          partial_assistant_text=assistant_text,
        )
      diff, diff_evidence = await _diff_with_fallback(client, run, before_snapshot=before_snapshot)

  except asyncio.TimeoutError:
    return await _timeout_run_result(run, spec, receipt=receipt, timeout_s=timeout_s)
  except ProxyError as exc:
    if exc.error_type == "timeout":
      return await _timeout_run_result(run, spec, receipt=receipt, timeout_s=timeout_s)
    return await _disconnect_run_result(
      run,
      spec,
      receipt=receipt,
      timeout_s=timeout_s,
      http_error=str(exc),
      phase=_read_run_phase(run),
      cause="opencode_provider_error",
    )
  except httpx.HTTPError as exc:
    return await _disconnect_run_result(
      run,
      spec,
      receipt=receipt,
      timeout_s=timeout_s,
      http_error=str(exc),
      phase=_read_run_phase(run),
    )
  except Exception as exc:
    return await _disconnect_run_result(
      run,
      spec,
      receipt=receipt,
      timeout_s=timeout_s,
      http_error=str(exc),
      phase=_read_run_phase(run),
      cause="opencode_run_failed",
    )

  lineage = {
    "parent_session_id": spec.fork_from_session_id,
    "fork_at_message_id": spec.fork_at_message_id,
  }

  result = {
    "schema": "scillm.opencode_run.result.v1",
    "run_id": run.run_id,
    "agent": run.agent,
    "logical_agent": spec.agent or None,
    "session_id": run.session_id,
    "session_lineage": lineage if any(lineage.values()) else None,
    "status": "completed",
    "timeout_s": timeout_s,
    "assistant_text": assistant_text,
    "collaboration_item": _collaboration_item(
      agent=run.agent,
      logical_agent=spec.agent or None,
      model=model,
      response=assistant_text,
      status="completed",
    ),
    "message": message,
    "session_status": status_map.get(run.session_id) if isinstance(status_map, dict) else None,
    "diff": diff,
    "diff_evidence": diff_evidence,
    "scillm_metadata": spec.scillm_metadata,
    "skills": receipt.as_dict(),
    "mcp_requested": list(spec.mcp),
    "artifacts": run.artifact_summary(),
    "human_monitor": run.human_monitor,
    "project_agent_message": (
      "OpenCode chat output is evidence only. Harness validators decide PASS / NEEDS_CHANGES / BLOCKED."
    ),
  }
  result = _apply_patch_delegate_status(result, spec=spec, run=run, diff_evidence=diff_evidence)
  run.write_result(result)
  run.write_status(
    state="completed",
    phase="done",
    cwd=spec.cwd,
    patch_delegate_status=result.get("patch_delegate_status"),
    patch_delegate_reason=result.get("patch_delegate_reason"),
  )
  run.emit(
    "run_completed",
    assistant_chars=len(assistant_text),
    patch_delegate_status=result.get("patch_delegate_status"),
    patch_delegate_reason=result.get("patch_delegate_reason"),
  )
  return result



async def _acquire_session(
  client: "OpenCodeServeClient",
  spec: OpenCodeRunRequest,
  *,
  agent: str,
  run_id: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
  """Create or fork an OpenCode session; return (session_payload, lineage)."""
  directory = _resolve_run_directory(spec.cwd)
  if spec.fork_from_session_id:
    parent = spec.fork_from_session_id.strip()
    if not directory:
      parent_payload = await client.get_session(parent)
      parent_dir = parent_payload.get("directory")
      if isinstance(parent_dir, str) and parent_dir.strip():
        directory = parent_dir.strip()
    forked = await client.fork_session(
      parent,
      message_id=spec.fork_at_message_id,
      directory=directory,
    )
    lineage = {
      "parent_session_id": parent,
      "fork_at_message_id": spec.fork_at_message_id,
      "directory": directory,
    }
    return forked, lineage
  session = await client.create_session(
    title=spec.title or f"scillm {agent} {run_id}",
    directory=directory,
  )
  return session, None


def create_opencode_serve_router(check_auth: AuthCheck) -> APIRouter:
  router = APIRouter()

  async def auth(request: Request) -> str:
    err = check_auth(request)
    if err:
      raise ProxyError(401, err, "authentication_error")
    if request.method != "GET" and not request.headers.get("x-caller-skill", "").strip():
      raise ProxyError(400, "X-Caller-Skill header is required", "caller_skill_required")
    return request.headers.get("authorization", "")

  @router.get("/opencode/health")
  async def opencode_health(request: Request, full: bool = False) -> JSONResponse:
    await auth(request)
    settings = load_opencode_serve_settings()
    async with OpenCodeServeClient(settings) as client:
      health = await client.health()
      agents = await client.list_agents() if full else []
    env_url = (os.environ.get("OPENCODE_SERVER_URL") or "").strip()
    managed_url = (os.environ.get("SCILLM_OPENCODE_SERVE_URL") or "").strip()
    warnings: list[str] = []
    if managed_url and env_url and managed_url.rstrip("/") != env_url.rstrip("/"):
      warnings.append(
        f"OPENCODE_SERVER_URL ({env_url}) differs from SCILLM_OPENCODE_SERVE_URL ({managed_url}); "
        "scillm uses the managed URL."
      )
    if full:
      agent_names = [a.get("name") for a in agents if isinstance(a, dict) and a.get("name")]
      agent_catalog_source = "opencode_agent_endpoint"
    else:
      agent_names = list(DEFAULT_OPENCODE_AGENT_NAMES)
      agent_catalog_source = "static_default"
    payload = {
        "schema": "scillm.opencode_health.v1",
        "status": "ok",
        "full": full,
        "opencode_url": settings.base_url,
        "opencode_url_managed": managed_url or None,
        "opencode_url_env": env_url or None,
        "warnings": warnings,
        "health": health,
        "agents": agent_names,
        "agent_count": len(agent_names),
        "agent_catalog_source": agent_catalog_source,
        "debugger_agent": debugger_agent_name(),
        "debugger_agent_available": debugger_agent_name() in agent_names,
      }
    if full:
      payload["agents_full"] = agents
    return JSONResponse(payload)


  @router.get("/opencode/serve/runtime")
  async def opencode_serve_runtime_status(request: Request) -> JSONResponse:
    await auth(request)
    payload = await inspect_opencode_serve_runtime()
    return JSONResponse(payload)

  @router.post("/opencode/serve/restart")
  async def opencode_serve_runtime_restart(request: Request) -> JSONResponse:
    await auth(request)
    payload = await restart_opencode_serve_runtime()
    return JSONResponse(payload)

  @router.post("/opencode/serve/up")
  async def opencode_serve_runtime_up(request: Request) -> JSONResponse:
    await auth(request)
    payload = await restart_opencode_serve_runtime(ensure_up=True)
    return JSONResponse(payload)

  @router.get("/opencode/agents")
  async def opencode_agents(request: Request) -> JSONResponse:
    await auth(request)
    async with OpenCodeServeClient() as client:
      agents = await client.list_agents()
    return JSONResponse({"schema": "scillm.opencode_agents.v1", "agents": agents})


  async def _start_run(spec: OpenCodeRunRequest, request: Request, *, default_agent: str | None = None) -> JSONResponse:
    await auth(request)
    logical_agent = (spec.agent or default_agent or debugger_agent_name()).strip()
    if not logical_agent:
      raise ProxyError(400, "agent is required", "invalid_request_error")
    settings_peek = load_opencode_serve_settings()
    async with OpenCodeServeClient(settings_peek) as peek_client:
      available = [a.get("name") for a in await peek_client.list_agents() if isinstance(a, dict)]
    agent = logical_agent
    debugger_overlay: str | None = None
    if logical_agent == debugger_agent_name():
      runtime = debugger_runtime_agent(available_agents=available)
      if runtime != logical_agent:
        agent = runtime
        debugger_overlay = load_debugger_system_prompt()

    run_id = _safe_id(spec.run_id or f"oc-{uuid.uuid4().hex[:12]}")
    caller = request.headers.get("x-caller-skill", "scillm-opencode-serve")

    if debugger_overlay:
      spec = spec.model_copy(update={"system": merge_system_prompt(debugger_overlay, spec.system)})

    # Provisional status.json BEFORE session acquisition: the run must appear
    # in the run indexes from the moment it exists, even if opencode session
    # creation hangs (issue #10 — live run invisible to the Transport UI).
    provisional_dir = _artifact_root() / run_id
    provisional_dir.mkdir(parents=True, exist_ok=True)
    (provisional_dir / "status.json").write_text(
      json.dumps(
        {
          "schema": "scillm.opencode_run.status.v1",
          "run_id": run_id,
          "agent": agent,
          "session_id": "",
          "caller_skill": caller,
          "updated_at": _now(),
          "state": "running",
          "phase": "acquiring_session",
        },
        indent=2,
      ),
      encoding="utf-8",
    )

    session_lineage: dict[str, Any] | None = None
    async with OpenCodeServeClient() as client:
      session, session_lineage = await _acquire_session(client, spec, agent=agent, run_id=run_id)
      session_id = _session_id(session)
      if not session_id:
        raise ProxyError(502, "opencode serve returned session without id", "provider_error")
      resolved_cwd = _resolve_run_directory(spec.cwd)
      actual_dir = session.get("directory") if isinstance(session.get("directory"), str) else None
      if resolved_cwd and actual_dir and actual_dir != resolved_cwd:
        raise ProxyError(
          502,
          f"opencode session directory mismatch: requested {resolved_cwd}, got {actual_dir}",
          "provider_error",
        )

    run = OpenCodeServeRun(
      run_id=run_id,
      artifact_root=_artifact_root(),
      caller_skill=caller,
      agent=agent,
      session_id=session_id,
      request_payload=spec.model_dump(mode="json"),
      directory=resolved_cwd,
    )
    if logical_agent != agent:
      run.emit("debugger_agent_fallback", logical_agent=logical_agent, runtime_agent=agent)
    if session_lineage:
      run.emit("session_forked", **session_lineage)
    skill_receipt = materialize_skill_view(run_id=run.run_id, skills=spec.skills or None)
    run.emit("skill_view_ready", skills=skill_receipt.as_dict())

    settings = load_opencode_serve_settings()
    session_title = None
    session_slug = None
    if isinstance(session, dict):
      raw_title = session.get("title")
      if isinstance(raw_title, str) and raw_title.strip():
        session_title = raw_title.strip()
      raw_slug = session.get("slug")
      if isinstance(raw_slug, str) and raw_slug.strip():
        session_slug = raw_slug.strip()
    run.human_monitor = build_human_monitor(
      run=run,
      scillm_base_url=_scillm_public_base_url(request),
      opencode_settings=settings,
      session_title=session_title,
      session_slug=session_slug,
    )
    run.emit(
      "human_monitor_ready",
      scillm_chat_monitor_url=run.human_monitor.get("scillm_chat_monitor_url"),
      opencode_workspace_url=run.human_monitor.get("opencode_workspace_url"),
      opencode_session_api_url=run.human_monitor.get("opencode_session_api_url"),
    )
    run.emit("run_started", agent=agent, cwd=spec.cwd, directory=resolved_cwd)
    run.write_status(
      state="running",
      phase="created",
      cwd=spec.cwd,
      directory=resolved_cwd,
      skills=skill_receipt.as_dict(),
      human_monitor=run.human_monitor,
    )

    async with _ACTIVE_LOCK:
      _ACTIVE_RUNS[run.run_id] = run

    timeout_s = float(spec.timeout_s or settings.timeout_s)

    async def _ensure_terminal_result(
      *,
      http_error: str | None = None,
      cause: str = "opencode_serve_response_disconnected",
    ) -> dict[str, Any]:
      if run.result_path.is_file():
        try:
          loaded = json.loads(run.result_path.read_text(encoding="utf-8"))
          if isinstance(loaded, dict):
            return loaded
        except json.JSONDecodeError:
          pass
      status_payload: dict[str, Any] = {}
      if run.status_path.is_file():
        try:
          status_payload = json.loads(run.status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
          status_payload = {}
      if _run_is_terminal(status_payload) and run.result_path.is_file():
        return json.loads(run.result_path.read_text(encoding="utf-8"))
      return await _disconnect_run_result(
        run,
        spec,
        receipt=skill_receipt,
        timeout_s=timeout_s,
        http_error=http_error,
        phase=_read_run_phase(run) or str(status_payload.get("phase") or ""),
        cause=cause,
      )

    async def _run_to_terminal() -> dict[str, Any]:
      result: dict[str, Any] | None = None
      run_terminalized = False
      try:
        result = await _execute_run(run, spec, skill_receipt=skill_receipt)
        run_terminalized = True
      except asyncio.CancelledError:
        result = await asyncio.shield(
          _ensure_terminal_result(
            http_error="Server disconnected without sending a response.",
            cause="client_disconnected",
          )
        )
        run_terminalized = True
      except Exception as exc:
        result = await _ensure_terminal_result(http_error=str(exc), cause="opencode_run_failed")
        run_terminalized = True
      finally:
        if not run_terminalized:
          try:
            result = await asyncio.shield(
              _ensure_terminal_result(
                http_error="run_finalize_guard",
                cause="run_finalize_guard",
              )
            )
            run_terminalized = True
          except Exception as finalize_exc:
            run.emit("run_finalize_guard_failed", error=str(finalize_exc))
        if spec.cleanup_session:
          try:
            async with OpenCodeServeClient() as client:
              outcome = await client.kill_session(run.session_id)
            run.emit("session_cleanup", **outcome)
          except Exception as cleanup_exc:
            run.emit("session_cleanup_failed", error=str(cleanup_exc))
        if spec.cleanup_skill_view:
          cleanup_skill_view(skill_receipt)
          run.emit("skill_view_cleaned")
        async with _ACTIVE_LOCK:
          _ACTIVE_RUNS.pop(run.run_id, None)
      return _enrich_run_response(result or {})

    if not spec.wait:
      run.emit("run_receipt_returned", wait=False)
      task = asyncio.create_task(_run_to_terminal())

      def _background_done(done: asyncio.Task[dict[str, Any]]) -> None:
        try:
          done.result()
        except asyncio.CancelledError:
          run.emit("background_run_cancelled")
        except Exception as exc:
          run.emit("background_run_failed", error=str(exc))

      task.add_done_callback(_background_done)
      return JSONResponse(_run_receipt(run, spec, timeout_s=timeout_s, skills=skill_receipt))

    result = await _run_to_terminal()
    return JSONResponse(result)

  @router.get("/opencode/events")
  async def opencode_events_stream(
    request: Request,
    heartbeat_s: float = 15.0,
    timeout_s: float | None = None,
  ) -> StreamingResponse:
    """Proxy OpenCode ``GET /event`` as SSE (live bus + server.connected).

    Distinct from ``GET /opencode/runs/{run_id}/events`` which tails scillm's
  artifact ``events.jsonl`` for a completed run.
    """
    await auth(request)

    async def upstream():
      async with OpenCodeServeClient() as client:
        async for chunk in client.iter_event_stream():
          yield chunk

    stream = sse_liveness_wrapper(
      upstream(),
      overall_timeout_s=timeout_s,
      heartbeat_interval_s=heartbeat_s,
      progress_events=True,
    )
    return StreamingResponse(stream, media_type="text/event-stream", headers=SSE_HEADERS)

  @router.post("/opencode/runs")
  async def opencode_create_run(spec: OpenCodeRunRequest, request: Request) -> JSONResponse:
    return await _start_run(spec, request)

  @router.post("/opencode/agents/{agent_name}/runs")
  async def opencode_create_agent_run(agent_name: str, spec: OpenCodeRunRequest, request: Request) -> JSONResponse:
    return await _start_run(spec, request, default_agent=agent_name)

  @router.post("/opencode/serve/debugger/run")
  async def opencode_debugger_run(spec: OpenCodeRunRequest, request: Request) -> JSONResponse:
    """Convenience alias for the default debugger agent profile."""
    return await _start_run(spec, request, default_agent=debugger_agent_name())

  def _load_run(run_id: str) -> OpenCodeServeRun:
    safe = _safe_id(run_id)
    active = _ACTIVE_RUNS.get(safe)
    if active is not None:
      return active
    status_path = _artifact_root() / safe / "status.json"
    if not status_path.exists():
      raise ProxyError(404, f"opencode run not found: {safe}", "not_found")
    status = json.loads(status_path.read_text(encoding="utf-8"))
    session_id = str(status.get("session_id") or "")
    agent = str(status.get("agent") or debugger_agent_name())
    restored = OpenCodeServeRun(
      run_id=safe,
      artifact_root=_artifact_root(),
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
    monitor = status.get("human_monitor")
    if isinstance(monitor, dict):
      restored.human_monitor = monitor
    return restored

  @router.get("/opencode/runs/run-index")
  async def opencode_serve_run_index(request: Request) -> JSONResponse:
    await auth(request)
    from scillm.proxy.opencode_serve_dialog import list_serve_run_index

    return JSONResponse(
      {
        "schema": "scillm.opencode_serve.run_index.v1",
        "runs": list_serve_run_index(),
      }
    )

  @router.get("/opencode/runs/{run_id}/dialog")
  async def opencode_serve_run_dialog(request: Request, run_id: str) -> JSONResponse:
    await auth(request)
    from scillm.proxy.opencode_serve_dialog import (
      build_serve_dialog_response_async,
      load_serve_run,
    )

    run = load_serve_run(run_id)
    return JSONResponse(await build_serve_dialog_response_async(run))

  @router.post("/opencode/runs/{run_id}/dialog")
  async def opencode_serve_run_dialog_post(request: Request, run_id: str) -> JSONResponse:
    await auth(request)
    from scillm.proxy.opencode_serve_dialog import load_serve_run, post_serve_dialog_message

    run = load_serve_run(run_id)
    body = await request.json()
    if not isinstance(body, dict):
      raise ProxyError(400, "dialog body must be a JSON object", "invalid_request")
    speaker = str(body.get("speaker") or "Project agent")
    text = str(body.get("body") or body.get("text") or "")
    result = await post_serve_dialog_message(run, speaker=speaker, body=text)
    return JSONResponse(result)

  @router.get("/opencode/runs/{run_id}/events/stream")
  async def opencode_serve_run_events_stream(
    request: Request,
    run_id: str,
    after_line: int = 0,
    timeout_s: float = 120,
  ) -> StreamingResponse:
    await auth(request)
    from scillm.proxy.opencode_serve_dialog import (
      load_serve_run,
      read_serve_events,
      serve_stream_event_sse,
    )

    run = load_serve_run(run_id)
    deadline = time.monotonic() + max(5.0, min(timeout_s, 600.0))

    async def _gen():
      cursor = max(0, after_line)
      while time.monotonic() < deadline:
        batch, cursor = read_serve_events(run, after_line=cursor)
        for row in batch:
          yield serve_stream_event_sse(row)
        if run.run_id not in _ACTIVE_RUNS:
          break
        await asyncio.sleep(1.0)
      yield serve_stream_event_sse(
        {
          "schema": "scillm.opencode_transport.event.v1",
          "event_type": "stream.end",
          "transport_run_id": run.run_id,
          "subagent_run_id": run.run_id,
        }
      )

    return StreamingResponse(_gen(), media_type="text/event-stream", headers=SSE_HEADERS)

  @router.get("/opencode/runs/{run_id}")
  async def opencode_get_run(request: Request, run_id: str) -> JSONResponse:
    await auth(request)
    run = _load_run(run_id)
    await _maybe_finalize_orphan_run(run)
    status = json.loads(run.status_path.read_text(encoding="utf-8")) if run.status_path.exists() else {}
    result = json.loads(run.result_path.read_text(encoding="utf-8")) if run.result_path.exists() else None
    human_monitor = status.get("human_monitor")
    if human_monitor is None and isinstance(result, dict):
      human_monitor = result.get("human_monitor")
    if human_monitor is None and run.human_monitor is not None:
      human_monitor = run.human_monitor
    return JSONResponse(
      _enrich_run_response(
        {
          "schema": "scillm.opencode_run.v1",
          "run_id": run.run_id,
          "agent": run.agent,
          "session_id": run.session_id,
          "status": status,
          "result": result,
          "human_monitor": human_monitor,
          "artifacts": run.artifact_summary(),
        }
      )
    )

  @router.get("/opencode/runs/{run_id}/status")
  async def opencode_run_status(request: Request, run_id: str) -> JSONResponse:
    await auth(request)
    run = _load_run(run_id)
    await _maybe_finalize_orphan_run(run)
    if not run.status_path.exists():
      raise ProxyError(404, f"status not found for run {run.run_id}", "not_found")
    return JSONResponse(json.loads(run.status_path.read_text(encoding="utf-8")))

  @router.get("/opencode/runs/{run_id}/events")
  async def opencode_run_events(request: Request, run_id: str, tail: int = 200) -> JSONResponse:
    await auth(request)
    run = _load_run(run_id)
    if not run.events_path.exists():
      raise ProxyError(404, f"events not found for run {run.run_id}", "not_found")
    lines = run.events_path.read_text(errors="replace").splitlines()
    tail = max(1, min(tail, 5000))
    events: list[dict[str, Any]] = []
    for line in lines[-tail:]:
      if line.strip():
        try:
          parsed = json.loads(line)
        except json.JSONDecodeError:
          continue
        if isinstance(parsed, dict):
          events.append(parsed)
    return JSONResponse({"run_id": run.run_id, "events": events})

  @router.get("/opencode/runs/{run_id}/messages")
  async def opencode_run_messages(
    request: Request, run_id: str, limit: int = 50, live: bool = False
  ) -> JSONResponse:
    await auth(request)
    run = _load_run(run_id)
    if live:
      messages = await _fetch_live_messages(run, limit=limit)
      return JSONResponse(
        {
          "run_id": run.run_id,
          "session_id": run.session_id,
          "source": "opencode_live",
          "messages": messages,
        }
      )
    snapshot_path = run.run_dir / "messages_snapshot.json"
    if snapshot_path.exists():
      snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
      return JSONResponse(
        {
          "run_id": run.run_id,
          "session_id": run.session_id,
          "source": "artifact_snapshot",
          "messages": snapshot.get("messages", []),
        }
      )
    if run.result_path.exists():
      result = json.loads(run.result_path.read_text(encoding="utf-8"))
      message = result.get("message") if isinstance(result.get("message"), dict) else None
      if message is not None:
        return JSONResponse(
          {
            "run_id": run.run_id,
            "session_id": run.session_id,
            "source": "artifact_result",
            "messages": [message],
          }
        )
      collaboration_item = result.get("collaboration_item") if isinstance(result.get("collaboration_item"), dict) else {}
      response = collaboration_item.get("response") if isinstance(collaboration_item.get("response"), str) else ""
      if response:
        return JSONResponse(
          {
            "run_id": run.run_id,
            "session_id": run.session_id,
            "source": "artifact_collaboration_item",
            "messages": [
              {
                "info": {
                  "role": "assistant",
                  "id": f"{run.run_id}-collaboration-item",
                  "agent": collaboration_item.get("person_or_persona_name"),
                  "model": collaboration_item.get("model"),
                  "thread_type": collaboration_item.get("thread_type"),
                },
                "parts": [{"type": "text", "text": response}],
              }
            ],
          }
        )
    async with OpenCodeServeClient() as client:
      messages = await client.list_messages(run.session_id, limit=limit, directory=run.directory)
    return JSONResponse({"run_id": run.run_id, "session_id": run.session_id, "source": "opencode_live", "messages": messages})

  @router.get("/opencode/runs/{run_id}/monitor")
  async def opencode_run_monitor(
    request: Request,
    run_id: str,
    token: str | None = None,
    refresh_s: int = 3,
    limit: int = 200,
  ) -> HTMLResponse:
    run = _load_run(run_id)
    if not _monitor_auth_ok(request, run, token):
      await auth(request)
    messages = await _load_run_messages_for_monitor(run, limit=limit)
    status: dict[str, Any] = {}
    if run.status_path.exists():
      try:
        status = json.loads(run.status_path.read_text(encoding="utf-8"))
      except json.JSONDecodeError:
        status = {}
    body = _render_chat_monitor_page(
      run=run,
      messages=messages,
      status=status,
      refresh_s=refresh_s,
    )
    return HTMLResponse(content=body)

  @router.get("/opencode/runs/{run_id}/diff")
  async def opencode_run_diff(request: Request, run_id: str, message_id: str | None = None) -> JSONResponse:
    await auth(request)
    run = _load_run(run_id)
    if run.result_path.exists() and not message_id:
      result = json.loads(run.result_path.read_text(encoding="utf-8"))
      if isinstance(result.get("diff"), list):
        return JSONResponse(
          {
            "run_id": run.run_id,
            "session_id": run.session_id,
            "source": "artifact_result",
            "diff": result["diff"],
          }
        )
    async with OpenCodeServeClient() as client:
      diff = await client.diff(run.session_id, message_id=message_id, directory=run.directory)
    return JSONResponse({"run_id": run.run_id, "session_id": run.session_id, "source": "opencode_live", "diff": diff})

  @router.post("/opencode/runs/{run_id}/abort")
  async def opencode_abort_run(request: Request, run_id: str) -> JSONResponse:
    await auth(request)
    run = _load_run(run_id)
    async with OpenCodeServeClient() as client:
      ok = await client.abort(run.session_id)
    run.emit("aborted", ok=ok)
    run.write_status(state="aborted")
    return JSONResponse({"run_id": run.run_id, "session_id": run.session_id, "aborted": ok})

  @router.delete("/opencode/runs/{run_id}")
  async def opencode_delete_run(request: Request, run_id: str) -> JSONResponse:
    await auth(request)
    run = _load_run(run_id)
    async with OpenCodeServeClient() as client:
      ok = await client.delete_session(run.session_id)
    run.emit("session_deleted", ok=ok)
    run.write_status(state="deleted")
    return JSONResponse({"run_id": run.run_id, "session_id": run.session_id, "deleted": ok})



  @router.post("/opencode/sessions/{session_id}/fork")
  async def opencode_fork_session(
    session_id: str,
    spec: OpenCodeForkRequest,
    request: Request,
  ) -> JSONResponse:
    """Fork an OpenCode session; returns the new child session (no prompt run)."""
    await auth(request)
    async with OpenCodeServeClient() as client:
      child = await client.fork_session(session_id, message_id=spec.message_id)
    child_id = _session_id(child)
    return JSONResponse(
      {
        "schema": "scillm.opencode_session_fork.v1",
        "parent_session_id": session_id,
        "child_session_id": child_id,
        "fork_at_message_id": spec.message_id,
        "session": child,
        "project_agent_message": (
          "Use the child session id in a follow-up POST /v1/scillm/opencode/runs with "
          "fork_from_session_id only when re-forking; normally pass prompt on a new run "
          "or set cleanup_session false on parent before fork."
        ),
      }
    )

  @router.get("/opencode/sessions/{session_id}/children")
  async def opencode_session_children(request: Request, session_id: str) -> JSONResponse:
    await auth(request)
    safe_id = _safe_id(session_id)
    async with OpenCodeServeClient() as client:
      children = await client.list_session_children(safe_id)
    return JSONResponse(
      {
        "schema": "scillm.opencode_session_children.v1",
        "session_id": safe_id,
        "children": children,
        "count": len(children),
      }
    )

  @router.post("/opencode/sessions/{session_id}/summarize")
  async def opencode_summarize_session(
    session_id: str,
    spec: OpenCodeSummarizeRequest,
    request: Request,
  ) -> JSONResponse:
    await auth(request)
    safe_id = _safe_id(session_id)
    async with OpenCodeServeClient() as client:
      ok = await client.summarize(
        safe_id,
        provider_id=spec.provider_id.strip(),
        model_id=spec.model_id.strip(),
      )
    return JSONResponse(
      {
        "schema": "scillm.opencode_session_summarize.v1",
        "session_id": safe_id,
        "provider_id": spec.provider_id,
        "model_id": spec.model_id,
        "ok": ok,
      }
    )

  @router.post("/opencode/sessions/{session_id}/revert")
  async def opencode_revert_session(
    session_id: str,
    spec: OpenCodeRevertRequest,
    request: Request,
  ) -> JSONResponse:
    await auth(request)
    safe_id = _safe_id(session_id)
    async with OpenCodeServeClient() as client:
      ok = await client.revert(
        safe_id,
        message_id=spec.message_id.strip(),
        part_id=spec.part_id.strip() if spec.part_id else None,
      )
    return JSONResponse(
      {
        "schema": "scillm.opencode_session_revert.v1",
        "session_id": safe_id,
        "message_id": spec.message_id,
        "part_id": spec.part_id,
        "ok": ok,
      }
    )

  @router.post("/opencode/sessions/{session_id}/unrevert")
  async def opencode_unrevert_session(request: Request, session_id: str) -> JSONResponse:
    await auth(request)
    safe_id = _safe_id(session_id)
    async with OpenCodeServeClient() as client:
      ok = await client.unrevert(safe_id)
    return JSONResponse(
      {
        "schema": "scillm.opencode_session_unrevert.v1",
        "session_id": safe_id,
        "ok": ok,
      }
    )

  @router.get("/opencode/sessions")
  async def opencode_list_sessions(
    request: Request,
    stale_busy_s: float = 600.0,
    max_idle_age_s: float = 86400.0,
    kill_idle: bool = False,
  ) -> JSONResponse:
    await auth(request)
    protected_ids = _protected_session_ids()
    async with OpenCodeServeClient() as client:
      sessions = await client.list_sessions()
      status_map = await client.session_status_map()
    rows = [
      _enrich_session_row(
        item,
        status_map,
        protected_ids=protected_ids,
        stale_busy_s=stale_busy_s,
        max_idle_age_s=max_idle_age_s,
        kill_idle=kill_idle,
      )
      for item in sessions
    ]
    zombies = [row for row in rows if row.get("zombie_reason")]
    return JSONResponse(
      {
        "schema": "scillm.opencode_sessions.v1",
        "count": len(rows),
        "zombie_count": len(zombies),
        "protected_count": sum(1 for row in rows if row.get("protected")),
        "sessions": rows,
        "zombies": zombies,
      }
    )

  @router.post("/opencode/sessions/purge")
  async def opencode_purge_sessions(spec: OpenCodeSessionPurgeRequest, request: Request) -> JSONResponse:
    await auth(request)
    protected_ids = set() if spec.force else _protected_session_ids()
    async with OpenCodeServeClient() as client:
      sessions = await client.list_sessions()
      status_map = await client.session_status_map()
      by_id = {
        sid: item
        for item in sessions
        if (sid := session_id_from_payload(item))
      }

      targets: list[dict[str, Any]] = []
      if spec.session_ids:
        for raw_id in spec.session_ids:
          session_id = _safe_id(raw_id)
          if session_id in protected_ids:
            targets.append(
              {
                "session_id": session_id,
                "zombie_reason": "protected",
                "skipped": True,
              }
            )
            continue
          payload = by_id.get(session_id, {"id": session_id})
          targets.append(
            {
              "session_id": session_id,
              "zombie_reason": "explicit",
              "session": payload,
            }
          )
      else:
        for item in sessions:
          session_id = session_id_from_payload(item)
          if not session_id:
            continue
          reason = classify_zombie_session(
            item,
            status_map,
            protected_ids=protected_ids,
            stale_busy_s=spec.stale_busy_s,
            max_idle_age_s=spec.max_idle_age_s,
            kill_idle=spec.kill_idle,
          )
          if reason:
            targets.append(
              {
                "session_id": session_id,
                "zombie_reason": reason,
                "session": item,
              }
            )

      killed: list[dict[str, Any]] = []
      skipped: list[dict[str, Any]] = []
      for target in targets:
        if target.get("skipped"):
          skipped.append(target)
          continue
        session_id = str(target["session_id"])
        if spec.dry_run:
          killed.append({**target, "dry_run": True, "aborted": None, "deleted": None})
          continue
        outcome = await client.kill_session(session_id)
        killed.append({**target, "dry_run": False, **outcome})

    return JSONResponse(
      {
        "schema": "scillm.opencode_sessions_purge.v1",
        "dry_run": spec.dry_run,
        "force": spec.force,
        "stale_busy_s": spec.stale_busy_s,
        "max_idle_age_s": spec.max_idle_age_s,
        "kill_idle": spec.kill_idle,
        "target_count": len(targets),
        "killed": killed,
        "skipped": skipped,
        "protected_session_ids": sorted(protected_ids),
      }
    )

  @router.post("/opencode/runs/{run_id}/kill")
  async def opencode_kill_run(request: Request, run_id: str, dry_run: bool = False) -> JSONResponse:
    """Abort/delete the OpenCode session owned by a scillm run (bypasses stale protection)."""
    await auth(request)
    run = _load_run(run_id)
    if not run.session_id:
      raise ProxyError(404, f"run has no session_id: {run.run_id}", "not_found")
    if dry_run:
      return JSONResponse(
        {
          "schema": "scillm.opencode_session_kill.v1",
          "run_id": run.run_id,
          "session_id": run.session_id,
          "dry_run": True,
          "owner_bypass": True,
        }
      )
    async with OpenCodeServeClient() as client:
      outcome = await client.kill_session(run.session_id)
    run.emit("owner_kill", **outcome)
    run.write_status(state="killed", phase="killed")
    return JSONResponse(
      {
        "schema": "scillm.opencode_session_kill.v1",
        "run_id": run.run_id,
        "session_id": run.session_id,
        "dry_run": False,
        "owner_bypass": True,
        **outcome,
      }
    )

  @router.post("/opencode/sessions/{session_id}/kill")
  async def opencode_kill_session(
    request: Request,
    session_id: str,
    force: bool = False,
    dry_run: bool = False,
    run_id: str | None = None,
  ) -> JSONResponse:
    await auth(request)
    safe_id = _safe_id(session_id)
    owner_kill = bool(run_id and _session_owned_by_run(safe_id, run_id))
    protected_ids = set() if force or owner_kill else _protected_session_ids()
    if safe_id in protected_ids and not owner_kill:
      return JSONResponse(
        {
          "schema": "scillm.opencode_session_kill.v1",
          "session_id": safe_id,
          "skipped": True,
          "reason": "protected",
          "protected_session_ids": sorted(protected_ids),
          "project_agent_message": (
            "Session is actively running. POST /opencode/runs/{run_id}/abort or "
            "/opencode/runs/{run_id}/kill, or pass run_id= on this endpoint."
          ),
        },
        status_code=409,
      )
    if dry_run:
      return JSONResponse(
        {
          "schema": "scillm.opencode_session_kill.v1",
          "session_id": safe_id,
          "dry_run": True,
          "aborted": None,
          "deleted": None,
        }
      )
    async with OpenCodeServeClient() as client:
      outcome = await client.kill_session(safe_id)
    return JSONResponse({"schema": "scillm.opencode_session_kill.v1", "dry_run": False, **outcome})

  register_opencode_transport_routes(router, auth)
  return router
