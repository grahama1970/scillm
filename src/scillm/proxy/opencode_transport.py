"""scillm v1 OpenCode transport: parent/child sessions, sync messages, delivery state."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from scillm.proxy.errors import ProxyError
from scillm.harness.patch_delegate_receipt import (
    PATCH_DELEGATE_BLOCKED,
    classify_patch_delegate_result,
)
from scillm.harness.skill_adapters import SkillAdapterError, run_skill_call
from scillm.proxy.opencode_skill_view import (
    build_skills_system_overlay,
    materialize_skill_view,
    merge_system_prompt,
)
from scillm.proxy.opencode_serve import (
    OpenCodeServeClient,
    OpenCodeServeSettings,
    extract_assistant_text,
    extract_text_from_parts,
    session_epoch_s,
    session_id_from_payload,
    session_is_busy,
    text_parts,
)

TRANSPORT_SCHEMA = "scillm.opencode_transport.v1"
OBSERVATION_SCHEMA = "scillm.opencode_transport.observation.v1"
DIALOG_COLLAB_SCHEMA = "scillm.opencode_transport.dialog.v1"
SUBAGENT_SCHEMA = "scillm.subagent_run.v1"
DIALOG_SPEAKER_RE = re.compile(r"^\*\*(.+?)\*\*\s*\n\n", re.DOTALL)

WORKER_SPEAKER_RE = re.compile(r"^worker\s*\(([^)]+)\)\s*$", re.IGNORECASE)
SPAWN_ROLE_RE = re.compile(r"spawned(?:\s+worker)?\s+\*\*([^*]+)\*\*", re.IGNORECASE)
SKILL_SLUG_INLINE_RE = re.compile(r"(?:^|[\s(])(/([a-z][a-z0-9-]*))", re.IGNORECASE)
FORWARD_HUMAN_RE = re.compile(r"forwarding\s+\*\*human\*\*", re.IGNORECASE)
DISPATCH_ROLE_RE = re.compile(r"dispatching\s+\*\*([^*]+)\*\*", re.IGNORECASE)

SPAWN_ATTEMPT_RE = re.compile(r"attempt\s+(\d+)", re.IGNORECASE)

DEFAULT_TRANSPORT_CHILD_SKILLS: tuple[str, ...] = (
    "memory",
    "debugger",
    "dogpile",
    "scillm",
    "best-practices-scillm",
    "best-practices-python",
)

ROLE_SKILL_DEFAULTS: dict[str, tuple[str, ...]] = {
    "debugger": DEFAULT_TRANSPORT_CHILD_SKILLS,
    "reviewer": ("memory", "scillm", "best-practices-scillm"),
    "patch": ("memory", "scillm", "best-practices-scillm"),
}

AGENT_SKILL_DEFAULTS: dict[str, tuple[str, ...]] = {
    "scillm-debugger": DEFAULT_TRANSPORT_CHILD_SKILLS,
    "scillm-worker": ("memory", "scillm", "best-practices-scillm", "best-practices-python"),
}


def subagent_kind_label(role: str) -> str:
    """Human-facing subagent kind for collaboration UI."""
    key = (role or "").strip().lower()
    labels = {
        "debugger": "Debugger",
        "reviewer": "Reviewer",
        "patch": "Patch worker",
        "explore": "Explorer",
        "designer": "Designer",
    }
    if key in labels:
        return labels[key]
    if not key:
        return "Worker"
    return key.replace("_", " ").replace("-", " ").title()


def default_skills_for_child(
    *,
    role: str,
    agent: str,
    skills: list[str] | None = None,
    agent_id: str | None = None,
) -> list[str]:
    if skills:
        return [s.strip().lower() for s in skills if str(s).strip()]
    if agent_id:
        from scillm.proxy.worker_agents import WorkerAgentResolutionError, resolve_worker_agent

        spec = resolve_worker_agent(agent_id)
        if spec and spec.composes:
            return list(spec.composes)
        raise WorkerAgentResolutionError(f"unknown worker agent_id: {agent_id}")
    agent_key = (agent or "").strip().lower()
    if agent_key in AGENT_SKILL_DEFAULTS:
        return list(AGENT_SKILL_DEFAULTS[agent_key])
    role_key = (role or "").strip().lower()
    if role_key in ROLE_SKILL_DEFAULTS:
        return list(ROLE_SKILL_DEFAULTS[role_key])
    return list(DEFAULT_TRANSPORT_CHILD_SKILLS)


def child_summary_dict(child: "ChildAttempt") -> dict[str, Any]:
    from scillm.proxy.worker_agents import resolve_worker_agent

    spec = resolve_worker_agent(child.agent_id) if child.agent_id else None
    kind = spec.title if spec else subagent_kind_label(child.role)
    label_agent = child.agent_id or child.agent
    return {
        "subagent_run_id": child.subagent_run_id,
        "role": child.role,
        "agent_id": child.agent_id,
        "subagent_kind": kind,
        "subagent_label": f"{kind} · {label_agent}",
        "agent": child.agent,
      "mode": child.mode,
      "attempt_id": child.attempt_id,
      "child_session_id": child.child_session_id,
      "delivery_state": child.delivery_state,
      "active": child.active,
      "skills": list(child.skills),
      "skills_materialized": list(child.skills_materialized),
      "skills_missing": list(child.skills_missing),
  }


def active_subagent_dict(state: "TransportState") -> dict[str, Any] | None:
    child = state.active_child()
    if child is None:
        return None
    return child_summary_dict(child)

DELIVERY_CREATED = "created"
DELIVERY_QUEUED = "queued"
DELIVERY_POSTED = "posted"
DELIVERY_DELIVERED = "delivered"
DELIVERY_RUNNING = "running"
DELIVERY_WAITING_PERMISSION = "waiting_permission"
DELIVERY_IDLE_SEEN = "idle_seen"
DELIVERY_ACTED = "acted"
DELIVERY_COMPLETED = "completed"
DELIVERY_FAILED = "failed"
DELIVERY_SUPERSEDED = "superseded"
DELIVERY_ABORTED = "aborted"
DELIVERY_TIMED_OUT = "timed_out"
DELIVERY_BLOCKED = "blocked"
TERMINAL_DELIVERY_STATES = {
    DELIVERY_ABORTED,
    DELIVERY_SUPERSEDED,
    DELIVERY_TIMED_OUT,
    DELIVERY_BLOCKED,
}

PINNED_OPENCODE_VERSION = os.environ.get("SCILLM_OPENCODE_PINNED_VERSION", "1.14.31")


def prompt_async_allowed() -> bool:
    return os.environ.get("SCILLM_OPENCODE_ALLOW_PROMPT_ASYNC", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def message_id_from_payload(message_payload: dict[str, Any]) -> str:
    for key in ("id", "messageID", "message_id"):
        value = message_payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    info = message_payload.get("info")
    if isinstance(info, dict):
        for key in ("id", "messageID", "message_id"):
            value = info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def build_capability_flags(*, health: dict[str, Any], opencode_url: str) -> dict[str, Any]:
    nested = health.get("health") if isinstance(health.get("health"), dict) else health
    version = ""
    if isinstance(nested, dict):
        version = str(nested.get("version") or health.get("version") or "").strip()
    return {
        "schema": "scillm.opencode_capabilities.v1",
        "opencode_url": opencode_url,
        "opencode_version": version or PINNED_OPENCODE_VERSION,
        "sync_message": True,
        "child_sessions": True,
        "session_diff": True,
        "permission_reply": True,
        "event_stream": "sse_with_reasoning",
        "message_stream_default": True,
        "custom_session_metadata": True,
        "prompt_async_core": prompt_async_allowed(),
        "nested_subagent_permissions": False,
        "transport_api": True,
    }


def enrich_event(
    event: dict[str, Any],
    *,
    transport_run_id: str,
    dag_node_id: str = "",
    subagent_run_id: str = "",
    attempt_id: int = 0,
    parent_session_id: str = "",
    child_session_id: str = "",
    message_id: str = "",
    delivery_state: str = "",
    workspace: str = "",
    agent: str = "",
    agent_id: str = "",
    model: str = "",
) -> dict[str, Any]:
    row = dict(event)
    row.setdefault("event_id", f"evt_{uuid.uuid4().hex[:12]}")
    row.setdefault("ts", time.time())
    row.setdefault("source", "scillm-transport")
    row["transport_run_id"] = transport_run_id
    if dag_node_id:
        row["dag_node_id"] = dag_node_id
    if subagent_run_id:
        row["subagent_run_id"] = subagent_run_id
    if attempt_id:
        row["attempt_id"] = attempt_id
    if parent_session_id:
        row["parent_session_id"] = parent_session_id
    if child_session_id:
        row["child_session_id"] = child_session_id
    if message_id:
        row["message_id"] = message_id
    if delivery_state:
        row["delivery_state"] = delivery_state
    if workspace:
        row["workspace"] = workspace
    if agent:
        row["agent"] = agent
    if agent_id:
        row["agent_id"] = agent_id
    if model:
        row["model"] = model
    return row


@dataclass
class ChildAttempt:
    subagent_run_id: str
    role: str
    child_session_id: str
    agent: str
    attempt_id: int
    delivery_state: str = DELIVERY_CREATED
    active: bool = True
    last_message_id: str = ""
    mode: str = "propose_patches"
    agent_id: str = ""
    skills: list[str] = field(default_factory=list)
    skills_materialized: list[str] = field(default_factory=list)
    skills_missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChildAttempt:
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


@dataclass
class TransportState:
    schema: str = TRANSPORT_SCHEMA
    transport_run_id: str = ""
    dag_node_id: str = ""
    parent_session_id: str = ""
    workspace: str = ""
    opencode_url: str = ""
    active_subagent_run_id: str = ""
    children: list[dict[str, Any]] = field(default_factory=list)
    dialog_last_human_message_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TransportState:
        return cls(
            schema=str(data.get("schema") or TRANSPORT_SCHEMA),
            transport_run_id=str(data.get("transport_run_id") or ""),
            dag_node_id=str(data.get("dag_node_id") or ""),
            parent_session_id=str(data.get("parent_session_id") or ""),
            workspace=str(data.get("workspace") or ""),
            opencode_url=str(data.get("opencode_url") or ""),
            active_subagent_run_id=str(data.get("active_subagent_run_id") or ""),
            children=list(data.get("children") or []),
            dialog_last_human_message_id=str(data.get("dialog_last_human_message_id") or ""),
        )

    def active_child(self) -> ChildAttempt | None:
        for row in self.children:
            if not isinstance(row, dict):
                continue
            if row.get("active") and row.get("subagent_run_id") == self.active_subagent_run_id:
                return ChildAttempt.from_dict(row)
        for row in reversed(self.children):
            if isinstance(row, dict) and row.get("active"):
                return ChildAttempt.from_dict(row)
        return None


class TransportStore:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir.resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def run_dir(self, transport_run_id: str) -> Path:
        return self.base_dir / transport_run_id

    def state_path(self, transport_run_id: str) -> Path:
        return self.run_dir(transport_run_id) / "transport_state.json"

    def events_path(self, transport_run_id: str) -> Path:
        return self.run_dir(transport_run_id) / "events.jsonl"

    def subagent_runs_path(self, transport_run_id: str) -> Path:
        return self.run_dir(transport_run_id) / "subagent_runs.jsonl"

    def load(self, transport_run_id: str) -> TransportState:
        path = self.state_path(transport_run_id)
        if not path.is_file():
            raise ProxyError(404, f"transport run {transport_run_id} not found", "not_found")
        return TransportState.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, state: TransportState) -> None:
        run_dir = self.run_dir(state.transport_run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        self.state_path(state.transport_run_id).write_text(
            json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def append_event(self, transport_run_id: str, event: dict[str, Any]) -> None:
        run_dir = self.run_dir(transport_run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        with self.events_path(transport_run_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")

    def append_subagent_run(self, transport_run_id: str, row: dict[str, Any]) -> None:
        run_dir = self.run_dir(transport_run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        with self.subagent_runs_path(transport_run_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def transport_output_base() -> Path:
    raw = os.environ.get("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", "").strip()
    if raw:
        return Path(raw) / "transport"
    return Path(os.environ.get("SCILLM_OPENCODE_TRANSPORT_DIR", ".scillm/opencode-transport"))




def transport_index_bases() -> list[Path]:
    """Candidate artifact roots for run-index (env + common local layouts)."""
    bases: list[Path] = []
    serve = os.environ.get("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", "").strip()
    if serve:
        bases.append(Path(serve) / "transport")
    legacy = os.environ.get("SCILLM_OPENCODE_TRANSPORT_DIR", "").strip()
    if legacy:
        bases.append(Path(legacy))
    project = os.environ.get("SCILLM_PROJECT_ROOT", "").strip()
    roots = [Path(project)] if project else []
    roots.extend([Path.cwd(), Path(__file__).resolve().parents[3]])
    seen: set[str] = set()
    for root in roots:
        for rel in (".scillm/opencode-serve/transport", ".scillm/opencode-transport"):
            candidate = (root / rel).resolve()
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            bases.append(candidate)
    primary = transport_output_base().resolve()
    if str(primary) not in {str(b.resolve()) for b in bases}:
        bases.insert(0, primary)
    out: list[Path] = []
    seen_dirs: set[str] = set()
    for base in bases:
        resolved = base.resolve()
        key = str(resolved)
        if key in seen_dirs:
            continue
        seen_dirs.add(key)
        if resolved.is_dir():
            out.append(resolved)
    return out


def list_transport_run_index() -> list[dict[str, object]]:
    """Scan artifact dirs for run-index UI."""
    by_id: dict[str, dict[str, object]] = {}
    for base in transport_index_bases():
        for ent in base.iterdir():
            if not ent.is_dir():
                continue
            state_path = ent / "transport_state.json"
            if not state_path.is_file():
                continue
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                st = state_path.stat()
                transport_run_id = str(state.get("transport_run_id") or ent.name)
                row = {
                    "transport_run_id": transport_run_id,
                    "run_id": transport_run_id,
                    "id": transport_run_id,
                    "title": state.get("title") or state.get("dag_node_id") or transport_run_id,
                    "dag_node_id": state.get("dag_node_id"),
                    "mtime_ms": int(st.st_mtime * 1000),
                    "updated_at": state.get("updated_at"),
                    "state": state.get("state"),
                    "phase": state.get("phase"),
                    "session_id": state.get("session_id"),
                }
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            prev = by_id.get(row["transport_run_id"])
            if not prev or int(row["mtime_ms"]) > int(prev.get("mtime_ms") or 0):
                by_id[str(row["transport_run_id"])] = row
    try:
        from scillm.proxy.opencode_serve_dialog import list_serve_run_index

        for row in list_serve_run_index():
            run_id = str(row.get("run_id") or row.get("transport_run_id") or row.get("id") or "")
            if not run_id:
                continue
            prev = by_id.get(run_id)
            if not prev or int(row.get("mtime_ms") or 0) > int(prev.get("mtime_ms") or 0):
                by_id[run_id] = row
    except Exception:
        pass
    rows = list(by_id.values())
    rows.sort(key=lambda r: int(r.get("mtime_ms") or 0), reverse=True)
    return rows






def parent_dialog_enabled() -> bool:
    raw = os.environ.get("SCILLM_OPENCODE_TRANSPORT_PARENT_DIALOG", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def session_browser_path(workspace: str, session_id: str) -> str:
    import base64

    enc = base64.urlsafe_b64encode(workspace.encode()).decode().strip("=")
    sid = session_id.strip()
    return f"/{enc}/session/{sid}" if sid else ""


def session_browser_url(opencode_url: str, workspace: str, session_id: str) -> str:
    base = opencode_url.rstrip("/")
    path = session_browser_path(workspace, session_id)
    return f"{base}{path}" if path else base


def format_dialog_message(speaker: str, body: str) -> str:
    return f"**{speaker}**\n\n{body.strip()}\n"

def human_dialog_enabled() -> bool:
    raw = os.environ.get("SCILLM_OPENCODE_TRANSPORT_HUMAN_DIALOG", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def parent_ui_model() -> str:
    """Model pinned on the parent collaboration session (OpenCode UI human typing).

    ChatGPT OAuth / Codex rejects ``gpt-5.5-pro``; use ``gpt-5.5`` or ``gpt-5.2-codex``.
    """
    raw = os.environ.get("SCILLM_OPENCODE_TRANSPORT_PARENT_MODEL", "gpt-5.5").strip()
    model = raw or "gpt-5.5"
    if model in {"gpt-5.5-pro", "openai/gpt-5.5-pro"}:
        return "gpt-5.5"
    return model


def worker_message_model() -> str:
    raw = os.environ.get("SCILLM_OPENCODE_TRANSPORT_WORKER_MODEL", "").strip()
    return raw or parent_ui_model()


def opencode_message_error(message_payload: dict[str, Any]) -> dict[str, Any] | None:
    info = message_payload.get("info")
    if not isinstance(info, dict):
        return None
    error = info.get("error")
    if not isinstance(error, dict):
        return None
    data = error.get("data") if isinstance(error.get("data"), dict) else {}
    message = str(data.get("message") or error.get("message") or error.get("name") or "opencode message error")
    return {
        "message": message,
        "name": str(error.get("name") or ""),
        "status_code": data.get("statusCode"),
        "is_retryable": data.get("isRetryable"),
        "response_body": data.get("responseBody"),
    }


def is_opencode_message_aborted_error(provider_error: dict[str, Any] | None) -> bool:
    if not provider_error:
        return False
    name = str(provider_error.get("name") or "").casefold()
    message = str(provider_error.get("message") or "").strip().casefold()
    return name == "messageabortederror" or message == "aborted"


def concrete_blocked_reason_code(reason: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", reason.strip().casefold()).strip("_")
    return value[:80] or "blocked_substrate"


def assistant_text_blocker(text: str) -> dict[str, Any] | None:
    receipt = classify_patch_delegate_result(text)
    if receipt.get("status") == PATCH_DELEGATE_BLOCKED and receipt.get("has_concrete_blocker"):
        return {
            "blocked_reason": concrete_blocked_reason_code(
                str(receipt.get("reason") or "blocked_substrate")
            ),
            "receipt_classifier": receipt,
        }
    return None


def parent_ui_agent() -> str:
    """Optional OpenCode agent on the parent session (empty = server default ``build``)."""
    return os.environ.get("SCILLM_OPENCODE_TRANSPORT_PARENT_AGENT", "").strip()


def extract_message_text(message_payload: dict[str, Any]) -> str:
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


def message_role(message_payload: dict[str, Any]) -> str:
    info = message_payload.get("info") if isinstance(message_payload.get("info"), dict) else {}
    return str(info.get("role") or message_payload.get("role") or "").strip()


def parse_dialog_speaker(text: str) -> str | None:
    match = DIALOG_SPEAKER_RE.match(text.strip())
    if not match:
        return None
    return match.group(1).strip()


def collaborator_from_message(*, speaker: str | None, role: str) -> str:
    if speaker:
        low = speaker.casefold()
        if low.startswith("worker"):
            return "worker"
        if "project agent" in low:
            return "project_agent"
        return "labeled"
    role_low = role.casefold()
    if role_low in {"user", "human"}:
        return "human"
    if "assistant" in role_low or role_low in {"agent", "bot"}:
        return "opencode_model"
    return "unknown"


@dataclass
class DialogTurn:
    message_id: str
    collaborator: str
    speaker: str
    text: str
    role: str = ""
    subagent_run_id: str = ""
    subagent_kind: str = ""
    subagent_label: str = ""
    agent: str = ""
    agent_id: str = ""
    mode: str = ""
    attempt_id: int = 0
    skills: list[str] = field(default_factory=list)
    created_at: str = ""
    routing_hint: dict[str, Any] = field(default_factory=dict)
    audience: str = ""

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        if not row.get("routing_hint"):
            row.pop("routing_hint", None)
        return row


def message_created_at_iso(item: dict[str, Any]) -> str:
    ts = session_epoch_s(item)
    if ts is None:
        info = item.get("info")
        if isinstance(info, dict):
            ts = session_epoch_s(info)
    if ts is None:
        return ""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_skill_slugs(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for match in SKILL_SLUG_INLINE_RE.finditer(text or ""):
        slug = (match.group(2) or "").strip().lower()
        if slug and slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


def strip_skill_slugs(text: str) -> str:
    return SKILL_SLUG_INLINE_RE.sub(" ", text or "").strip()


def routing_hint_for_turn(turn: DialogTurn) -> dict[str, Any]:
    body = strip_speaker_markdown(turn.text)
    if turn.collaborator == "human":
        return {
            "label": "Reviewer room",
            "tone": "to-reviewer",
            "audience": "project_agent",
            "inferred": False,
        }
    if FORWARD_HUMAN_RE.search(body):
        return {
            "label": "Worker",
            "tone": "to-worker",
            "audience": "worker",
            "inferred": False,
        }
    if SPAWN_ROLE_RE.search(body) or DISPATCH_ROLE_RE.search(body):
        return {
            "label": "Worker",
            "tone": "to-worker",
            "audience": "worker",
            "inferred": False,
        }
    if turn.collaborator == "worker":
        return {
            "label": "Collaboration room",
            "tone": "to-human",
            "audience": "human",
            "inferred": False,
        }
    if turn.collaborator == "project_agent":
        return {
            "label": "Collaboration room",
            "tone": "to-human",
            "audience": "human",
            "inferred": False,
        }
    return {
        "label": "Collaboration room",
        "tone": "to-human",
        "audience": "human",
        "inferred": False,
    }


def build_skill_call_spec(
    *,
    skill: str,
    args: dict[str, Any],
    transport_run_id: str,
    turn_id: str,
    requested_by: str,
    timeout_sec: int = 600,
    project_scope: str = "scillm-transport",
) -> dict[str, Any]:
    import hashlib

    skill_call_id = f"{transport_run_id}-{skill}-{hashlib.sha256(turn_id.encode()).hexdigest()[:10]}"
    idem = f"sha256:{hashlib.sha256((transport_run_id + skill + turn_id).encode()).hexdigest()}"
    return {
        "schema": "scillm.skill_call.v1",
        "action": "skill_call",
        "skill_call_id": skill_call_id,
        "idempotency_key": idem,
        "skill": skill,
        "args": args,
        "requested_by": requested_by,
        "allowed_tools": [f"{skill}.run_sh"],
        "timeout_sec": timeout_sec,
        "turn_id": turn_id,
        "project_scope": project_scope,
        "thread_id": transport_run_id,
    }


def format_skill_call_dialog_body(*, skill: str, receipt: dict[str, Any], user_note: str) -> str:
    status = str(receipt.get("status") or "unknown")
    excerpt = str(receipt.get("assistant_excerpt") or receipt.get("plan_excerpt") or "")
    hits = receipt.get("recall_hits")
    lines = [
        f"Executed `/{skill}` via mediated **skill_call** (`{status}`).",
        "",
    ]
    if user_note.strip():
        lines.extend(["**Operator note:**", user_note.strip(), ""])
    if excerpt.strip():
        lines.extend(["**Result excerpt:**", excerpt.strip()[:2500], ""])
    elif isinstance(hits, list) and hits:
        lines.append("**Memory hits:**")
        for hit in hits[:5]:
            if isinstance(hit, dict):
                title = str(hit.get("title") or hit.get("id") or "hit")
                snippet = str(hit.get("snippet") or hit.get("text") or "")[:300]
                lines.append(f"- {title}: {snippet}")
    errors = receipt.get("errors")
    if isinstance(errors, list) and errors:
        lines.extend(["", "**Errors:**", *[f"- {e}" for e in errors[:5]]])
    validation = receipt.get("validation")
    if isinstance(validation, dict) and validation.get("commands_run"):
        lines.extend(["", "**Commands:**", *[f"- {c}" for c in validation["commands_run"][:5]]])
    return "\n".join(lines).strip() + "\n"


def dialog_turn_from_message(item: dict[str, Any]) -> DialogTurn | None:
    if not isinstance(item, dict):
        return None
    message_id = message_id_from_payload(item)
    role = message_role(item)
    text = extract_message_text(item)
    if not text and not message_id:
        return None
    speaker_label = parse_dialog_speaker(text)
    collaborator = collaborator_from_message(speaker=speaker_label, role=role)
    if collaborator == "labeled":
        collaborator = "worker" if (speaker_label or "").casefold().startswith("worker") else "project_agent"
    display = speaker_label or ("Human" if collaborator == "human" else role or "unknown")
    turn = DialogTurn(
        message_id=message_id,
        collaborator=collaborator,
        speaker=display,
        text=text,
        role=role,
        created_at=message_created_at_iso(item),
    )
    hint = routing_hint_for_turn(turn)
    turn.routing_hint = hint
    turn.audience = str(hint.get("audience") or "")
    return turn


def _child_for_role_attempt(state: TransportState, role: str, attempt_id: int) -> ChildAttempt | None:
    role_key = (role or "").strip().lower()
    for row in state.children:
        if not isinstance(row, dict):
            continue
        if str(row.get("role") or "").strip().lower() != role_key:
            continue
        if int(row.get("attempt_id") or 0) == attempt_id:
            return ChildAttempt.from_dict(row)
    return None


def _annotate_turn(turn: DialogTurn, child: ChildAttempt) -> DialogTurn:
    summary = child_summary_dict(child)
    turn.subagent_run_id = child.subagent_run_id
    turn.subagent_kind = str(summary["subagent_kind"])
    turn.subagent_label = str(summary["subagent_label"])
    turn.agent = child.agent
    turn.agent_id = child.agent_id
    turn.role = child.role
    turn.mode = child.mode
    turn.attempt_id = child.attempt_id
    turn.skills = list(child.skills_materialized or child.skills)
    return turn


def enrich_dialog_turns(turns: list[DialogTurn], state: TransportState) -> list[DialogTurn]:
    """Attach structured subagent metadata for collaboration UI (source of truth: transport state)."""
    if not turns:
        return turns
    out: list[DialogTurn] = []
    for turn in turns:
        body = strip_speaker_markdown(turn.text) if hasattr(turn, "text") else turn.text
        body = body or turn.text
        speaker = (turn.speaker or "").strip()
        worker_match = WORKER_SPEAKER_RE.match(speaker)
        if worker_match:
            token = worker_match.group(1).strip()
            token_low = token.lower()
            child = None
            for row in reversed(state.children):
                if not isinstance(row, dict):
                    continue
                c = ChildAttempt.from_dict(row)
                if str(c.role or "").strip().lower() == token_low:
                    child = c
                    break
                if subagent_kind_label(c.role).casefold() == token.casefold():
                    child = c
                    break
            if child is not None:
                out.append(_annotate_turn(turn, child))
                continue
        spawn_role = SPAWN_ROLE_RE.search(body)
        if spawn_role:
            attempt_id = int(SPAWN_ATTEMPT_RE.search(body).group(1)) if SPAWN_ATTEMPT_RE.search(body) else 0
            token = spawn_role.group(1).strip()
            child = _child_for_role_attempt(state, token, attempt_id)
            if child is None:
                active = state.active_child()
                if active is not None:
                    summary = child_summary_dict(active)
                    if token.casefold() in {
                        str(active.role or "").casefold(),
                        summary.get("subagent_kind", "").casefold(),
                        str(active.agent_id or "").casefold(),
                    }:
                        child = active
            if child is not None:
                out.append(_annotate_turn(turn, child))
                continue
        dispatch_role = re.search(r"dispatching\s+\*\*([^*]+)\*\*", body, re.IGNORECASE)
        if dispatch_role:
            active = state.active_child()
            if active is not None:
                token = dispatch_role.group(1).strip().casefold()
                summary = child_summary_dict(active)
                if token in {
                    str(active.role or "").casefold(),
                    str(summary.get("subagent_kind") or "").casefold(),
                    str(active.agent_id or "").casefold(),
                }:
                    out.append(_annotate_turn(turn, active))
                    continue
        if not turn.routing_hint:
            hint = routing_hint_for_turn(turn)
            turn.routing_hint = hint
            turn.audience = str(hint.get("audience") or "")
        out.append(turn)
    return out


def strip_speaker_markdown(text: str) -> str:
    match = DIALOG_SPEAKER_RE.match(text.strip())
    if match:
        return text[match.end() :].strip()
    return text.strip()


def is_human_turn(turn: DialogTurn) -> bool:
    return turn.collaborator == "human"


def format_human_context_block(human_turns: list[DialogTurn]) -> str:
    if not human_turns:
        return ""
    lines = ["## Human input (from collaboration session)", ""]
    for turn in human_turns:
        lines.append(f"### {turn.speaker}")
        lines.append("")
        lines.append(turn.text.strip())
        lines.append("")
    lines.append("## Worker task")
    lines.append("")
    return "\n".join(lines)


def incorporate_human_dialog(prompt: str, human_turns: list[DialogTurn]) -> str:
    block = format_human_context_block(human_turns)
    if not block:
        return prompt
    return block + prompt.strip()


def build_dialog_collaboration_contract(*, transport_run_id: str, state: TransportState) -> dict[str, Any]:
    children_rows = [
        child_summary_dict(ChildAttempt.from_dict(row))
        for row in state.children
        if isinstance(row, dict)
    ]
    return {
        "schema": DIALOG_COLLAB_SCHEMA,
        "transport_run_id": transport_run_id,
        "collaborators": ["human", "project_agent", "worker"],
        "human_can_participate": human_dialog_enabled(),
        "dialog_session_id": state.parent_session_id,
        "dialog_last_human_message_id": state.dialog_last_human_message_id,
        "children": children_rows,
        "active_subagent": active_subagent_dict(state),
        "note": (
            "Three-way collaboration: the human types in the OpenCode parent session UI; "
            "the project agent posts labeled noReply handoffs; the worker runs on a child "
            "session and summaries return to the parent thread."
        ),
    }



async def post_parent_dialog(
    client: OpenCodeServeClient,
    state: TransportState,
    *,
    speaker: str,
    body: str,
) -> None:
    """Append orchestration text to the parent session without invoking a model (noReply)."""
    if not parent_dialog_enabled():
        return
    parent_id = state.parent_session_id.strip()
    if not parent_id:
        return
    await client.send_message(
        parent_id,
        parts=text_parts(format_dialog_message(speaker, body)),
        no_reply=True,
        directory=state.workspace,
    )


def build_transport_observation(
    *,
    transport_run_id: str,
    state: TransportState,
    settings: OpenCodeServeSettings | None = None,
) -> dict[str, Any]:
    """Machine- and human-facing links for dual observation (scillm SSE + OpenCode UI)."""
    opencode_url = (state.opencode_url or (settings.base_url if settings else "")).rstrip("/")
    parent_session_id = state.parent_session_id.strip()
    child_session_ids: list[str] = []
    for row in state.children:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("child_session_id") or "").strip()
        if sid and sid not in child_session_ids:
            child_session_ids.append(sid)
    active = state.active_child()
    active_child_session_id = active.child_session_id if active else ""
    auth_required = bool(settings and settings.password)
    scillm_prefix = f"/v1/scillm/opencode/transport/runs/{transport_run_id}"
    children_api = (
        f"{opencode_url}/session/{parent_session_id}/children"
        if opencode_url and parent_session_id
        else ""
    )
    return {
        "schema": OBSERVATION_SCHEMA,
        "transport_run_id": transport_run_id,
        "opencode_url": opencode_url,
        "browser_url": f"{opencode_url}/" if opencode_url else "",
        "auth_required": auth_required,
        "auth_username": settings.username if auth_required and settings else None,
        "browser_note": (
            "OpenCode serve requires HTTP Basic auth; a bare browser tab returns 401 until signed in. "
            "scillm-proxy injects credentials on API calls."
        ),
        "parent_session_id": parent_session_id,
        "child_session_ids": child_session_ids,
        "active_child_session_id": active_child_session_id,
        "opencode_children_api": children_api,
        "scillm_transport_run": scillm_prefix,
        "scillm_events_stream": f"{scillm_prefix}/events/stream",
        "artifact_dir": str(transport_output_base() / transport_run_id),
        "dialog_session_id": parent_session_id,
        "worker_session_id": active_child_session_id,
        "browser_dialog_url": session_browser_url(opencode_url, state.workspace, parent_session_id),
        "browser_worker_url": (
            session_browser_url(opencode_url, state.workspace, active_child_session_id)
            if active_child_session_id
            else ""
        ),
        "dialog_note": (
            "Open this URL for the three-way collaboration room (human + project agent + worker). "
            "Type in the chat as yourself; the project agent posts labeled handoffs. "
            "Linked worker sessions hold the full model trace (reasoning/tools)."
        ),
        "collaboration_mode": "three_way",
        "parent_ui_model": parent_ui_model(),
        "parent_ui_model_note": (
            "OpenCode parent session model for human typing in the UI. "
            "Avoid gpt-5.5-pro with ChatGPT OAuth; transport pins gpt-5.5 by default."
        ),
        "human_can_participate": human_dialog_enabled(),
        "scillm_dialog_api": f"{scillm_prefix}/dialog",
    }

def git_diff_empty(workspace: Path) -> bool:
    try:
        resolved_workspace = workspace.resolve()
        env = os.environ.copy()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "safe.directory"
        env["GIT_CONFIG_VALUE_0"] = str(resolved_workspace)
        root_proc = subprocess.run(
            ["git", "-C", str(resolved_workspace), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        if root_proc.returncode != 0:
            return True
        git_root = Path(root_proc.stdout.strip()).resolve()
        env["GIT_CONFIG_VALUE_0"] = str(git_root)
        pathspec = "."
        try:
            rel = os.path.relpath(resolved_workspace, git_root)
            pathspec = "." if rel == "." else rel
        except ValueError:
            pathspec = str(resolved_workspace)
        proc = subprocess.run(
            ["git", "-C", str(git_root), "diff", "--quiet", "--", pathspec],
            capture_output=True,
            timeout=30,
            env=env,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class OpenCodeTransport:
    """Parent/child session orchestration with sync-only authoritative messages."""

    def __init__(self, store: TransportStore | None = None) -> None:
        self.store = store or TransportStore(transport_output_base())


    async def mirror_run_started(
        self,
        client: OpenCodeServeClient,
        state: TransportState,
    ) -> None:
        await post_parent_dialog(
            client,
            state,
            speaker="Project agent",
            body=(
                f"Started transport run `{state.transport_run_id}` (DAG `{state.dag_node_id}`).\n\n"
                "This session is the **collaboration transcript**: project-agent handoffs and worker "
                "summaries appear here. Full model traces (reasoning, tools) live in linked worker "
                "sessions below."
            ),
        )

    async def mirror_child_created(
        self,
        client: OpenCodeServeClient,
        state: TransportState,
        child: ChildAttempt,
    ) -> None:
        worker_url = session_browser_url(
            state.opencode_url, state.workspace, child.child_session_id
        )
        await post_parent_dialog(
            client,
            state,
            speaker="Project agent",
            body=(
                f"Spawned **{child_summary_dict(child)['subagent_kind']}**"
                + (f" (`{child.agent_id}`)" if child.agent_id else f" (`{child.role}`)")
                + f" attempt {child.attempt_id}.\n"
                + (f"- Worker id: `{child.agent_id}`\n" if child.agent_id else "")
                + f"- OpenCode agent: `{child.agent}`\n"
                f"- Mode: `{child.mode}`\n"
                + (
                    f"- Skills: {', '.join(f'`{s}`' for s in child.skills_materialized)}\n"
                    if child.skills_materialized
                    else ""
                )
                + (
                    f"- Skills missing: {', '.join(child.skills_missing)}\n"
                    if child.skills_missing
                    else ""
                )
                + f"- Worker session: `{child.child_session_id}`\n"
                + f"- Full trace: {worker_url}"
            ),
        )

    async def mirror_worker_dispatch(
        self,
        client: OpenCodeServeClient,
        state: TransportState,
        child: ChildAttempt,
        *,
        model: str | None,
        prompt: str,
    ) -> None:
        preview = prompt.strip()
        if len(preview) > 600:
            preview = preview[:600] + "\n…(truncated)"
        await post_parent_dialog(
            client,
            state,
            speaker="Project agent",
            body=(
                f"Dispatching **{child_summary_dict(child)['subagent_kind']}**"
                + (f" (`{child.agent_id}`)" if child.agent_id else "")
                + f" → `{child.agent}`"
                + (f" with model `{model}`." if model else ".")
                + "\n\n**Worker task (preview):**\n\n```\n"
                + preview
                + "\n```"
            ),
        )

    async def mirror_worker_completed(
        self,
        client: OpenCodeServeClient,
        state: TransportState,
        child: ChildAttempt,
        *,
        model: str | None,
        assistant_text: str,
    ) -> None:
        worker_url = session_browser_url(
            state.opencode_url, state.workspace, child.child_session_id
        )
        excerpt = (assistant_text or "").strip()
        if len(excerpt) > 2500:
            excerpt = excerpt[:2500] + "\n…(truncated)"
        if not excerpt:
            excerpt = "(no assistant text captured)"
        await post_parent_dialog(
            client,
            state,
            speaker=f"Worker ({child_summary_dict(child)['subagent_kind']})",
            body=(
                f"Finished on `{child.agent}`"
                + (f" / `{model}`." if model else ".")
                + f"\n\n{excerpt}\n\nFull trace: {worker_url}"
            ),
        )



    async def list_dialog_turns(
        self,
        client: OpenCodeServeClient,
        state: TransportState,
        *,
        limit: int = 100,
    ) -> list[DialogTurn]:
        parent_id = state.parent_session_id.strip()
        if not parent_id:
            return []
        raw = await client.list_messages(parent_id, limit=limit, directory=state.workspace)
        turns: list[DialogTurn] = []
        for item in raw:
            turn = dialog_turn_from_message(item)
            if turn is not None:
                turns.append(turn)
        return enrich_dialog_turns(turns, state)

    async def pending_human_turns(
        self,
        client: OpenCodeServeClient,
        state: TransportState,
        *,
        limit: int = 100,
    ) -> list[DialogTurn]:
        if not human_dialog_enabled():
            return []
        turns = await self.list_dialog_turns(client, state, limit=limit)
        cursor = state.dialog_last_human_message_id.strip()
        pending: list[DialogTurn] = []
        past_cursor = not cursor
        for turn in turns:
            if not past_cursor:
                if turn.message_id == cursor:
                    past_cursor = True
                continue
            if is_human_turn(turn):
                pending.append(turn)
        return pending

    def acknowledge_human_turns(self, state: TransportState, human_turns: list[DialogTurn]) -> None:
        if not human_turns:
            return
        last_id = human_turns[-1].message_id.strip()
        if last_id:
            state.dialog_last_human_message_id = last_id
            self.store.save(state)

    async def prepare_worker_prompt(
        self,
        client: OpenCodeServeClient,
        state: TransportState,
        prompt: str,
    ) -> tuple[str, list[DialogTurn]]:
        human_turns = await self.pending_human_turns(client, state)
        if not human_turns:
            return prompt, []
        return incorporate_human_dialog(prompt, human_turns), human_turns

    async def mirror_human_input_incorporated(
        self,
        client: OpenCodeServeClient,
        state: TransportState,
        human_turns: list[DialogTurn],
    ) -> None:
        if not human_turns:
            return
        lines: list[str] = []
        for turn in human_turns:
            preview = turn.text.strip()
            if len(preview) > 400:
                preview = preview[:400] + "\n…(truncated)"
            lines.append(f"- {preview}")
        await post_parent_dialog(
            client,
            state,
            speaker="Project agent",
            body=(
                "Forwarding **human** input from this collaboration room into the worker dispatch:\n\n"
                + "\n".join(lines)
            ),
        )

    def _append_attachment_events(
        self,
        state: TransportState,
        *,
        subagent_run_id: str = "",
        turn_id: str = "",
        message_id: str = "",
        text_chunks: list[str] | None = None,
        receipt: dict[str, Any] | None = None,
    ) -> None:
        from scillm.proxy.opencode_transport_attachments import attachment_events_from_sources

        for row in attachment_events_from_sources(
            transport_run_id=state.transport_run_id,
            dag_node_id=state.dag_node_id,
            parent_session_id=state.parent_session_id,
            workspace=state.workspace,
            subagent_run_id=subagent_run_id,
            turn_id=turn_id,
            message_id=message_id,
            text_chunks=text_chunks,
            receipt=receipt,
        ):
            self.store.append_event(state.transport_run_id, row)

    async def execute_skill_call(
        self,
        client: OpenCodeServeClient,
        state: TransportState,
        *,
        skill: str,
        args: dict[str, Any],
        speaker: str,
        user_note: str = "",
        dry_run: bool | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        if dry_run is None:
            dry_run = os.environ.get("SCILLM_TRANSPORT_SKILL_DRY_RUN", "").strip().lower() in {
                "1",
                "true",
                "yes",
            }
        effective_turn = turn_id or f"transport_dialog/{state.transport_run_id}/{uuid.uuid4().hex[:8]}"
        spec = build_skill_call_spec(
            skill=skill.strip().lower(),
            args={**args, "transport_run_id": state.transport_run_id},
            transport_run_id=state.transport_run_id,
            turn_id=effective_turn,
            requested_by=speaker or "transport",
        )
        try:
            receipt = run_skill_call(spec, dry_run=dry_run)
            status = "ok"
        except (SkillAdapterError, NotImplementedError, ValueError) as exc:
            receipt = {
                "schema": "memory.skill_invocation.v1",
                "skill": skill,
                "status": "error",
                "errors": [str(exc)],
                "turn_id": effective_turn,
            }
            status = "error"

        dialog_body = format_skill_call_dialog_body(skill=skill, receipt=receipt, user_note=user_note)
        await post_parent_dialog(client, state, speaker=speaker or "Project agent", body=dialog_body)
        self.store.append_event(
            state.transport_run_id,
            enrich_event(
                {
                    "event_type": "skill_call.completed",
                    "skill": skill,
                    "status": status,
                    "dry_run": dry_run,
                    "turn_id": effective_turn,
                },
                transport_run_id=state.transport_run_id,
                dag_node_id=state.dag_node_id,
                parent_session_id=state.parent_session_id,
                workspace=state.workspace,
            ),
        )
        message_id = ""
        try:
            turns = await self.list_dialog_turns(client, state, limit=8)
            if turns:
                message_id = turns[-1].message_id or ""
        except Exception:
            message_id = ""
        self._append_attachment_events(
            state,
            turn_id=effective_turn,
            message_id=message_id,
            receipt=receipt,
        )
        return {
            "schema": "scillm.opencode_transport.skill_call.v1",
            "transport_run_id": state.transport_run_id,
            "skill_call_spec": spec,
            "skill_invocation": receipt,
            "status": status,
        }

    async def post_dialog_message(
        self,
        client: OpenCodeServeClient,
        state: TransportState,
        *,
        speaker: str,
        body: str,
        execute_skills: bool = True,
        dry_run: bool | None = None,
    ) -> dict[str, Any] | None:
        slugs = extract_skill_slugs(body) if execute_skills else []
        if slugs:
            primary = slugs[0]
            cleaned = strip_skill_slugs(body)
            return await self.execute_skill_call(
                client,
                state,
                skill=primary,
                args={"query": cleaned or body.strip(), "prompt": cleaned or body.strip()},
                speaker=speaker,
                user_note=body.strip(),
                dry_run=dry_run,
            )
        await post_parent_dialog(client, state, speaker=speaker, body=body)
        return None


    async def probe_capabilities(self, client: OpenCodeServeClient) -> dict[str, Any]:
        health = await client.health()
        return build_capability_flags(health=health, opencode_url=client.settings.base_url)

    async def create_transport_run(
        self,
        client: OpenCodeServeClient,
        *,
        dag_node_id: str,
        workspace: str,
        title: str | None = None,
        transport_run_id: str | None = None,
    ) -> TransportState:
        run_id = (transport_run_id or "").strip() or f"otr-{uuid.uuid4().hex[:12]}"
        directory = str(Path(workspace).resolve())
        parent = await client.create_session(
            title=title or f"scillm dag {dag_node_id}",
            directory=directory,
            model=parent_ui_model(),
            agent=parent_ui_agent() or None,
        )
        parent_session_id = session_id_from_payload(parent) or ""
        if not parent_session_id:
            raise ProxyError(502, "opencode parent session missing id", "provider_error")
        state = TransportState(
            transport_run_id=run_id,
            dag_node_id=dag_node_id,
            parent_session_id=parent_session_id,
            workspace=directory,
            opencode_url=client.settings.base_url,
        )
        self.store.save(state)
        self.store.append_subagent_run(
            run_id,
            {
                "schema": SUBAGENT_SCHEMA,
                "kind": "parent",
                "parent_session_id": parent_session_id,
                "dag_node_id": dag_node_id,
                "delivery_state": DELIVERY_CREATED,
                "ts": time.time(),
            },
        )
        event = enrich_event(
            {"event_type": "transport.created"},
            transport_run_id=run_id,
            dag_node_id=dag_node_id,
            parent_session_id=parent_session_id,
            delivery_state=DELIVERY_CREATED,
            workspace=directory,
        )
        self.store.append_event(run_id, event)
        await self.mirror_run_started(client, state)
        return state

    async def create_child(
        self,
        client: OpenCodeServeClient,
        state: TransportState,
        *,
        role: str = "",
        agent: str = "",
        mode: str = "propose_patches",
        title: str | None = None,
        skills: list[str] | None = None,
        agent_id: str | None = None,
    ) -> ChildAttempt:
        from scillm.proxy.worker_agents import (
            WorkerAgentResolutionError,
            resolve_child_spawn,
            resolve_worker_agent,
        )

        try:
            role, agent, mode, resolved_skills = resolve_child_spawn(
                agent_id=agent_id,
                role=role or None,
                agent=agent or None,
                mode=mode,
                skills=skills,
            )
        except WorkerAgentResolutionError as exc:
            raise ProxyError(
                400,
                str(exc),
                "unknown_worker_agent",
                details={"agent_id": agent_id},
            ) from exc
        worker = resolve_worker_agent(agent_id) if agent_id else None
        resolved_agent_id = (agent_id or (worker.agent_id if worker else "") or "").strip().lower()
        attempt_id = sum(1 for c in state.children if isinstance(c, dict) and c.get("role") == role) + 1
        subagent_run_id = f"{state.transport_run_id}-{role}-{attempt_id}"
        child_payload = await client.create_session(
            title=title or f"{role} attempt {attempt_id}",
            parent_id=state.parent_session_id,
            directory=state.workspace,
        )
        child_session_id = session_id_from_payload(child_payload) or ""
        if not child_session_id:
            raise ProxyError(502, "opencode child session missing id", "provider_error")
        for row in state.children:
            if isinstance(row, dict):
                row["active"] = False
        receipt = materialize_skill_view(
            run_id=f"{state.transport_run_id}-{subagent_run_id}",
            skills=resolved_skills,
            base_dir=transport_output_base() / state.transport_run_id / "skill-views",
        )
        child = ChildAttempt(
            subagent_run_id=subagent_run_id,
            role=role,
            child_session_id=child_session_id,
            agent=agent,
            attempt_id=attempt_id,
            delivery_state=DELIVERY_CREATED,
            active=True,
            mode=mode,
            agent_id=resolved_agent_id,
            skills=list(receipt.skills_requested),
            skills_materialized=list(receipt.skills_materialized),
            skills_missing=list(receipt.skills_missing),
        )
        state.children.append(child.to_dict())
        state.active_subagent_run_id = subagent_run_id
        self.store.save(state)
        self.store.append_subagent_run(
            state.transport_run_id,
            {**child.to_dict(), "schema": SUBAGENT_SCHEMA, "kind": "child", "ts": time.time()},
        )
        self.store.append_event(
            state.transport_run_id,
            enrich_event(
                {
                    "event_type": "child.created",
                    "role": role,
                    "skills": list(child.skills_materialized),
                    "subagent_kind": child_summary_dict(child)["subagent_kind"],
                    "agent_id": child.agent_id,
                    "subagent_label": child_summary_dict(child)["subagent_label"],
                },
                transport_run_id=state.transport_run_id,
                dag_node_id=state.dag_node_id,
                subagent_run_id=subagent_run_id,
                attempt_id=attempt_id,
                parent_session_id=state.parent_session_id,
                child_session_id=child_session_id,
                delivery_state=DELIVERY_CREATED,
                workspace=state.workspace,
                agent=agent,
            ),
        )
        await self.mirror_child_created(client, state, child)
        return child

    async def wait_idle(
        self,
        client: OpenCodeServeClient,
        session_id: str,
        *,
        deadline: float,
        directory: str | None = None,
        poll_s: float = 1.0,
    ) -> dict[str, Any]:
        while time.monotonic() < deadline:
            status_map = await client.session_status_map(directory=directory)
            if not session_is_busy(status_map, session_id):
                return status_map
            await asyncio.sleep(poll_s)
        raise ProxyError(504, f"session {session_id} did not become idle", "timeout")

    async def post_message_sync(
        self,
        client: OpenCodeServeClient,
        state: TransportState,
        child: ChildAttempt,
        *,
        prompt: str,
        system: str | None = None,
        model: str | None = None,
        timeout_s: float = 600.0,
        wait_idle: bool = True,
    ) -> dict[str, Any]:
        import asyncio

        effective_model = model or worker_message_model()
        deadline = time.monotonic() + timeout_s
        child.delivery_state = DELIVERY_QUEUED
        self._update_child(state, child)

        if session_is_busy(await client.session_status_map(directory=state.workspace), child.child_session_id):
            await client.abort(child.child_session_id, directory=state.workspace)
            await self.wait_idle(
                client,
                child.child_session_id,
                deadline=deadline,
                directory=state.workspace,
            )

        effective_prompt, human_turns = await self.prepare_worker_prompt(client, state, prompt)
        if human_turns:
            await self.mirror_human_input_incorporated(client, state, human_turns)
            self.acknowledge_human_turns(state, human_turns)
            self.store.append_event(
                state.transport_run_id,
                enrich_event(
                    {
                        "event_type": "dialog.human_incorporated",
                        "human_message_ids": [t.message_id for t in human_turns if t.message_id],
                        "human_turn_count": len(human_turns),
                    },
                    transport_run_id=state.transport_run_id,
                    dag_node_id=state.dag_node_id,
                    parent_session_id=state.parent_session_id,
                    workspace=state.workspace,
                ),
            )
        child.delivery_state = DELIVERY_POSTED
        self._update_child(state, child)
        self.store.append_event(
            state.transport_run_id,
            enrich_event(
                {
                    "event_type": "message.queued",
                    "prompt": effective_prompt,
                },
                transport_run_id=state.transport_run_id,
                dag_node_id=state.dag_node_id,
                subagent_run_id=child.subagent_run_id,
                attempt_id=child.attempt_id,
                parent_session_id=state.parent_session_id,
                child_session_id=child.child_session_id,
                delivery_state=DELIVERY_QUEUED,
                workspace=state.workspace,
                agent=child.agent,
                agent_id=child.agent_id,
                model=effective_model,
            ),
        )

        await self.mirror_worker_dispatch(
            client, state, child, model=effective_model, prompt=effective_prompt
        )
        if child.skills_materialized or child.skills:
            receipt = materialize_skill_view(
                run_id=f"{state.transport_run_id}-{child.subagent_run_id}",
                skills=child.skills or list(child.skills_materialized),
                base_dir=transport_output_base() / state.transport_run_id / "skill-views",
            )
            overlay = build_skills_system_overlay(receipt)
            system = merge_system_prompt(system, overlay)
        payload = await client.send_message(
            child.child_session_id,
            agent=child.agent,
            model=effective_model,
            parts=text_parts(effective_prompt),
            system=system,
            directory=state.workspace,
        )
        message_id = message_id_from_payload(payload)
        terminal = self._externally_terminal_child(state, child)
        if terminal is not None:
            terminal.last_message_id = terminal.last_message_id or message_id
            self._update_child(state, terminal)
            return self._terminal_message_result(
                state,
                terminal,
                payload=payload if isinstance(payload, dict) else {},
                message_id=message_id,
                event_type="message.aborted"
                if terminal.delivery_state == DELIVERY_ABORTED
                else "message.superseded",
                effective_model=effective_model,
            )
        child.last_message_id = message_id
        child.delivery_state = DELIVERY_DELIVERED
        self._update_child(state, child)
        provider_error = opencode_message_error(payload) if isinstance(payload, dict) else None
        if provider_error:
            if is_opencode_message_aborted_error(provider_error):
                terminal = self._externally_terminal_child(state, child)
                if terminal is None:
                    child.delivery_state = DELIVERY_ABORTED
                    child.active = False
                    self._update_child(state, child)
                    state.active_subagent_run_id = ""
                    self.store.save(state)
                    terminal = child
                return self._terminal_message_result(
                    state,
                    terminal,
                    payload=payload,
                    message_id=message_id,
                    event_type="message.aborted",
                    effective_model=effective_model,
                )
            blocked_result = self.mark_child_blocked(
                state,
                child,
                reason=provider_error["message"],
                message_id=message_id,
                payload=payload,
                effective_model=effective_model,
                provider_error=provider_error,
            )
            raise ProxyError(
                502,
                f"opencode message failed: {provider_error['message']}",
                "blocked_substrate",
                details={
                    "failure_type": "provider_error",
                    "provider_error": provider_error,
                    "terminal_result": blocked_result,
                },
            )

        assistant_text = extract_assistant_text(payload) if isinstance(payload, dict) else ""
        if wait_idle:
            try:
                await self.wait_idle(
                    client,
                    child.child_session_id,
                    deadline=deadline,
                    directory=state.workspace,
                )
            except ProxyError as exc:
                if exc.error_type != "timeout":
                    raise
                return await self.mark_child_timed_out(
                    client,
                    state,
                    child,
                    reason=str(exc),
                    message_id=message_id,
                    payload=payload if isinstance(payload, dict) else {},
                    effective_model=effective_model,
                )
            child.delivery_state = DELIVERY_IDLE_SEEN
            self._update_child(state, child)
            terminal = self._externally_terminal_child(state, child)
            if terminal is not None:
                return self._terminal_message_result(
                    state,
                    terminal,
                    payload=payload if isinstance(payload, dict) else {},
                    message_id=message_id,
                    event_type="message.aborted"
                    if terminal.delivery_state == DELIVERY_ABORTED
                    else "message.superseded",
                    effective_model=effective_model,
                )
            if not assistant_text:
                status_map = await client.session_status_map(directory=state.workspace)
                while time.monotonic() < deadline:
                    assistant_text, _ = await self._latest_assistant(
                        client, child.child_session_id, state.workspace
                    )
                    if assistant_text:
                        break
                    if not session_is_busy(status_map, child.child_session_id):
                        status_map = await client.session_status_map(directory=state.workspace)
                        if not session_is_busy(status_map, child.child_session_id):
                            break
                    await asyncio.sleep(1.0)
                    status_map = await client.session_status_map(directory=state.workspace)

        if child.mode == "workspace_write":
            blocker = assistant_text_blocker(assistant_text)
            if blocker is not None:
                return self.mark_child_blocked(
                    state,
                    child,
                    reason=blocker["blocked_reason"],
                    message_id=message_id,
                    payload=payload if isinstance(payload, dict) else {},
                    effective_model=effective_model,
                    extra={"receipt_classifier": blocker["receipt_classifier"]},
                )

        diff = await client.diff(child.child_session_id, directory=state.workspace)
        if assistant_text:
            child.delivery_state = DELIVERY_ACTED
            self._update_child(state, child)

        if child.mode == "propose_patches" and not git_diff_empty(Path(state.workspace)):
            child.delivery_state = DELIVERY_FAILED
            self._update_child(state, child)
            raise ProxyError(
                409,
                "read-only propose_patches mode requires empty git diff after message",
                "write_allowlist_violation",
            )

        if child.delivery_state == DELIVERY_ACTED:
            child.delivery_state = DELIVERY_COMPLETED
            self._update_child(state, child)

        await self.mirror_worker_completed(
            client, state, child, model=effective_model, assistant_text=assistant_text
        )

        result = {
            "schema": "scillm.opencode_transport.message.v1",
            "transport_run_id": state.transport_run_id,
            "subagent_run_id": child.subagent_run_id,
            "child_session_id": child.child_session_id,
            "message_id": message_id,
            "delivery_state": child.delivery_state,
            "assistant_text": assistant_text,
            "diff": diff,
            "message": payload,
        }
        self.store.append_event(
            state.transport_run_id,
            enrich_event(
                {"event_type": "message.completed"},
                transport_run_id=state.transport_run_id,
                dag_node_id=state.dag_node_id,
                subagent_run_id=child.subagent_run_id,
                attempt_id=child.attempt_id,
                parent_session_id=state.parent_session_id,
                child_session_id=child.child_session_id,
                message_id=message_id,
                delivery_state=child.delivery_state,
                workspace=state.workspace,
                agent=child.agent,
                agent_id=child.agent_id,
            ),
        )
        return result

    async def fork_supersede(
        self,
        client: OpenCodeServeClient,
        state: TransportState,
        *,
        role: str,
        agent: str,
        reason: str,
        message_id: str | None = None,
        mode: str = "propose_patches",
        agent_id: str | None = None,
    ) -> ChildAttempt:
        active = state.active_child()
        if active is None:
            raise ProxyError(409, "no active child to supersede", "invalid_state")
        active.delivery_state = DELIVERY_SUPERSEDED
        active.active = False
        self._update_child(state, active)
        forked = await client.fork_session(
            active.child_session_id,
            message_id=message_id or active.last_message_id or None,
            directory=state.workspace,
        )
        fork_session_id = session_id_from_payload(forked) or ""
        attempt_id = active.attempt_id + 1
        subagent_run_id = f"{state.transport_run_id}-{role}-{attempt_id}"
        resolved_agent_id = (agent_id or active.agent_id or "").strip().lower()
        child = ChildAttempt(
            subagent_run_id=subagent_run_id,
            role=role,
            child_session_id=fork_session_id,
            agent=agent,
            attempt_id=attempt_id,
            delivery_state=DELIVERY_CREATED,
            active=True,
            mode=mode,
            agent_id=resolved_agent_id,
            skills=list(active.skills),
            skills_materialized=list(active.skills_materialized),
            skills_missing=list(active.skills_missing),
        )
        state.children.append(child.to_dict())
        state.active_subagent_run_id = subagent_run_id
        self.store.save(state)
        self.store.append_event(
            state.transport_run_id,
            enrich_event(
                {"event_type": "child.superseded", "reason": reason},
                transport_run_id=state.transport_run_id,
                dag_node_id=state.dag_node_id,
                subagent_run_id=subagent_run_id,
                attempt_id=attempt_id,
                parent_session_id=state.parent_session_id,
                child_session_id=fork_session_id,
                delivery_state=DELIVERY_SUPERSEDED,
                workspace=state.workspace,
                agent=agent,
                agent_id=resolved_agent_id,
            ),
        )
        return child

    async def abort_active_child(
        self,
        client: OpenCodeServeClient,
        state: TransportState,
        *,
        reason: str = "operator_abort",
    ) -> dict[str, Any]:
        active = state.active_child()
        if active is None:
            raise ProxyError(409, "no active child to abort", "invalid_state")
        ok = await client.abort(active.child_session_id, directory=state.workspace)
        active.delivery_state = DELIVERY_ABORTED if ok else DELIVERY_FAILED
        active.active = False
        self._update_child(state, active)
        state.active_subagent_run_id = ""
        self.store.save(state)
        self.store.append_event(
            state.transport_run_id,
            enrich_event(
                {
                    "event_type": "child.aborted" if ok else "child.abort_failed",
                    "reason": reason,
                    "aborted": ok,
                },
                transport_run_id=state.transport_run_id,
                dag_node_id=state.dag_node_id,
                subagent_run_id=active.subagent_run_id,
                attempt_id=active.attempt_id,
                parent_session_id=state.parent_session_id,
                child_session_id=active.child_session_id,
                message_id=active.last_message_id,
                delivery_state=active.delivery_state,
                workspace=state.workspace,
                agent=active.agent,
                agent_id=active.agent_id,
            ),
        )
        return {
            "schema": "scillm.opencode_transport.abort.v1",
            "transport_run_id": state.transport_run_id,
            "subagent_run_id": active.subagent_run_id,
            "child_session_id": active.child_session_id,
            "delivery_state": active.delivery_state,
            "aborted": ok,
            "reason": reason,
        }

    async def reply_child_permission(
        self,
        client: OpenCodeServeClient,
        state: TransportState,
        *,
        permission_id: str,
        response: str = "reject",
        reason: str = "permission_rejected",
        subagent_run_id: str | None = None,
    ) -> dict[str, Any]:
        child = None
        if subagent_run_id:
            for row in state.children:
                if isinstance(row, dict) and row.get("subagent_run_id") == subagent_run_id:
                    child = ChildAttempt(**row)
                    break
        else:
            child = state.active_child()
        if child is None:
            raise ProxyError(409, "no child session for permission reply", "invalid_state")

        ok = await client.reply_permission(
            child.child_session_id,
            permission_id=permission_id,
            response=response,
            directory=state.workspace,
        )
        self.store.append_event(
            state.transport_run_id,
            enrich_event(
                {
                    "event_type": "permission.replied",
                    "permission_id": permission_id,
                    "permission_response": response,
                    "reply_succeeded": ok,
                    "reason": reason,
                },
                transport_run_id=state.transport_run_id,
                dag_node_id=state.dag_node_id,
                subagent_run_id=child.subagent_run_id,
                attempt_id=child.attempt_id,
                parent_session_id=state.parent_session_id,
                child_session_id=child.child_session_id,
                message_id=child.last_message_id,
                delivery_state=child.delivery_state,
                workspace=state.workspace,
                agent=child.agent,
                agent_id=child.agent_id,
            ),
        )
        if response == "reject":
            return self.mark_child_blocked(
                state,
                child,
                reason=concrete_blocked_reason_code(reason or "permission_rejected"),
                message_id=child.last_message_id,
                extra={
                    "permission_id": permission_id,
                    "permission_response": response,
                    "reply_succeeded": ok,
                },
            )
        self.store.save(state)
        return {
            "schema": "scillm.opencode_transport.permission_reply.v1",
            "transport_run_id": state.transport_run_id,
            "subagent_run_id": child.subagent_run_id,
            "child_session_id": child.child_session_id,
            "permission_id": permission_id,
            "permission_response": response,
            "reply_succeeded": ok,
            "delivery_state": child.delivery_state,
        }

    async def mark_child_timed_out(
        self,
        client: OpenCodeServeClient,
        state: TransportState,
        child: ChildAttempt,
        *,
        reason: str,
        message_id: str = "",
        payload: dict[str, Any] | None = None,
        effective_model: str = "",
    ) -> dict[str, Any]:
        child.delivery_state = DELIVERY_TIMED_OUT
        child.active = False
        if message_id:
            child.last_message_id = message_id
        self._update_child(state, child)
        state.active_subagent_run_id = ""
        self.store.save(state)
        abort_ok = False
        try:
            abort_ok = await client.abort(child.child_session_id, directory=state.workspace)
        except Exception:
            abort_ok = False
        result = self._terminal_message_result(
            state,
            child,
            payload=payload or {},
            message_id=message_id,
            event_type="message.timed_out",
            effective_model=effective_model,
            extra_event={
                "failure_type": "timeout",
                "reason": reason,
                "abort_attempted": True,
                "abort_succeeded": abort_ok,
            },
        )
        result["failure_type"] = "timeout"
        result["reason"] = reason
        result["abort_attempted"] = True
        result["abort_succeeded"] = abort_ok
        return result

    def mark_child_blocked(
        self,
        state: TransportState,
        child: ChildAttempt,
        *,
        reason: str,
        message_id: str = "",
        payload: dict[str, Any] | None = None,
        effective_model: str = "",
        provider_error: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        child.delivery_state = DELIVERY_BLOCKED
        child.active = False
        if message_id:
            child.last_message_id = message_id
        self._update_child(state, child)
        state.active_subagent_run_id = ""
        self.store.save(state)
        extra_event: dict[str, Any] = {
            "failure_type": "provider_error" if provider_error else "substrate_blocked",
            "blocked_reason": reason,
        }
        if provider_error:
            extra_event["provider_error"] = provider_error
        if extra:
            extra_event.update(extra)
        result = self._terminal_message_result(
            state,
            child,
            payload=payload or {},
            message_id=message_id,
            event_type="message.blocked",
            effective_model=effective_model,
            extra_event=extra_event,
        )
        result["failure_type"] = extra_event["failure_type"]
        result["blocked_reason"] = reason
        if provider_error:
            result["provider_error"] = provider_error
        if extra:
            result.update(extra)
        return result


    async def iter_transport_message_stream(
        self,
        client,
        state: TransportState,
        child: ChildAttempt,
        *,
        prompt: str,
        system: str | None = None,
        model: str | None = None,
        timeout_s: float = 600.0,
        heartbeat_s: float = 15.0,
        wait_idle: bool = True,
    ):
        from scillm.proxy.opencode_transport_stream import iter_transport_message_stream

        effective_model = model or worker_message_model()
        async for row in iter_transport_message_stream(
            self,
            client,
            state,
            child,
            prompt=prompt,
            system=system,
            model=effective_model,
            timeout_s=timeout_s,
            heartbeat_s=heartbeat_s,
            wait_idle=wait_idle,
        ):
            yield row

    def tail_transport_events(
        self,
        transport_run_id: str,
        *,
        after_line: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        path = self.store.events_path(transport_run_id)
        if not path.is_file():
            return [], after_line
        rows: list[dict[str, Any]] = []
        line_no = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            line_no += 1
            if line_no <= after_line or not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows, line_no

    def _update_child(self, state: TransportState, child: ChildAttempt) -> None:
        persisted_state: TransportState | None = None
        try:
            persisted_state = self.store.load(state.transport_run_id)
        except ProxyError:
            persisted_state = None
        if persisted_state is not None:
            for row in persisted_state.children:
                if not isinstance(row, dict):
                    continue
                if row.get("subagent_run_id") != child.subagent_run_id:
                    continue
                existing_state = str(row.get("delivery_state") or "")
                if (
                    existing_state in TERMINAL_DELIVERY_STATES
                    and child.delivery_state != existing_state
                ):
                    state.active_subagent_run_id = persisted_state.active_subagent_run_id
                    state.children = persisted_state.children
                    state.dialog_last_human_message_id = persisted_state.dialog_last_human_message_id
                    return
                break
        for index, row in enumerate(state.children):
            if isinstance(row, dict) and row.get("subagent_run_id") == child.subagent_run_id:
                existing_state = str(row.get("delivery_state") or "")
                if (
                    existing_state in TERMINAL_DELIVERY_STATES
                    and child.delivery_state != existing_state
                ):
                    return
                state.children[index] = child.to_dict()
                break
        self.store.save(state)

    def _externally_terminal_child(
        self, state: TransportState, child: ChildAttempt
    ) -> ChildAttempt | None:
        """Refresh persisted state and detect abort/supersede races.

        ``post_message_sync`` can be awaiting OpenCode while another request
        aborts or supersedes the active child. When the await returns, the stale
        local ``ChildAttempt`` must not overwrite that terminal state.
        """
        try:
            latest_state = self.store.load(state.transport_run_id)
        except ProxyError:
            latest_state = state
        state.active_subagent_run_id = latest_state.active_subagent_run_id
        state.children = latest_state.children
        state.dialog_last_human_message_id = latest_state.dialog_last_human_message_id
        for row in state.children:
            if not isinstance(row, dict):
                continue
            if row.get("subagent_run_id") != child.subagent_run_id:
                continue
            latest_child = ChildAttempt.from_dict(row)
            if latest_child.delivery_state in TERMINAL_DELIVERY_STATES:
                return latest_child
            return None
        return None

    def _terminal_message_result(
        self,
        state: TransportState,
        child: ChildAttempt,
        *,
        payload: dict[str, Any],
        message_id: str,
        event_type: str,
        effective_model: str,
        extra_event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assistant_text = extract_assistant_text(payload) if payload else ""
        event = {
            "event_type": event_type,
            "terminal_state": child.delivery_state,
        }
        if extra_event:
            event.update(extra_event)
        self.store.append_event(
            state.transport_run_id,
            enrich_event(
                event,
                transport_run_id=state.transport_run_id,
                dag_node_id=state.dag_node_id,
                subagent_run_id=child.subagent_run_id,
                attempt_id=child.attempt_id,
                parent_session_id=state.parent_session_id,
                child_session_id=child.child_session_id,
                message_id=message_id or child.last_message_id,
                delivery_state=child.delivery_state,
                workspace=state.workspace,
                agent=child.agent,
                agent_id=child.agent_id,
                model=effective_model,
            ),
        )
        return {
            "schema": "scillm.opencode_transport.message.v1",
            "transport_run_id": state.transport_run_id,
            "subagent_run_id": child.subagent_run_id,
            "child_session_id": child.child_session_id,
            "message_id": message_id or child.last_message_id,
            "delivery_state": child.delivery_state,
            "assistant_text": assistant_text,
            "diff": [],
            "message": payload,
        }

    async def _latest_assistant(
        self,
        client: OpenCodeServeClient,
        session_id: str,
        directory: str,
    ) -> tuple[str, dict[str, Any] | None]:
        messages = await client.list_messages(session_id, limit=50, directory=directory)
        for item in reversed(messages):
            if not isinstance(item, dict):
                continue
            info = item.get("info") if isinstance(item.get("info"), dict) else {}
            role = str(info.get("role") or item.get("role") or "").lower()
            if "assistant" in role or role in {"agent", "bot"}:
                text = extract_assistant_text(item)
                if text:
                    return text, item
        return "", None
