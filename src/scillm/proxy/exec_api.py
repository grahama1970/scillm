"""scillm exec worker runtime endpoints.

This module adds a small, artifacted execution layer on top of the existing
scillm model proxy.  It is intentionally a runtime substrate, not a project
planner: callers such as plan-iterate or ask own goals, contracts, review
verdicts, and iteration policy.  scillm exec owns bounded worker execution,
status, events, and result artifacts.
"""

from __future__ import annotations

import asyncio
from asyncio.exceptions import LimitOverrunError
import contextlib
import fnmatch
import json
import os
import re
import shlex
import shutil
import hashlib
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Literal

import httpx
import jsonschema
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from scillm.harness.terminal_backend import (
    TerminalBackendError,
    TerminalRequest,
    TmuxTerminalBackend,
)
from scillm.proxy.errors import ProxyError
from scillm.dag_phart import run_phart_on_dag

AuthCheck = Callable[[Request], str | None]

RunnerKind = Literal[
    "scillm_call",
    "scillm_batch",
    "codex_exec",
    "opencode_exec",
    "opencode_serve",
    "pi_exec",
    "kimi_exec",
    "cursor_exec",
    "claude_print",
    "local_command",
    "deterministic_render",
    "deterministic_verifier",
]
SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]

_ACTIVE_RUNS: dict[str, "ExecRun"] = {}
_ACTIVE_LOCK = asyncio.Lock()

_DANGEROUS_COMMAND_RE = re.compile(
    r"(^|[;&|`$()\n])\s*(rm\s+-rf\s+/|rm\s+-rf\s+\.|mkfs\.|dd\s+if=|shutdown\b|reboot\b|:(){:|chmod\s+-R\s+777\s+/)",
    re.IGNORECASE,
)

_OPENCODE_EXEC_PROFILES: dict[str, dict[str, str]] = {
    "oc-chutes-deepseek": {
        "provider": "chutes",
        "model_env": "SCILLM_OPENCODE_CHUTES_DEEPSEEK_MODEL",
        "default_model": "chutes/moonshotai/Kimi-K2.6-TEE",
    },
}

_PI_EXEC_PROFILES: dict[str, dict[str, str]] = {
    "pi-chutes-kimi": {
        "provider": "chutes",
        "model_env": "SCILLM_PI_CHUTES_KIMI_MODEL",
        "default_model": "moonshotai/Kimi-K2.6-TEE",
    },
    "pi-opencode-kimi": {
        "provider": "opencode-go",
        "model_env": "SCILLM_PI_OPENCODE_KIMI_MODEL",
        "default_model": "kimi-k2.5",
    },
}

_CURSOR_EXEC_PROFILES: dict[str, dict[str, Any]] = {
    "cursor-auto": {
        "cursor_model": "auto",
        "mode": None,
        "default_force": False,
    },
    "cursor-plan": {
        "cursor_model": "auto",
        "mode": "plan",
        "default_force": False,
    },
    "cursor-composer-2.5": {
        "cursor_model": "composer-2.5",
        "mode": None,
        "default_force": False,
    },
}

_CODEX_EXEC_PROFILES: dict[str, dict[str, str]] = {
    "codex-gpt-5.5": {
        "model_env": "SCILLM_CODEX_EXEC_MODEL",
        "default_model": "gpt-5.5",
    },
    "codex-vision": {
        "model_env": "SCILLM_CODEX_EXEC_MODEL_VISION",
        "default_model": "gpt-5.5",
    },
}

_CODEX_EXEC_PROFILE_ALIASES: dict[str, str] = {
    "gpt-5.5": "codex-gpt-5.5",
}

_KIMI_EXEC_PROFILES: dict[str, dict[str, str]] = {
    "kimi-k2.6": {
        "model_env": "SCILLM_KIMI_EXEC_MODEL",
        "default_model": "kimi-k2.6",
    },
    "kimi-k2.5": {
        "model_env": "SCILLM_KIMI_EXEC_MODEL_K25",
        "default_model": "kimi-k2.5",
    },
}

_KIMI_EXEC_PROFILE_ALIASES: dict[str, str] = {
    "kimi": "kimi-k2.6",
}



def _opencode_serve_enabled() -> bool:
    return os.environ.get("SCILLM_OPENCODE_SERVE_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}



class WorktreeSpec(BaseModel):
    """Optional isolated git worktree settings for a worker node."""

    enabled: bool = False
    base_ref: str = "HEAD"
    cleanup_after_run: bool = False


class RetryPolicy(BaseModel):
    """Mechanical retry policy for a node.

    Semantic failures, review failures, and contract drift belong to the caller
    layer.  This retry policy is only for execution-level failures such as bad
    JSON, process errors, and timeouts.
    """

    max_attempts: int = 1
    retry_on: list[str] = Field(default_factory=lambda: ["process_error", "invalid_json", "timeout"])


class ReviewScopeSpec(BaseModel):
    """One narrowly scoped review-code fanout node call.

    The exec DAG stores these as selectable planner/editor metadata.  The
    review-code skill owns the actual fan-out execution and reducer semantics.
    `scope` is retained as a compatibility alias for older graph JSON; new
    callers should set `contract`.
    """

    scope: str | None = None
    contract: str | None = None
    agent: str | None = None
    model: str | None = None
    review_level: str | None = None
    proof_level: str | None = None
    reducer_policy: str | None = None
    read_only: bool = True
    evidence_required: bool = True
    closure_authority: str | None = None
    risk_triggers: list[str] = Field(default_factory=list)
    best_practice_skills: list[str] = Field(default_factory=list)
    prompt_preset: str | None = None
    prompt: str | None = None
    catalog_id: str | None = None
    catalog_version: str | None = None
    catalog_sha256: str | None = None
    inline_overrides: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ExecNode(BaseModel):
    """One executable worker/local/model node."""

    id: str
    type: RunnerKind
    depends_on: list[str] = Field(default_factory=list)

    graph_goal: str | None = None
    node_goal: str
    persona_ref: str | None = None
    persona_text: str | None = None
    protocol_role: str | None = None
    forbidden_decisions: list[str] = Field(
        default_factory=lambda: [
            "change_project_goal",
            "change_phase_contract",
            "declare_phase_complete",
            "declare_project_complete",
        ]
    )

    model: str | None = None
    model_pool: str | None = None
    reasoning_effort: str | None = None
    cwd: str | None = None
    sandbox: SandboxMode = "read-only"
    worktree: WorktreeSpec = Field(default_factory=WorktreeSpec)
    env: dict[str, str] = Field(default_factory=dict)

    messages: list[dict[str, Any]] | None = None
    prompt: str | None = None
    prompt_path: str | None = None
    command: str | list[str] | None = None
    review_scopes: list[ReviewScopeSpec] = Field(default_factory=list)
    template_id: str | None = None
    template_version: str | None = None
    template_sha256: str | None = None
    catalog_id: str | None = None
    catalog_version: str | None = None
    catalog_sha256: str | None = None
    inline_overrides: dict[str, Any] = Field(default_factory=dict)

    items: list[dict[str, Any]] | None = None
    manifest_path: str | None = None
    concurrency: int | None = None

    output_schema: dict[str, Any] | None = None
    output_schema_path: str | None = None

    timeout_s: float = 900.0
    idle_timeout_s: float = 300.0
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    allow_failure: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecRequest(ExecNode):
    """Single exec request."""

    exec_version: str = "scillm.exec.v1"
    run_id: str | None = None
    stream: bool = False


class ExecBatchRequest(BaseModel):
    """Independent worker batch request."""

    exec_batch_version: str = "scillm.exec.batch.v1"
    batch_id: str = Field(default_factory=lambda: f"exec-batch-{uuid.uuid4().hex[:12]}")
    graph_goal: str
    max_concurrency: int = 4
    stream: bool = False
    defaults: dict[str, Any] = Field(default_factory=dict)
    items: list[ExecNode]
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecGraphRequest(BaseModel):
    """Small runtime DAG for bounded worker execution.

    This is not a plan-iterate phase contract.  It only expresses execution
    dependencies and runtime properties for local/model/exec nodes.
    """

    exec_graph_version: str = "scillm.exec.graph.v1"
    graph_id: str = Field(default_factory=lambda: f"exec-graph-{uuid.uuid4().hex[:12]}")
    graph_goal: str

    cwd: str | None = None
    model: str | None = None
    sandbox: SandboxMode = "read-only"
    worktree: WorktreeSpec = Field(default_factory=WorktreeSpec)
    max_concurrency: int = 4
    stream: bool = False
    self_improvement_iterations: int | None = None
    review_fanout_limits: dict[str, int] = Field(default_factory=dict)
    review_iteration_limits: dict[str, int] = Field(default_factory=dict)

    metadata: dict[str, Any] = Field(default_factory=dict)
    personas: dict[str, dict[str, Any]] = Field(default_factory=dict)
    nodes: list[ExecNode]


def _redacted_model_dump(model: BaseModel) -> dict[str, Any]:
    payload = model.model_dump(mode="json", exclude_none=True)
    return _redact_env_values(payload)


def _redact_env_values(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key == "env" and isinstance(item, dict):
                redacted[key] = {env_key: "<redacted>" for env_key in item}
            else:
                redacted[key] = _redact_env_values(item)
        return redacted
    if isinstance(value, list):
        return [_redact_env_values(item) for item in value]
    return value


class ExecGraphAmendmentRequest(BaseModel):
    """Persisted draft amendment for an exec graph.

    Amendments are audit records, not direct runtime mutation.  The authoritative
    store is the memory daemon, which writes to ArangoDB.
    """

    amendment_version: str = "scillm.exec.graph.amendment.v1"
    graph_id: str
    run_id: str | None = None
    base_graph: ExecGraphRequest
    draft_graph: ExecGraphRequest
    diff: list[dict[str, Any]] = Field(default_factory=list)
    validation: dict[str, Any] = Field(default_factory=dict)
    warning_acceptance: dict[str, Any] | None = None
    actor: str = "unknown"
    provenance: dict[str, Any] = Field(default_factory=dict)
    status: Literal["proposed", "approved", "rejected", "superseded"] = "proposed"
    amendment_id: str | None = None


class ExecGraphAmendmentStatusRequest(BaseModel):
    """Status-only update for a persisted exec graph amendment."""

    status: Literal["approved", "rejected", "superseded"]
    actor: str = "unknown"
    reason: str | None = None


class ExecGraphAmendmentApplyRequest(BaseModel):
    """Publish an approved amendment as an applied runtime decision overlay."""

    actor: str = "unknown"
    reason: str | None = None
    expected_base_graph_sha256: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)


class ExecGraphActionRequest(BaseModel):
    """Runtime control action for an active exec graph.

    Actions are live runtime controls, not plan amendments.  They are therefore
    accepted only against an active run and are written into status/events as an
    action ledger with actor/provenance.
    """

    action: Literal["pause", "resume", "disable", "cancel", "stop"]
    target: Literal["graph", "node", "subtree"] = "graph"
    node_id: str | None = None
    actor: str = "unknown"
    reason: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)


class ReviewCatalogSaveRequest(BaseModel):
    """Create or update one review agent/contract catalog markdown file."""

    id: str
    version: str = "1"
    label: str | None = None
    description: str | None = None
    default_agent: str | None = None
    default_model: str | None = None
    default_preset: str | None = None
    review_level: str | None = None
    proof_level: str | None = None
    reducer_policy: str | None = None
    read_only: bool = True
    evidence_required: bool = True
    closure_authority: str | None = "final_review_gate"
    risk_triggers: list[str] = Field(default_factory=list)
    best_practice_skills: list[str] = Field(default_factory=list)
    compatible_node_types: list[str] = Field(default_factory=list)
    compatible_upstream_types: list[str] = Field(default_factory=list)
    compatible_downstream_types: list[str] = Field(default_factory=list)
    required_fields: list[str] = Field(default_factory=list)
    default: bool = False
    order: int | None = None
    prompt: str = ""
    overwrite: bool = True


class ExecRun:
    """Runtime state for one exec/exec-batch/exec-graph run."""

    def __init__(self, *, run_id: str, artifact_root: Path, auth_header: str, caller_skill: str) -> None:
        self.run_id = _safe_id(run_id)
        self.run_dir = artifact_root / self.run_id
        self.auth_header = auth_header
        self.caller_skill = caller_skill
        self.events_path = self.run_dir / "events.jsonl"
        self.status_path = self.run_dir / "status.json"
        self.node_results: dict[str, dict[str, Any]] = {}
        self._event_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._processes: set[asyncio.subprocess.Process] = set()
        self._graph_nodes: dict[str, ExecNode] = {}
        self._graph_children: dict[str, set[str]] = {}
        self._running_node_ids: set[str] = set()
        self._paused_graph = False
        self._paused_node_ids: set[str] = set()
        self._disabled_node_ids: set[str] = set()
        self._disabled_node_actions: dict[str, dict[str, Any]] = {}
        self._action_history: list[dict[str, Any]] = []
        self._action_condition = asyncio.Condition()
        self._cancel_requested = False
        self.run_dir.mkdir(parents=True, exist_ok=True)

    async def cancel(
        self,
        *,
        actor: str = "unknown",
        reason: str | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> int:
        self._cancel_requested = True
        self._append_action(
            action="cancel",
            target="graph",
            node_id=None,
            affected_node_ids=sorted(self._graph_nodes) if self._graph_nodes else [],
            actor=actor,
            reason=reason,
            provenance=provenance or {},
            status="accepted",
        )
        killed = 0
        for proc in list(self._processes):
            if proc.returncode is None:
                proc.terminate()
                killed += 1
        await self.emit("cancel_requested", killed_processes=killed)
        await self.write_status(state="cancel_requested")
        return killed

    async def apply_action(self, spec: ExecGraphActionRequest) -> dict[str, Any]:
        action = "cancel" if spec.action == "stop" else spec.action
        affected = self._resolve_action_nodes(spec)

        if action in {"pause", "disable"} and spec.target in {"node", "subtree"}:
            running_targets = sorted(set(affected) & self._running_node_ids)
            if running_targets:
                raise ProxyError(
                    409,
                    f"cannot {action} already-running node(s): {running_targets}; cancel the run or target pending nodes",
                    "conflict",
                )

        if action == "cancel":
            killed = await self.cancel(actor=spec.actor, reason=spec.reason, provenance=spec.provenance)
            return {
                "ok": True,
                "run_id": self.run_id,
                "action": action,
                "target": spec.target,
                "affected_node_ids": affected,
                "cancel_requested": True,
                "killed_processes": killed,
                "runtime_actions": self._action_history,
            }

        async with self._action_condition:
            if action == "pause":
                if spec.target == "graph":
                    self._paused_graph = True
                else:
                    self._paused_node_ids.update(affected)
            elif action == "resume":
                if spec.target == "graph":
                    self._paused_graph = False
                    self._paused_node_ids.clear()
                else:
                    for node_id in affected:
                        self._paused_node_ids.discard(node_id)
            elif action == "disable":
                self._disabled_node_ids.update(affected)

            entry = self._append_action(
                action=action,
                target=spec.target,
                node_id=spec.node_id,
                affected_node_ids=affected,
                actor=spec.actor,
                reason=spec.reason,
                provenance=spec.provenance,
                status="accepted",
            )
            if action == "disable":
                for node_id in affected:
                    self._disabled_node_actions[node_id] = entry
            self._action_condition.notify_all()

        await self.emit("runtime_action_applied", **entry)
        await self.write_status(state="paused" if self._has_pause_gate() else "running")
        return {
            "ok": True,
            "run_id": self.run_id,
            "action": action,
            "target": spec.target,
            "node_id": spec.node_id,
            "affected_node_ids": affected,
            "paused": self._has_pause_gate(),
            "disabled_node_ids": sorted(self._disabled_node_ids),
            "runtime_actions": self._action_history,
        }

    async def stream_single(self, node: ExecNode) -> AsyncIterator[str]:
        self._event_queue = asyncio.Queue()
        task = asyncio.create_task(self.run_single(node))
        async for chunk in self._stream_task(task):
            yield chunk

    async def stream_graph(self, graph: ExecGraphRequest) -> AsyncIterator[str]:
        self._event_queue = asyncio.Queue()
        task = asyncio.create_task(self.run_graph(graph))
        async for chunk in self._stream_task(task):
            yield chunk

    async def _stream_task(self, task: asyncio.Task[dict[str, Any]]) -> AsyncIterator[str]:
        while True:
            if task.done() and (self._event_queue is None or self._event_queue.empty()):
                break
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=2.0)
                yield _sse("event", event)
            except asyncio.TimeoutError:
                yield _sse("heartbeat", {"run_id": self.run_id, "ts": _now()})
        result = await task
        yield _sse("done", result)
        yield "data: [DONE]\n\n"

    async def run_single(self, node: ExecNode) -> dict[str, Any]:
        await self.emit("exec_started", node_id=node.id, runner=node.type)
        result = await self._run_node(node)
        self.node_results[node.id] = result
        status = "completed" if result.get("ok") else "failed"
        await self.emit("exec_finished", node_id=node.id, status=status)
        final = {
            "exec_result_version": "scillm.exec.result.v1",
            "run_id": self.run_id,
            "status": status,
            "result": result,
            "artifacts": self._artifact_summary(),
        }
        (self.run_dir / "execution_result.json").write_text(json.dumps(final, indent=2), encoding="utf-8")
        await self.write_status(state=status)
        return final

    async def run_graph(self, graph: ExecGraphRequest) -> dict[str, Any]:
        await self.emit("graph_started", graph_id=graph.graph_id, graph_goal=graph.graph_goal)
        (self.run_dir / "graph.request.json").write_text(
            json.dumps(_redacted_model_dump(graph), indent=2) + "\n",
            encoding="utf-8",
        )

        nodes = [_apply_graph_defaults(graph, node) for node in graph.nodes]
        _validate_graph(nodes)
        self._graph_nodes = {node.id: node for node in nodes}
        self._graph_children = _graph_children(nodes)

        pending = {node.id: node for node in nodes}
        running: dict[str, asyncio.Task[dict[str, Any]]] = {}
        completed: set[str] = set()
        failed: set[str] = set()
        disabled: set[str] = set()
        semaphore = asyncio.Semaphore(max(1, int(graph.max_concurrency)))

        async def launch(node: ExecNode) -> dict[str, Any]:
            async with semaphore:
                try:
                    return await self._run_node(node)
                finally:
                    self._running_node_ids.discard(node.id)

        async def wait_for_action() -> None:
            async with self._action_condition:
                try:
                    await asyncio.wait_for(self._action_condition.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    return

        while pending or running:
            if self._cancel_requested:
                for task in running.values():
                    task.cancel()
                break

            disabled_ready = [
                node
                for node in pending.values()
                if node.id in self._disabled_node_ids and all(dep in completed for dep in node.depends_on)
            ]
            for node in disabled_ready:
                pending.pop(node.id, None)
                completed.add(node.id)
                disabled.add(node.id)
                action_entry = self._disabled_node_actions.get(node.id, {})
                skipped = {
                    "node_id": node.id,
                    "ok": True,
                    "status": "disabled",
                    "failure_type": None,
                    "disabled_by_action": True,
                    "action_id": action_entry.get("action_id"),
                    "action_actor": action_entry.get("actor"),
                    "action_reason": action_entry.get("reason"),
                    "action_provenance": action_entry.get("provenance", {}),
                    "depends_on": node.depends_on,
                }
                self.node_results[node.id] = skipped
                await self.emit("node_disabled", **skipped)

            if disabled_ready:
                await self.write_status(state="running")
                continue

            ready_candidates = [
                node
                for node in pending.values()
                if node.id not in self._disabled_node_ids and all(dep in completed for dep in node.depends_on)
            ]
            ready = [node for node in ready_candidates if not self._is_node_paused(node.id)]
            paused_ready = [node for node in ready_candidates if self._is_node_paused(node.id)]
            blocked_by_failed = [
                node
                for node in pending.values()
                if any(dep in failed for dep in node.depends_on) and not node.allow_failure
            ]

            while blocked_by_failed:
                for node in blocked_by_failed:
                    pending.pop(node.id, None)
                    failed.add(node.id)
                    skipped = {
                        "node_id": node.id,
                        "ok": False,
                        "status": "skipped",
                        "failure_type": "dependency_failed",
                        "depends_on": node.depends_on,
                    }
                    self.node_results[node.id] = skipped
                    await self.emit("node_skipped", **skipped)
                blocked_by_failed = [
                    node
                    for node in pending.values()
                    if any(dep in failed for dep in node.depends_on) and not node.allow_failure
                ]

            for node in ready:
                pending.pop(node.id)
                self._running_node_ids.add(node.id)
                running[node.id] = asyncio.create_task(launch(node))
                await self.emit("node_scheduled", node_id=node.id, depends_on=node.depends_on)

            if not running:
                if paused_ready:
                    await self.emit("graph_paused", paused_node_ids=[node.id for node in paused_ready])
                    await self.write_status(state="paused")
                    await wait_for_action()
                    continue
                if pending:
                    raise ProxyError(400, f"DAG deadlock or unsatisfied dependencies: {sorted(pending)}", "invalid_request_error")
                break

            done, _pending_tasks = await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                node_id = next(k for k, v in running.items() if v is task)
                running.pop(node_id, None)
                try:
                    result = task.result()
                except asyncio.CancelledError:
                    result = {"node_id": node_id, "ok": False, "status": "cancelled", "failure_type": "cancelled"}
                self.node_results[node_id] = result
                if result.get("ok"):
                    completed.add(node_id)
                else:
                    node = self._graph_nodes.get(node_id)
                    if node and node.allow_failure:
                        completed.add(node_id)
                    else:
                        failed.add(node_id)
                await self.write_status(state="running")

        status = "cancelled" if self._cancel_requested else ("completed" if not failed else "failed")
        final = {
            "exec_graph_result_version": "scillm.exec.graph.result.v1",
            "run_id": self.run_id,
            "graph_id": graph.graph_id,
            "graph_goal": graph.graph_goal,
            "status": status,
            "completed": sorted(completed),
            "failed": sorted(failed),
            "disabled": sorted(disabled),
            "node_results": self.node_results,
            "runtime_actions": self._action_history,
            "artifacts": self._artifact_summary(),
        }
        (self.run_dir / "execution_result.json").write_text(json.dumps(final, indent=2), encoding="utf-8")
        await self.emit("graph_finished", status=status, completed=len(completed), failed=len(failed))
        await self.write_status(state=status)
        return final

    async def _run_node(self, node: ExecNode) -> dict[str, Any]:
        node_dir = self.run_dir / "nodes" / _safe_id(node.id)
        node_dir.mkdir(parents=True, exist_ok=True)
        (node_dir / "node.request.json").write_text(
            json.dumps(_redacted_model_dump(node), indent=2) + "\n",
            encoding="utf-8",
        )

        attempts = max(1, node.retry_policy.max_attempts)
        last_result: dict[str, Any] | None = None
        for attempt in range(1, attempts + 1):
            await self.emit("node_started", node_id=node.id, attempt=attempt, runner=node.type)
            result = await self._run_node_once(node, node_dir, attempt)
            last_result = result
            if result.get("ok"):
                await self.emit("node_finished", node_id=node.id, attempt=attempt, status="passed")
                return result

            failure_type = str(result.get("failure_type") or "process_error")
            retryable = failure_type in node.retry_policy.retry_on and attempt < attempts
            await self.emit(
                "node_failed",
                node_id=node.id,
                attempt=attempt,
                failure_type=failure_type,
                retryable=retryable,
            )
            if not retryable:
                return result
        return last_result or {"node_id": node.id, "ok": False, "failure_type": "unknown_failure"}

    async def _run_node_once(self, node: ExecNode, node_dir: Path, attempt: int) -> dict[str, Any]:
        started = time.monotonic()
        attempt_dir = node_dir / f"attempt-{attempt}"
        attempt_dir.mkdir(parents=True, exist_ok=True)

        upstream_results: dict[str, dict[str, Any]] | None = None
        if node.depends_on:
            parts: dict[str, dict[str, Any]] = {}
            for dep in node.depends_on:
                raw = self.node_results.get(dep)
                if isinstance(raw, dict):
                    parts[dep] = _compact_upstream_result(raw)
            if parts:
                upstream_results = parts
        prompt = _assemble_prompt(node, upstream_results=upstream_results)
        (attempt_dir / "assembled_prompt.txt").write_text(prompt, encoding="utf-8")

        cwd, cleanup = await self._prepare_cwd(node, attempt_dir)
        try:
            if node.type in {"local_command", "deterministic_render", "deterministic_verifier"}:
                result = await self._run_local(node, cwd, attempt_dir)
            elif node.type == "scillm_call":
                result = await self._run_scillm_call(node, prompt, attempt_dir)
            elif node.type == "scillm_batch":
                result = await self._run_scillm_batch(node, attempt_dir)
            elif node.type == "codex_exec":
                result = await self._run_codex_exec(node, prompt, cwd, attempt_dir)
            elif node.type == "opencode_exec":
                result = await self._run_opencode_exec(node, prompt, cwd, attempt_dir)
            elif node.type == "opencode_serve":
                result = await self._run_opencode_serve(node, prompt, attempt_dir)

            elif node.type == "pi_exec":
                result = await self._run_pi_exec(node, prompt, cwd, attempt_dir)
            elif node.type == "kimi_exec":
                result = await self._run_kimi_exec(node, prompt, cwd, attempt_dir)
            elif node.type == "cursor_exec":
                result = await self._run_cursor_exec(node, prompt, cwd, attempt_dir)
            elif node.type == "claude_print":
                result = await self._run_claude_print(node, prompt, cwd, attempt_dir)
            else:
                raise ValueError(f"unsupported node type: {node.type}")

            result.setdefault("node_id", node.id)
            result.setdefault("runner", node.type)
            result["attempt"] = attempt
            result["elapsed_s"] = round(time.monotonic() - started, 3)

            schema = _load_schema(node)
            if result.get("ok") and schema is not None:
                jsonschema.validate(result.get("result"), schema)
                result["schema_validated"] = True

            self._attach_output_evidence(node=node, result=result, node_dir=node_dir)
            (node_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            return result
        except jsonschema.ValidationError as exc:
            return {
                "node_id": node.id,
                "ok": False,
                "failure_type": "schema_validation_failed",
                "error": str(exc),
                "elapsed_s": round(time.monotonic() - started, 3),
            }
        except LimitOverrunError as exc:
            return {
                "node_id": node.id,
                "ok": False,
                "failure_type": "stream_read_error",
                "error": str(exc),
                "elapsed_s": round(time.monotonic() - started, 3),
            }
        except Exception as exc:
            message = str(exc)
            failure_type = "process_error"
            if "separator" in message.lower() and "chunk" in message.lower():
                failure_type = "stream_read_error"
            return {
                "node_id": node.id,
                "ok": False,
                "failure_type": failure_type,
                "error": message,
                "elapsed_s": round(time.monotonic() - started, 3),
            }
        finally:
            if cleanup:
                await _remove_worktree(node, cwd)

    async def _prepare_cwd(self, node: ExecNode, attempt_dir: Path) -> tuple[Path, bool]:
        cwd = Path(node.cwd or os.getcwd()).expanduser().resolve()
        if not node.worktree.enabled:
            cwd.mkdir(parents=True, exist_ok=True)
            return cwd, False
        worktree_dir = attempt_dir / "worktree"
        cmd = ["git", "-C", str(cwd), "worktree", "add", "--detach", str(worktree_dir), node.worktree.base_ref]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {err.decode(errors='replace') or out.decode(errors='replace')}")
        return worktree_dir, bool(node.worktree.cleanup_after_run)

    async def _run_local(self, node: ExecNode, cwd: Path, attempt_dir: Path) -> dict[str, Any]:
        if not node.command:
            raise ValueError(f"{node.type} requires command")
        if node.sandbox != "danger-full-access":
            _reject_dangerous_command(node.command)
        if str(node.metadata.get("terminal_backend") or "").strip().lower() == "tmux":
            return await self._run_local_tmux(node, cwd, attempt_dir)
        return await self._run_process(
            command=node.command,
            cwd=cwd,
            attempt_dir=attempt_dir,
            timeout_s=node.timeout_s,
            idle_timeout_s=node.idle_timeout_s,
            shell=isinstance(node.command, str),
            extra_env={
                **node.env,
                "SCILLM_EXEC_NODE_ID": node.id,
                "SCILLM_EXEC_RUN_DIR": str(self.run_dir),
                "SCILLM_EXEC_NODE_DIR": str(attempt_dir.parent),
                "SCILLM_EXEC_ATTEMPT_DIR": str(attempt_dir),
                "SCILLM_EXEC_STATUS_PATH": str(self.status_path),
                "SCILLM_EXEC_EVENTS_PATH": str(self.events_path),
            },
        )

    async def _run_local_tmux(self, node: ExecNode, cwd: Path, attempt_dir: Path) -> dict[str, Any]:
        """Run a local command through the attachable tmux terminal backend."""

        if not node.command:
            raise ValueError(f"{node.type} requires command")
        command = node.command if isinstance(node.command, list) else ["sh", "-lc", node.command]
        if not isinstance(command, list) or any(not isinstance(part, str) for part in command):
            raise ValueError("terminal_backend=tmux requires command to resolve to a string argv list")

        stdout_path = attempt_dir / "stdout.log"
        stderr_path = attempt_dir / "stderr.log"
        proc_events_path = attempt_dir / "events.jsonl"
        transcript_path = attempt_dir / "terminal.log"
        evidence_path = attempt_dir / "terminal.evidence.json"
        backend = TmuxTerminalBackend(
            tmux_binary=str(node.metadata.get("tmux_binary") or "tmux"),
            transcript_root=self.run_dir / "terminal",
        )
        terminal_env = {
            **node.env,
            "SCILLM_EXEC_NODE_ID": node.id,
            "SCILLM_EXEC_RUN_DIR": str(self.run_dir),
            "SCILLM_EXEC_NODE_DIR": str(attempt_dir.parent),
            "SCILLM_EXEC_ATTEMPT_DIR": str(attempt_dir),
            "SCILLM_EXEC_STATUS_PATH": str(self.status_path),
            "SCILLM_EXEC_EVENTS_PATH": str(self.events_path),
        }
        exit_marker = f"__SCILLM_EXIT_{uuid.uuid4().hex}"
        exit_status_option = f"@scillm_exit_status_{uuid.uuid4().hex}"
        request = TerminalRequest(
            run_id=self.run_id,
            node_id=node.id,
            worker_id=str(node.metadata.get("terminal_worker_id") or node.type),
            command=command,
            cwd=str(cwd),
            env=terminal_env,
            policy={
                "transcript_path": str(transcript_path),
                "exit_marker": exit_marker,
                "exit_status_option": exit_status_option,
            },
        )

        identity = None
        cleanup_status = "not_started"
        timed_out = False
        idle_timed_out = False
        cancelled = False
        exit_code: int | None = None
        cleanup_error: str | None = None
        terminal_evidence_capture_error: str | None = None
        session_exists_after_cleanup: bool | None = None
        transcript = ""
        last_size = -1
        last_output_at = time.monotonic()
        deadline = time.monotonic() + float(node.timeout_s)
        try:
            identity = await asyncio.to_thread(backend.create_session, request)
            await self.emit(
                "terminal_session_started",
                node_id=node.id,
                backend="tmux",
                session_id=identity.session_id,
                transcript_path=str(transcript_path),
            )
            while True:
                transcript = await asyncio.to_thread(backend.read_transcript, identity)
                current_size = len(transcript.encode("utf-8", errors="replace"))
                if current_size != last_size:
                    last_size = current_size
                    last_output_at = time.monotonic()
                status_code = await asyncio.to_thread(backend.exit_status, identity, exit_status_option)
                if status_code is not None:
                    exit_code = status_code
                    break
                now = time.monotonic()
                if self._cancel_requested:
                    cancelled = True
                    break
                if node.timeout_s > 0 and now > deadline:
                    timed_out = True
                    break
                if node.idle_timeout_s > 0 and (now - last_output_at) > node.idle_timeout_s:
                    idle_timed_out = True
                    break
                await asyncio.sleep(0.1)
        except TerminalBackendError as exc:
            stderr_path.write_text(str(exc), encoding="utf-8", errors="replace")
            return {
                "ok": False,
                "failure_type": "terminal_backend_error",
                "exit_code": None,
                "error": str(exc),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "events_path": str(proc_events_path),
                "terminal_backend": "tmux",
            }
        finally:
            if identity is not None:
                try:
                    await asyncio.to_thread(backend.cleanup, identity)
                    cleanup_status = "cleanup_completed"
                    session_exists_after_cleanup = False
                except Exception as exc:  # pragma: no cover - defensive cleanup evidence
                    cleanup_error = str(exc)
                    cleanup_status = f"cleanup_failed:{cleanup_error}"
                    try:
                        session_exists_after_cleanup = await asyncio.to_thread(backend._session_exists, identity.session_id)
                    except Exception:
                        session_exists_after_cleanup = None

        if identity is not None:
            try:
                evidence = await asyncio.to_thread(
                    backend.capture_transcript_evidence,
                    identity,
                    cleanup_status=cleanup_status,
                    session_exists=session_exists_after_cleanup,
                )
                evidence_path.write_text(json.dumps(evidence.to_dict(), indent=2) + "\n", encoding="utf-8")
                terminal_evidence = evidence.to_dict()
            except Exception as exc:
                terminal_evidence_capture_error = str(exc)
                terminal_evidence = {
                    "schema": "scillm.terminal_backend.transcript_evidence.v1",
                    "exists": transcript_path.exists(),
                    "capture_error": terminal_evidence_capture_error,
                    "cleanup_status": cleanup_status,
                }
        else:
            terminal_evidence = {
                "schema": "scillm.terminal_backend.transcript_evidence.v1",
                "exists": False,
                "cleanup_status": cleanup_status,
            }

        transcript = transcript_path.read_text(encoding="utf-8", errors="replace") if transcript_path.exists() else transcript
        stdout = _terminal_stdout_from_transcript(transcript, exit_marker)
        stdout_path.write_text(stdout, encoding="utf-8", errors="replace")
        stderr_text = "\n".join(
            part
            for part in [
                f"terminal cleanup failed: {cleanup_error}" if cleanup_error else "",
                f"terminal evidence capture failed: {terminal_evidence_capture_error}"
                if terminal_evidence_capture_error
                else "",
            ]
            if part
        )
        stderr_path.write_text(stderr_text, encoding="utf-8")
        with proc_events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "terminal.output", "text": stdout, "ts": _now()}, sort_keys=True) + "\n")
            f.write(
                json.dumps(
                    {
                        "type": "terminal.transcript_captured",
                        "terminal_evidence_path": str(evidence_path) if evidence_path.exists() else "",
                        "ts": _now(),
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        await self.emit(
            "terminal_transcript_captured",
            node_id=node.id,
            backend="tmux",
            transcript_path=str(transcript_path),
            terminal_evidence_path=str(evidence_path) if evidence_path.exists() else "",
            cleanup_status=cleanup_status,
        )

        parsed = _parse_jsonish(stdout)
        failure_type = None
        error = None
        if cancelled:
            failure_type = "cancelled"
            error = "run cancelled"
        elif timed_out:
            failure_type = "timeout"
            error = f"terminal command exceeded timeout_s={node.timeout_s}"
        elif idle_timed_out:
            failure_type = "idle_timeout"
            error = f"terminal command exceeded idle_timeout_s={node.idle_timeout_s}"
        elif exit_code is None:
            failure_type = "missing_terminal_exit_status"
            error = "terminal backend did not write the authoritative exit status"
        elif exit_code != 0:
            failure_type = "process_error"
        if cleanup_error is not None:
            failure_type = failure_type or "terminal_cleanup_failed"
            error = "; ".join(part for part in [error, f"terminal cleanup failed: {cleanup_error}"] if part)
        if terminal_evidence_capture_error is not None:
            failure_type = failure_type or "terminal_evidence_capture_failed"
            error = "; ".join(
                part
                for part in [
                    error,
                    f"terminal evidence capture failed: {terminal_evidence_capture_error}",
                ]
                if part
            )
        cleanup_ok = cleanup_status == "cleanup_completed"
        evidence_ok = terminal_evidence_capture_error is None

        return {
            "ok": exit_code == 0 and cleanup_ok and evidence_ok and not (cancelled or timed_out or idle_timed_out),
            "failure_type": failure_type,
            "exit_code": exit_code,
            "error": error,
            "result": parsed if parsed is not None else {"stdout_tail": stdout[-4000:], "transcript_tail": transcript[-4000:]},
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "events_path": str(proc_events_path),
            "terminal_backend": "tmux",
            "terminal_identity": identity.to_dict() if identity is not None else None,
            "terminal_evidence": terminal_evidence,
            "terminal_evidence_path": str(evidence_path) if evidence_path.exists() else None,
            "transcript_path": str(transcript_path),
            "terminal_cleanup_status": cleanup_status,
            "terminal_cleanup_error": cleanup_error,
            "terminal_evidence_capture_error": terminal_evidence_capture_error,
            "terminal_session_exists_after_cleanup": session_exists_after_cleanup,
        }

    async def _run_scillm_call(self, node: ExecNode, prompt: str, attempt_dir: Path) -> dict[str, Any]:
        url = os.environ.get("SCILLM_INTERNAL_CHAT_URL", "http://127.0.0.1:4001/v1/chat/completions")
        messages = node.messages or [{"role": "user", "content": prompt}]
        schema = _load_schema(node)
        payload: dict[str, Any] = {
            "model": node.model or os.environ.get("SCILLM_EXEC_DEFAULT_MODEL", "gpt-5.5"),
            "messages": messages,
            "scillm_metadata": {
                **node.metadata,
                "run_id": self.run_id,
                "node_id": node.id,
                "runner": node.type,
            },
        }
        if node.reasoning_effort:
            payload["reasoning_effort"] = node.reasoning_effort
        if schema is not None:
            payload["response_format"] = {"type": "json_object"}
            payload["json_schema"] = schema

        async with httpx.AsyncClient(timeout=node.timeout_s) as client:
            resp = await client.post(
                url,
                headers={"Authorization": self.auth_header, "X-Caller-Skill": self.caller_skill},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        (attempt_dir / "response.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        content = data.get("choices", [{}])[0].get("message", {}).get("content")
        parsed = _parse_jsonish(content)
        return {"ok": True, "result": parsed if parsed is not None else {"content": content}, "response_path": str(attempt_dir / "response.json")}

    async def _run_scillm_batch(self, node: ExecNode, attempt_dir: Path) -> dict[str, Any]:
        url = os.environ.get("SCILLM_INTERNAL_BATCH_URL", "http://127.0.0.1:4001/v1/scillm/batch/completions")
        items = _load_items(node)
        payload: dict[str, Any] = {
            "model_pool": node.model_pool or node.model or "qra-deepseek-pool",
            "batch_id": node.metadata.get("batch_id") or f"{self.run_id}-{node.id}",
            "items": items,
        }
        if node.concurrency is not None:
            payload["max_concurrency"] = node.concurrency
        async with httpx.AsyncClient(timeout=node.timeout_s) as client:
            resp = await client.post(
                url,
                headers={"Authorization": self.auth_header, "X-Caller-Skill": self.caller_skill},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        (attempt_dir / "response.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        return {"ok": data.get("failed", 0) == 0, "result": data, "response_path": str(attempt_dir / "response.json")}

    async def _run_codex_exec(self, node: ExecNode, prompt: str, cwd: Path, attempt_dir: Path) -> dict[str, Any]:
        profile = _resolve_codex_exec_profile(node.model)
        schema_path = _write_schema_if_needed(node, attempt_dir)
        result_path = attempt_dir / "final.json"
        command = _build_codex_exec_command(
            node=node,
            profile=profile,
            schema_path=schema_path,
            result_path=result_path,
        )
        command_path = attempt_dir / "codex.command.json"
        command_path.write_text(json.dumps(command, indent=2), encoding="utf-8")
        process_result = await self._run_process(
            command=command,
            cwd=cwd,
            attempt_dir=attempt_dir,
            timeout_s=node.timeout_s,
            idle_timeout_s=node.idle_timeout_s,
            stdin=prompt,
            shell=False,
            final_json_path=result_path if schema_path else None,
            extra_env=node.env,
        )
        result = None
        if schema_path and result_path.exists():
            result = _parse_jsonish(result_path.read_text(encoding="utf-8", errors="replace"))
        if result is None:
            stdout_path = Path(str(process_result.get("stdout_path") or ""))
            stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
            result = _parse_jsonish(stdout_text) or {"content": stdout_text}
        return {
            **process_result,
            "result": result,
            "codex_profile": profile["profile"],
            "codex_model": _codex_model_for_node(node, profile),
            "codex_command_path": str(command_path),
        }

    async def _run_opencode_exec(self, node: ExecNode, prompt: str, cwd: Path, attempt_dir: Path) -> dict[str, Any]:
        profile = _resolve_opencode_exec_profile(node.model)
        schema = _load_schema(node)
        opencode_prompt = _opencode_prompt(prompt, schema)
        config_path = _write_opencode_exec_config(node=node, profile=profile, attempt_dir=attempt_dir)
        agent = _opencode_agent_name(node)
        before = _snapshot_files(cwd) if node.sandbox == "workspace-write" else None
        allow_write_patterns = _opencode_allow_write_patterns(node) if node.sandbox == "workspace-write" else []

        command = [
            "opencode",
            "run",
            "--pure",
            "--format",
            "json",
            "--agent",
            agent,
            "--model",
            profile["model"],
            "--dir",
            str(cwd),
        ]
        if node.sandbox == "workspace-write":
            command.append("--dangerously-skip-permissions")
        command.append(opencode_prompt)

        process_result = await self._run_process(
            command=command,
            cwd=cwd,
            attempt_dir=attempt_dir,
            timeout_s=node.timeout_s,
            idle_timeout_s=node.idle_timeout_s,
            shell=False,
            extra_env={**node.env, "OPENCODE_CONFIG": str(config_path)},
        )

        stdout_path = Path(str(process_result.get("stdout_path")))
        extracted = _parse_opencode_json_events(stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else "")
        response_path = attempt_dir / "opencode.response.json"
        response_path.write_text(json.dumps(extracted, indent=2), encoding="utf-8")

        result = _parse_jsonish(extracted.get("text"))
        if result is None:
            result = {"content": extracted.get("text", ""), "events": extracted.get("events", [])[-5:]}

        ok = bool(process_result.get("ok"))
        failure_type = process_result.get("failure_type")
        write_audit: dict[str, Any] | None = None
        if before is not None:
            write_audit = _audit_write_allowlist(cwd=cwd, before=before, allow_patterns=allow_write_patterns)
            if write_audit["violations"]:
                ok = False
                failure_type = "write_allowlist_violation"

        return {
            **process_result,
            "ok": ok,
            "failure_type": failure_type,
            "result": result,
            "response_path": str(response_path),
            "opencode_profile": profile["profile"],
            "opencode_model": profile["model"],
            "opencode_provider": profile["provider"],
            "opencode_agent": agent,
            "opencode_config_path": str(config_path),
            "write_audit": write_audit,
        }



    async def _run_opencode_serve(self, node: ExecNode, prompt: str, attempt_dir: Path) -> dict[str, Any]:
        """Run an OpenCode serve session via scillm /v1/scillm/opencode/runs."""
        if not _opencode_serve_enabled():
            return {
                "ok": False,
                "failure_type": "feature_disabled",
                "error": "opencode_serve is disabled; set SCILLM_OPENCODE_SERVE_ENABLED=1 after live gates pass",
                "harness_role": "optional_tier2_actuator",
                "truth_source": "/memory harness_turns",
                "default_actuator": "opencode_exec",
            }
        url = os.environ.get(
            "SCILLM_INTERNAL_OPENCODE_RUN_URL",
            "http://127.0.0.1:4001/v1/scillm/opencode/runs",
        )
        agent = str(node.metadata.get("agent") or node.model or "").strip() or None
        skills = node.metadata.get("skills")
        if isinstance(skills, str):
            skills = [s.strip() for s in skills.split(",") if s.strip()]
        if not isinstance(skills, list):
            skills = []
        fork_from = node.metadata.get("fork_from_session_id") or node.metadata.get("fork_session_id")
        fork_msg = node.metadata.get("fork_at_message_id") or node.metadata.get("fork_message_id")
        payload: dict[str, Any] = {
            "prompt": prompt,
            "agent": agent,
            "model": node.model,
            "timeout_s": node.timeout_s,
            "wait": bool(node.metadata.get("wait", True)),
            "skills": skills,
            "mcp": node.metadata.get("mcp") or [],
            "cleanup_session": bool(node.metadata.get("cleanup_session", True)),
            "cleanup_skill_view": bool(node.metadata.get("cleanup_skill_view", True)),
            "fork_from_session_id": str(fork_from).strip() if fork_from else None,
            "fork_at_message_id": str(fork_msg).strip() if fork_msg else None,
            "scillm_metadata": {
                **node.metadata,
                "run_id": self.run_id,
                "node_id": node.id,
                "runner": node.type,
            },
        }
        if node.messages:
            payload["parts"] = node.messages
        async with httpx.AsyncClient(timeout=node.timeout_s + 30.0) as client:
            resp = await client.post(
                url,
                headers={"Authorization": self.auth_header, "X-Caller-Skill": self.caller_skill},
                json=payload,
            )
            if resp.status_code >= 400:
                detail = resp.text[:2000]
                return {
                    "ok": False,
                    "failure_type": "provider_error",
                    "error": f"opencode_serve HTTP {resp.status_code}: {detail}",
                }
            data = resp.json()
        (attempt_dir / "opencode_serve.response.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        assistant = data.get("assistant_text") or ""
        schema = _load_schema(node)
        parsed = _parse_jsonish(assistant)
        result_body = parsed if parsed is not None else {"assistant_text": assistant, "raw": data}
        ok = data.get("status") == "completed" or bool(assistant)
        if schema is not None and parsed is not None:
            try:
                jsonschema.validate(parsed, schema)
            except jsonschema.ValidationError as exc:
                return {
                    "ok": False,
                    "failure_type": "schema_validation_failed",
                    "error": str(exc),
                    "response_path": str(attempt_dir / "opencode_serve.response.json"),
                }
        return {
            "ok": ok,
            "result": result_body,
            "response_path": str(attempt_dir / "opencode_serve.response.json"),
            "session_id": data.get("session_id"),
            "skills": data.get("skills"),
        }

    async def _run_pi_exec(self, node: ExecNode, prompt: str, cwd: Path, attempt_dir: Path) -> dict[str, Any]:
        profile = _resolve_pi_exec_profile(node.model)
        schema = _load_schema(node)
        pi_prompt = _opencode_prompt(prompt, schema)
        before = _snapshot_files(cwd) if node.sandbox == "workspace-write" else None
        allow_write_patterns = _opencode_allow_write_patterns(node) if node.sandbox == "workspace-write" else []
        tools = "read,grep,find,ls" if node.sandbox == "read-only" else "read,grep,find,ls,edit,write"
        if node.sandbox == "danger-full-access":
            raise ProxyError(
                400,
                "pi_exec does not support sandbox='danger-full-access'; use workspace-write with metadata.allow_write_paths",
                "invalid_request_error",
            )

        command = [
            _pi_binary(),
            "--mode",
            "json",
            "--provider",
            profile["provider"],
            "--model",
            profile["model"],
            "--no-session",
            "--no-skills",
            "--no-extensions",
            "--no-prompt-templates",
            "--no-themes",
            "--thinking",
            "off",
            "--tools",
            tools,
            "-p",
            pi_prompt,
        ]
        process_result = await self._run_process(
            command=command,
            cwd=cwd,
            attempt_dir=attempt_dir,
            timeout_s=node.timeout_s,
            idle_timeout_s=node.idle_timeout_s,
            shell=False,
            extra_env=node.env,
        )

        stdout_path = Path(str(process_result.get("stdout_path")))
        extracted = _parse_pi_json_events(stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else "")
        response_path = attempt_dir / "pi.response.json"
        response_path.write_text(json.dumps(extracted, indent=2), encoding="utf-8")
        result = _parse_jsonish(extracted.get("text"))
        if result is None:
            result = {"content": extracted.get("text", ""), "events": extracted.get("events", [])[-5:]}

        ok = bool(process_result.get("ok")) and not extracted.get("error")
        failure_type = process_result.get("failure_type")
        if extracted.get("error") and failure_type is None:
            failure_type = "pi_error"

        write_audit: dict[str, Any] | None = None
        if before is not None:
            write_audit = _audit_write_allowlist(cwd=cwd, before=before, allow_patterns=allow_write_patterns)
            if write_audit["violations"]:
                ok = False
                failure_type = "write_allowlist_violation"
        pi_failure_type = _pi_exec_terminal_failure(extracted, write_audit)
        if pi_failure_type:
            ok = False
            failure_type = failure_type or pi_failure_type

        return {
            **process_result,
            "ok": ok,
            "failure_type": failure_type,
            "result": result,
            "response_path": str(response_path),
            "pi_profile": profile["profile"],
            "pi_model": profile["model"],
            "pi_provider": profile["provider"],
            "pi_tools": tools.split(","),
            "pi_stop_reason": extracted.get("stop_reason"),
            "pi_usage": extracted.get("usage"),
            "pi_text_length": len(str(extracted.get("text") or "")),
            "write_audit": write_audit,
        }


    async def _run_kimi_exec(self, node: ExecNode, prompt: str, cwd: Path, attempt_dir: Path) -> dict[str, Any]:
        profile = _resolve_kimi_exec_profile(node.model)
        schema = _load_schema(node)
        kimi_prompt = _opencode_prompt(prompt, schema)
        before = _snapshot_files(cwd) if node.sandbox == "workspace-write" else None
        allow_write_patterns = _opencode_allow_write_patterns(node) if node.sandbox == "workspace-write" else []
        if node.sandbox == "danger-full-access":
            raise ProxyError(
                400,
                "kimi_exec does not support sandbox='danger-full-access'; use workspace-write with metadata.allow_write_paths",
                "invalid_request_error",
            )

        command = _build_kimi_exec_command(node=node, profile=profile, cwd=cwd, prompt=kimi_prompt)
        command_path = attempt_dir / "kimi.command.json"
        command_path.write_text(json.dumps(command, indent=2), encoding="utf-8")
        process_result = await self._run_process(
            command=command,
            cwd=cwd,
            attempt_dir=attempt_dir,
            timeout_s=node.timeout_s,
            idle_timeout_s=node.idle_timeout_s,
            shell=False,
            extra_env=_kimi_exec_env(node),
        )

        stdout_path = Path(str(process_result.get("stdout_path") or ""))
        extracted = _parse_kimi_exec_output(
            stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else "",
            mode=_kimi_exec_output_mode(node),
        )
        response_path = attempt_dir / "kimi.response.json"
        response_path.write_text(json.dumps(extracted, indent=2), encoding="utf-8")
        result = _parse_jsonish(extracted.get("text"))
        if result is None:
            result = {"content": extracted.get("text", ""), "events": extracted.get("events", [])[-5:]}

        ok = bool(process_result.get("ok")) and not extracted.get("error")
        failure_type = process_result.get("failure_type")
        if extracted.get("error") and failure_type is None:
            failure_type = "kimi_error"

        write_audit: dict[str, Any] | None = None
        if before is not None:
            write_audit = _audit_write_allowlist(cwd=cwd, before=before, allow_patterns=allow_write_patterns)
            if write_audit["violations"]:
                ok = False
                failure_type = "write_allowlist_violation"
        kimi_failure_type = _kimi_exec_terminal_failure(extracted, write_audit)
        if kimi_failure_type:
            ok = False
            failure_type = failure_type or kimi_failure_type

        return {
            **process_result,
            "ok": ok,
            "failure_type": failure_type,
            "result": result,
            "response_path": str(response_path),
            "kimi_profile": profile["profile"],
            "kimi_model": _kimi_model_for_node(node, profile),
            "kimi_output_mode": _kimi_exec_output_mode(node),
            "kimi_text_length": len(str(extracted.get("text") or "")),
            "kimi_session_hint": extracted.get("session_hint"),
            "write_audit": write_audit,
            "kimi_command_path": str(command_path),
        }

    async def _run_cursor_exec(self, node: ExecNode, prompt: str, cwd: Path, attempt_dir: Path) -> dict[str, Any]:
        if node.sandbox == "danger-full-access":
            raise ProxyError(
                400,
                "cursor_exec does not support sandbox='danger-full-access'; use workspace-write with metadata.allow_write_paths",
                "invalid_request_error",
            )
        profile = _resolve_cursor_exec_profile(node.model)
        schema = _load_schema(node)
        cursor_model = _cursor_model_for_node(node, profile)
        cursor_force = _cursor_force_for_node(node, profile)
        cursor_mode = profile.get("mode")
        skills = _cursor_selected_skills(node)
        rule_name = _cursor_rule_name(node)
        run_ctx = cwd / ".scillm" / "cursor-headless" / _safe_id(f"{self.run_id}-{node.id}-a{attempt_dir.name}")
        harness = _materialize_cursor_harness(
            cwd=cwd,
            run_ctx=run_ctx,
            skills=skills,
            prompt=_cursor_prompt(prompt, schema, skills, run_ctx),
            rule_name=rule_name,
        )
        events_path = run_ctx / "cursor-events.jsonl"
        receipt_path = run_ctx / "receipt.json"
        before = _snapshot_files(cwd) if node.sandbox == "workspace-write" else None
        allow_write_patterns = _opencode_allow_write_patterns(node) if node.sandbox == "workspace-write" else []
        ignore_patterns = _cursor_write_ignore_patterns(rule_name)

        command: list[str] = [
            _cursor_agent_binary(),
            "-p",
            "--trust",
            "--workspace",
            str(cwd),
            "--output-format",
            "stream-json",
            "--stream-partial-output",
            "--model",
            cursor_model,
        ]
        if cursor_force:
            command.append("--force")
        if cursor_mode:
            command.extend(["--mode", str(cursor_mode)])
        cursor_sandbox = node.metadata.get("cursor_sandbox")
        if isinstance(cursor_sandbox, str) and cursor_sandbox.strip():
            command.extend(["--sandbox", cursor_sandbox.strip()])
        command.append(harness["prompt_text"])

        process_result = await self._run_cursor_agent_process(
            command=command,
            cwd=cwd,
            attempt_dir=attempt_dir,
            cursor_events_path=events_path,
            timeout_s=node.timeout_s,
            idle_timeout_s=float(node.idle_timeout_s),
            extra_env={**node.env, "CURSOR_API_KEY": _resolve_cursor_api_key()},
        )

        stderr_text = _read_process_stderr(process_result)
        stdout_text = process_result.get("cursor_stream_text") or ""
        if not stdout_text:
            stdout_path = Path(str(process_result.get("stdout_path") or ""))
            if stdout_path.is_file():
                stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
        extracted = process_result.get("cursor_extracted") or _parse_cursor_stream_json(stdout_text)
        extracted["agent_exit_code"] = process_result.get("exit_code")
        response_path = attempt_dir / "cursor.response.json"
        response_path.write_text(json.dumps(extracted, indent=2), encoding="utf-8")

        receipt = {
            "schema": "scillm.cursor_exec_receipt.v1",
            "run_ctx": str(run_ctx),
            "events_out": str(events_path),
            "prompt_out": harness["prompt_out"],
            "manifest": harness.get("manifest"),
            "rule_dir": harness.get("rule_dir"),
            "skills": skills,
            "cursor_profile": profile["profile"],
            "cursor_model": cursor_model,
            "cursor_mode": cursor_mode,
            "cursor_force": cursor_force,
            "agent_exit_code": process_result.get("exit_code"),
            **{k: extracted.get(k) for k in ("session_id", "model", "api_key_source", "tool_call_count", "result_event", "text")},
        }
        if stderr_text.strip():
            receipt["stderr_tail"] = stderr_text[-4000:]
        receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")

        result = _parse_jsonish(extracted.get("text"))
        if result is None:
            result = {"content": extracted.get("text", ""), "result_event": extracted.get("result_event")}

        ok = bool(process_result.get("ok"))
        failure_type = process_result.get("failure_type")
        write_audit: dict[str, Any] | None = None
        if before is not None:
            write_audit = _audit_write_allowlist(
                cwd=cwd,
                before=before,
                allow_patterns=allow_write_patterns,
                ignore_patterns=ignore_patterns,
            )
            if write_audit["violations"]:
                ok = False
                failure_type = "write_allowlist_violation"

        cursor_failure_type = _cursor_exec_terminal_failure(
            extracted,
            write_audit,
            recovered_from_stream=bool(process_result.get("recovered_from_stream")),
            stream_completed=bool(process_result.get("stream_completed")),
        )
        if cursor_failure_type:
            ok = False
            failure_type = failure_type or cursor_failure_type

        provider_error = _cursor_provider_error(stderr_text, extracted)
        return {
            **process_result,
            "ok": ok,
            "failure_type": failure_type,
            "error": provider_error if not ok and provider_error else process_result.get("error"),
            "stderr_tail": stderr_text[-4000:] if stderr_text else None,
            "result": result,
            "response_path": str(response_path),
            "receipt_path": str(receipt_path),
            "events_path": str(events_path),
            "cursor_events_path": str(events_path),
            "cursor_profile": profile["profile"],
            "cursor_model": cursor_model,
            "cursor_mode": cursor_mode,
            "cursor_force": cursor_force,
            "cursor_run_ctx": str(run_ctx),
            "cursor_harness": harness,
            "write_audit": write_audit,
        }


    async def _run_claude_print(self, node: ExecNode, prompt: str, cwd: Path, attempt_dir: Path) -> dict[str, Any]:
        schema_path = _write_schema_if_needed(node, attempt_dir)
        template = os.environ.get(
            "SCILLM_CLAUDE_PRINT_TEMPLATE",
            "claude -p --output-format stream-json {schema_args}",
        )
        schema_args = f"--json-schema {shlex.quote(str(schema_path))}" if schema_path else ""
        command = template.format(schema_args=schema_args)
        return await self._run_process(
            command=command,
            cwd=cwd,
            attempt_dir=attempt_dir,
            timeout_s=node.timeout_s,
            idle_timeout_s=node.idle_timeout_s,
            stdin=prompt,
            shell=True,
            extra_env=node.env,
        )

    async def _run_process(
        self,
        *,
        command: str | list[str],
        cwd: Path,
        attempt_dir: Path,
        timeout_s: float,
        idle_timeout_s: float,
        shell: bool = False,
        stdin: str | None = None,
        final_json_path: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        stdout_path = attempt_dir / "stdout.log"
        stderr_path = attempt_dir / "stderr.log"
        proc_events_path = attempt_dir / "events.jsonl"
        env = {
            **os.environ,
            "SCILLM_EXEC_RUN_ID": self.run_id,
            "SCILLM_EXEC_RUN_DIR": str(self.run_dir),
            "SCILLM_EXEC_ATTEMPT_DIR": str(attempt_dir),
            **(extra_env or {}),
        }

        if isinstance(command, list) and not shell:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            command_text = command if isinstance(command, str) else " ".join(shlex.quote(part) for part in command)
            proc = await asyncio.create_subprocess_shell(
                command_text,
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        self._processes.add(proc)
        last_output_at = time.monotonic()
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []

        if stdin is not None and proc.stdin is not None:
            proc.stdin.write(stdin.encode())
            await proc.stdin.drain()
            proc.stdin.close()

        async def read_stream(stream: asyncio.StreamReader | None, sink: list[str], label: str) -> None:
            nonlocal last_output_at
            if stream is None:
                return
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                last_output_at = time.monotonic()
                text = chunk.decode(errors="replace")
                sink.append(text)
                event = {"type": label, "text": text, "ts": _now()}
                with proc_events_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(event, sort_keys=True) + "\n")
                await self.emit(label, text=text)

        async def watchdog() -> None:
            while proc.returncode is None:
                if self._cancel_requested:
                    proc.terminate()
                    return
                if idle_timeout_s > 0 and (time.monotonic() - last_output_at) > idle_timeout_s:
                    proc.terminate()
                    return
                await asyncio.sleep(1.0)

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    read_stream(proc.stdout, stdout_chunks, "stdout"),
                    read_stream(proc.stderr, stderr_chunks, "stderr"),
                    proc.wait(),
                    watchdog(),
                ),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "ok": False,
                "failure_type": "timeout",
                "exit_code": proc.returncode,
                "error": f"process exceeded timeout_s={timeout_s}",
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "events_path": str(proc_events_path),
            }
        finally:
            self._processes.discard(proc)

        stdout = "".join(stdout_chunks)
        stderr = "".join(stderr_chunks)
        stdout_path.write_text(stdout, encoding="utf-8", errors="replace")
        stderr_path.write_text(stderr, encoding="utf-8", errors="replace")

        parsed = None
        if final_json_path and final_json_path.exists():
            parsed = json.loads(final_json_path.read_text())
        else:
            parsed = _parse_jsonish(stdout)

        failure_type = None
        if self._cancel_requested:
            failure_type = "cancelled"
        elif proc.returncode != 0:
            failure_type = "process_error"
        elif parsed is None:
            failure_type = "invalid_json"

        return {
            "ok": proc.returncode == 0 and not self._cancel_requested,
            "failure_type": failure_type,
            "exit_code": proc.returncode,
            "result": parsed if parsed is not None else {"stdout_tail": stdout[-4000:], "stderr_tail": stderr[-4000:]},
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "events_path": str(proc_events_path),
            "final_json_path": str(final_json_path) if final_json_path and final_json_path.exists() else None,
        }



    


    async def _run_cursor_agent_process(
        self,
        *,
        command: list[str],
        cwd: Path,
        attempt_dir: Path,
        cursor_events_path: Path,
        timeout_s: float,
        idle_timeout_s: float,
        extra_env: dict[str, str] | None = None,
        ) -> dict[str, Any]:
        """Run Cursor agent with incremental stream-json supervision."""
        stdout_path = attempt_dir / "stdout.log"
        stderr_path = attempt_dir / "stderr.log"
        proc_events_path = attempt_dir / "events.jsonl"
        cursor_events_path.parent.mkdir(parents=True, exist_ok=True)
        cursor_events_path.write_text("", encoding="utf-8")
        proc_events_path.write_text("", encoding="utf-8")

        env = {
            **os.environ,
            "SCILLM_EXEC_RUN_ID": self.run_id,
            "SCILLM_EXEC_RUN_DIR": str(self.run_dir),
            "SCILLM_EXEC_ATTEMPT_DIR": str(attempt_dir),
            **(extra_env or {}),
        }
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._processes.add(proc)

        stream_state = _new_cursor_stream_state()
        stdout_lines: list[str] = []
        stderr_chunks: list[str] = []
        started_at = time.monotonic()
        last_liveness_at = started_at
        stream_completed = False
        stream_terminal_status: str | None = None
        failure_type: str | None = None
        error: str | None = None

        async def append_proc_event(label: str, text: str) -> None:
            event = {"type": label, "text": text, "ts": _now()}
            with proc_events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
            await self.emit(label, text=text)

        async def drain_stderr() -> None:
            if proc.stderr is None:
                return
            while True:
                chunk = await proc.stderr.read(65536)
                if not chunk:
                    break
                stderr_chunks.append(chunk.decode(errors="replace"))

        stderr_task = asyncio.create_task(drain_stderr())

        async def handle_stream_line(line: str) -> None:
            nonlocal last_liveness_at, stream_completed, stream_terminal_status, failure_type, error
            stdout_lines.append(line)
            proc_text = line if line.endswith("\n") else f"{line}\n"
            await append_proc_event("stdout", proc_text)
            event = _parse_cursor_stream_line(line)
            if event is None:
                return
            with cursor_events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
            _ingest_cursor_stream_event(stream_state, event)
            if _cursor_stream_event_is_liveness(event):
                last_liveness_at = time.monotonic()
            terminal = _cursor_stream_terminal_status(event)
            if terminal == "success":
                stream_completed = True
                stream_terminal_status = "success"
            elif terminal == "error":
                stream_completed = True
                stream_terminal_status = "error"
                failure_type = "cursor_error"
                error = str(event.get("result") or event.get("message") or "cursor result error")

        stream_read_error: str | None = None
        try:
            assert proc.stdout is not None
            line_iter = _iter_subprocess_text_lines(proc.stdout).__aiter__()
            while True:
                now = time.monotonic()
                if self._cancel_requested:
                    failure_type = "cancelled"
                    proc.terminate()
                    break
                if timeout_s > 0 and (now - started_at) > timeout_s:
                    failure_type = "timeout"
                    error = f"process exceeded timeout_s={timeout_s}"
                    proc.kill()
                    break
                if idle_timeout_s > 0 and (now - last_liveness_at) > idle_timeout_s:
                    failure_type = "idle_timeout"
                    error = f"no cursor stream activity for idle_timeout_s={idle_timeout_s}"
                    proc.terminate()
                    break
                if stream_completed:
                    if proc.returncode is None:
                        proc.terminate()
                    break
                try:
                    line = await asyncio.wait_for(line_iter.__anext__(), timeout=1.0)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    if proc.returncode is not None:
                        break
                    continue
                await handle_stream_line(line.rstrip("\n"))
        except LimitOverrunError as exc:
            stream_read_error = str(exc)
            failure_type = failure_type or "stream_read_error"
            error = error or stream_read_error
        except ValueError as exc:
            if "exceed" in str(exc).lower() or "separator" in str(exc).lower():
                stream_read_error = str(exc)
                failure_type = failure_type or "stream_read_error"
                error = error or stream_read_error
            else:
                raise
        finally:
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
            self._processes.discard(proc)

        stdout_text = "\n".join(stdout_lines)
        if stdout_text:
            stdout_text += "\n"
        stderr_text = "".join(stderr_chunks)
        stdout_path.write_text(stdout_text, encoding="utf-8", errors="replace")
        stderr_path.write_text(stderr_text, encoding="utf-8", errors="replace")

        extracted = _finalize_cursor_stream_state(stream_state)
        (
            extracted,
            stream_completed,
            stream_terminal_status,
            failure_type,
            error,
            recovered_from_stream,
        ) = _apply_cursor_events_file_terminal(
            events_path=cursor_events_path,
            extracted=extracted,
            stream_completed=stream_completed,
            stream_terminal_status=stream_terminal_status,
            failure_type=failure_type,
            error=error,
            proc_returncode=proc.returncode,
        )
        ok = recovered_from_stream
        if failure_type == "cursor_error":
            ok = False
        elif failure_type in {None, ""}:
            if proc.returncode not in (None, 0) and not recovered_from_stream:
                failure_type = "process_error"
                ok = False
            elif recovered_from_stream:
                failure_type = None
            elif extracted.get("result_event"):
                terminal = _cursor_stream_terminal_status(extracted["result_event"])
                if terminal == "success":
                    ok = True
                    failure_type = None
                    recovered_from_stream = True
                    stream_completed = True
                elif terminal == "error":
                    ok = False
                    failure_type = "cursor_error"
            elif not str(extracted.get("text") or "").strip() and failure_type is None:
                failure_type = "empty_output"
                ok = False

        if stream_read_error and failure_type == "stream_read_error":
            # Do not relabel transport/parser issues as process_error downstream.
            pass
        return {
            "ok": ok and not self._cancel_requested,
            "failure_type": failure_type,
            "exit_code": proc.returncode,
            "error": error,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "events_path": str(proc_events_path),
            "cursor_events_path": str(cursor_events_path),
            "cursor_stream_text": stdout_text,
            "cursor_extracted": extracted,
            "stream_completed": stream_completed,
            "stream_terminal_status": stream_terminal_status,
            "recovered_from_stream": recovered_from_stream,
            "stream_read_error": stream_read_error,
        }


    async def emit(self, event_type: str, **payload: Any) -> None:
        event = {"ts": _now(), "run_id": self.run_id, "type": event_type, **payload}
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, sort_keys=True) + "\n")
        if self._event_queue is not None:
            await self._event_queue.put(event)

    async def write_status(self, *, state: str) -> None:
        status = {
            "schema_version": "scillm.exec.status.v1",
            "run_id": self.run_id,
            "state": state,
            "updated_at": _now(),
            "node_results": self.node_results,
            "cancel_requested": self._cancel_requested,
            "paused": self._has_pause_gate(),
            "paused_graph": self._paused_graph,
            "paused_node_ids": sorted(self._paused_node_ids),
            "disabled_node_ids": sorted(self._disabled_node_ids),
            "running_node_ids": sorted(self._running_node_ids),
            "runtime_actions": self._action_history,
            "artifacts": self._artifact_summary(),
        }
        tmp = self.status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, indent=2), encoding="utf-8")
        tmp.replace(self.status_path)

    def _has_pause_gate(self) -> bool:
        return self._paused_graph or bool(self._paused_node_ids)

    def _is_node_paused(self, node_id: str) -> bool:
        return self._paused_graph or node_id in self._paused_node_ids

    def _resolve_action_nodes(self, spec: ExecGraphActionRequest) -> list[str]:
        if spec.target == "graph":
            return sorted(self._graph_nodes)
        if not spec.node_id:
            raise ProxyError(400, "node_id is required when target is node or subtree", "invalid_request_error")
        safe_node_id = _safe_id(spec.node_id)
        if safe_node_id not in self._graph_nodes:
            raise ProxyError(404, f"exec graph node not found: {safe_node_id}", "not_found")
        if spec.target == "node":
            return [safe_node_id]
        return sorted({safe_node_id, *_descendants(safe_node_id, self._graph_children)})

    def _append_action(
        self,
        *,
        action: str,
        target: str,
        node_id: str | None,
        affected_node_ids: list[str],
        actor: str,
        reason: str | None,
        provenance: dict[str, Any],
        status: str,
    ) -> dict[str, Any]:
        entry = {
            "schema_version": "scillm.exec.runtime_action.v1",
            "action_id": hashlib.sha256(
                _canonical_json(
                    {
                        "run_id": self.run_id,
                        "action": action,
                        "target": target,
                        "node_id": node_id,
                        "affected_node_ids": affected_node_ids,
                        "actor": actor,
                        "reason": reason,
                        "provenance": provenance,
                        "index": len(self._action_history),
                    }
                ).encode("utf-8")
            ).hexdigest()[:24],
            "run_id": self.run_id,
            "action": action,
            "target": target,
            "node_id": _safe_id(node_id) if node_id else None,
            "affected_node_ids": affected_node_ids,
            "actor": actor,
            "reason": reason,
            "provenance": provenance,
            "status": status,
            "created_at": _now(),
        }
        self._action_history.append(entry)
        return entry

    def _artifact_summary(self) -> dict[str, str]:
        return {
            "run_dir": str(self.run_dir),
            "events_jsonl": str(self.events_path),
            "status_json": str(self.status_path),
            "execution_result_json": str(self.run_dir / "execution_result.json"),
        }

    def _attach_output_evidence(self, *, node: ExecNode, result: dict[str, Any], node_dir: Path) -> None:
        if result.get("status") == "skipped":
            return

        artifact_hashes: dict[str, dict[str, str]] = {}
        for key in (
            "stdout_path",
            "stderr_path",
            "events_path",
            "final_json_path",
            "response_path",
            "transcript_path",
            "terminal_evidence_path",
        ):
            value = result.get(key)
            if not value:
                continue
            path = Path(str(value))
            if path.exists() and path.is_file():
                artifact_hashes[key.removesuffix("_path")] = {
                    "path": str(path),
                    "sha256": _sha256_file(path),
                }

        output_payload = {
            "schema_version": "scillm.exec.node_output.v1",
            "run_id": self.run_id,
            "node_id": node.id,
            "runner": node.type,
            "attempt": result.get("attempt"),
            "ok": result.get("ok"),
            "exit_code": result.get("exit_code"),
            "failure_type": result.get("failure_type"),
            "schema_validated": result.get("schema_validated"),
            "result": result.get("result"),
            "artifact_hashes": artifact_hashes,
            "terminal_evidence": result.get("terminal_evidence"),
        }
        output_artifact = node_dir / "output.evidence.json"
        output_artifact.write_text(_canonical_json(output_payload) + "\n", encoding="utf-8")
        result["output_hash"] = _sha256_json(output_payload)
        result["output_artifact"] = str(output_artifact)
        result["output_hash_algorithm"] = "sha256.canonical_json.scillm_exec_node_output.v1"
        result["evidence_status"] = "hash_bound" if result.get("ok") else "failed_hash_bound"




async def _iter_subprocess_text_lines(
    stream: asyncio.StreamReader,
    *,
    read_size: int = 65536,
) -> Any:
    """Yield newline-delimited lines without asyncio StreamReader.readline() 64KiB cap."""
    buffer = b""
    while True:
        chunk = await stream.read(read_size)
        if not chunk:
            if buffer:
                yield buffer.decode(errors="replace")
            return
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            yield line.decode(errors="replace")


def _ingest_cursor_events_file(events_path: Path) -> dict[str, Any]:
    """Authoritative terminal parse from persisted cursor-events.jsonl."""
    state = _new_cursor_stream_state()
    if not events_path.is_file():
        return _finalize_cursor_stream_state(state)
    for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
        event = _parse_cursor_stream_line(line)
        if event is not None:
            _ingest_cursor_stream_event(state, event)
    return _finalize_cursor_stream_state(state)


def _merge_cursor_extracted(
    primary: dict[str, Any],
    from_events_file: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(primary)
    file_events = from_events_file.get("events") or []
    primary_events = primary.get("events") or []
    if len(file_events) >= len(primary_events):
        for key in (
            "session_id",
            "model",
            "api_key_source",
            "tool_call_count",
            "text",
            "result_event",
            "events",
        ):
            value = from_events_file.get(key)
            if value is not None:
                merged[key] = value
    return merged


def _apply_cursor_events_file_terminal(
    *,
    events_path: Path,
    extracted: dict[str, Any],
    stream_completed: bool,
    stream_terminal_status: str | None,
    failure_type: str | None,
    error: str | None,
    proc_returncode: int | None,
) -> tuple[dict[str, Any], bool, str | None, str | None, str | None, bool]:
    """Prefer on-disk cursor-events.jsonl for terminal success/error when present."""
    file_extracted = _ingest_cursor_events_file(events_path)
    extracted = _merge_cursor_extracted(extracted, file_extracted)
    result_event = extracted.get("result_event")
    if not isinstance(result_event, dict):
        return extracted, stream_completed, stream_terminal_status, failure_type, error, False

    terminal = _cursor_stream_terminal_status(result_event)
    if terminal == "success":
        return (
            extracted,
            True,
            "success",
            None if failure_type in {None, "", "process_error", "stream_read_error"} else failure_type,
            None if failure_type in {None, "", "process_error", "stream_read_error"} else error,
            True,
        )
    if terminal == "error":
        return (
            extracted,
            True,
            "error",
            failure_type or "cursor_error",
            error or str(result_event.get("result") or result_event.get("message") or "cursor result error"),
            False,
        )
    return extracted, stream_completed, stream_terminal_status, failure_type, error, False


def create_exec_router(check_auth: AuthCheck) -> APIRouter:
    router = APIRouter()

    async def auth(request: Request) -> str:
        err = check_auth(request)
        if err:
            raise ProxyError(401, err, "authentication_error")
        return request.headers.get("authorization", "")

    async def registered_stream(run: ExecRun, iterator: AsyncIterator[str]) -> AsyncIterator[str]:
        async with _registered_run(run):
            async for chunk in iterator:
                yield chunk

    @router.get("/exec/review-catalog")
    async def exec_review_catalog(request: Request, skill: str = "review-code"):
        await auth(request)
        return JSONResponse(await _load_review_catalog(skill))

    @router.post("/exec/review-catalog/{kind}")
    async def exec_review_catalog_save(request: Request, kind: str, skill: str = "review-code"):
        await auth(request)
        body = await request.json()
        spec = ReviewCatalogSaveRequest.model_validate(body)
        result = _save_review_catalog_entry(skill=skill, kind=kind, spec=spec)
        collection = _review_catalog_collection()
        memory_result = await _memory_upsert(
            collection=collection,
            documents=[
                _build_review_catalog_document(
                    skill=result["skill"],
                    kind=kind,
                    entry=result["entry"],
                    caller=request.headers.get("x-caller-skill", "scillm-exec-review-catalog"),
                )
            ],
        )
        result["collection"] = collection
        result["memory"] = memory_result
        return JSONResponse(result)

    @router.post("/exec")
    async def exec_one(request: Request):
        auth_header = await auth(request)
        body = await request.json()
        spec = ExecRequest.model_validate(body)
        run = ExecRun(
            run_id=spec.run_id or f"exec-{uuid.uuid4().hex[:12]}",
            artifact_root=_artifact_root(),
            auth_header=auth_header,
            caller_skill=request.headers.get("x-caller-skill", "scillm-exec"),
        )
        node = _exec_request_to_node(spec)
        if spec.stream:
            return StreamingResponse(
                registered_stream(run, run.stream_single(node)),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        async with _registered_run(run):
            final = await run.run_single(node)
            if node.type == "cursor_exec":
                final = _slim_exec_http_payload(final)
            return JSONResponse(final)

    @router.post("/exec/batch")
    async def exec_batch(request: Request):
        auth_header = await auth(request)
        body = await request.json()
        spec = ExecBatchRequest.model_validate(body)
        nodes = []
        for item in spec.items:
            merged = {**spec.defaults, **item.model_dump(exclude_none=True)}
            merged.setdefault("graph_goal", spec.graph_goal)
            merged["metadata"] = {**spec.metadata, **merged.get("metadata", {})}
            nodes.append(ExecNode.model_validate(merged))
        graph = ExecGraphRequest(
            graph_id=spec.batch_id,
            graph_goal=spec.graph_goal,
            max_concurrency=spec.max_concurrency,
            stream=spec.stream,
            metadata=spec.metadata,
            nodes=nodes,
        )
        run = ExecRun(
            run_id=spec.batch_id,
            artifact_root=_artifact_root(),
            auth_header=auth_header,
            caller_skill=request.headers.get("x-caller-skill", "scillm-exec-batch"),
        )
        if spec.stream:
            return StreamingResponse(
                registered_stream(run, run.stream_graph(graph)),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        async with _registered_run(run):
            return JSONResponse(await run.run_graph(graph))

    @router.post("/dag/validate")
    async def dag_validate(request: Request):
        await auth(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise ProxyError(400, "DAG validate body must be a JSON object", "invalid_request_error")
        json_out = bool(body.get("json") or body.get("json_out"))
        dag = body.get("dag") if isinstance(body.get("dag"), dict) else body
        if not isinstance(dag, dict):
            raise ProxyError(400, "DAG validate requires a dag object", "invalid_request_error")
        try:
            result = run_phart_on_dag(dag, "validate", json_out=json_out)
        except FileNotFoundError as exc:
            raise ProxyError(503, str(exc), "service_unavailable") from exc
        payload = result.as_dict()
        if json_out and result.stdout.strip():
            try:
                payload["report"] = json.loads(result.stdout)
            except json.JSONDecodeError:
                payload["report_raw"] = result.stdout
        status = 200 if result.ok else 422
        return JSONResponse(payload, status_code=status)

    @router.post("/dag/chart")
    async def dag_chart(request: Request):
        await auth(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise ProxyError(400, "DAG chart body must be a JSON object", "invalid_request_error")
        dag = body.get("dag") if isinstance(body.get("dag"), dict) else body
        if not isinstance(dag, dict):
            raise ProxyError(400, "DAG chart requires a dag object", "invalid_request_error")
        try:
            result = run_phart_on_dag(dag, "chart")
        except FileNotFoundError as exc:
            raise ProxyError(503, str(exc), "service_unavailable") from exc
        payload = result.as_dict()
        status = 200 if result.ok else 422
        return JSONResponse(payload, status_code=status)

    @router.post("/exec/graph")
    async def exec_graph(request: Request):
        auth_header = await auth(request)
        body = await request.json()
        try:
            spec = ExecGraphRequest.model_validate(body)
        except ValidationError as exc:
            raise _exec_validation_error(exc, "exec graph request") from exc
        run = ExecRun(
            run_id=spec.graph_id,
            artifact_root=_artifact_root(),
            auth_header=auth_header,
            caller_skill=request.headers.get("x-caller-skill", "scillm-exec-graph"),
        )
        if spec.stream:
            return StreamingResponse(
                registered_stream(run, run.stream_graph(spec)),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        async with _registered_run(run):
            return JSONResponse(await run.run_graph(spec))

    @router.post("/exec/graph/amendments")
    async def exec_graph_amendment(request: Request):
        await auth(request)
        body = await request.json()
        spec = ExecGraphAmendmentRequest.model_validate(body)
        if spec.graph_id != spec.base_graph.graph_id or spec.graph_id != spec.draft_graph.graph_id:
            raise ProxyError(400, "graph_id must match base_graph and draft_graph", "invalid_request_error")

        base_nodes = [_apply_graph_defaults(spec.base_graph, node) for node in spec.base_graph.nodes]
        draft_nodes = [_apply_graph_defaults(spec.draft_graph, node) for node in spec.draft_graph.nodes]
        _validate_graph_contract(spec.base_graph, base_nodes, label="base_graph")
        _validate_graph_contract(spec.draft_graph, draft_nodes, label="draft_graph")
        _validate_graph(base_nodes)
        _validate_graph(draft_nodes)

        document = _build_graph_amendment_document(spec, caller=request.headers.get("x-caller-skill", "scillm-exec-graph-editor"))
        collection = os.environ.get("SCILLM_EXEC_AMENDMENT_COLLECTION", "scillm_exec_graph_amendments")
        memory_result = await _memory_upsert(collection=collection, documents=[document])
        return JSONResponse(
            {
                "ok": True,
                "collection": collection,
                "amendment_key": document["_key"],
                "graph_id": spec.graph_id,
                "base_graph_sha256": document["base_graph_sha256"],
                "draft_graph_sha256": document["draft_graph_sha256"],
                "memory": memory_result,
            }
        )

    @router.get("/exec/graph/{graph_id}/amendments")
    async def exec_graph_amendments(request: Request, graph_id: str, limit: int = 50, status: str | None = None):
        await auth(request)
        safe_limit = max(1, min(int(limit), 200))
        collection = os.environ.get("SCILLM_EXEC_AMENDMENT_COLLECTION", "scillm_exec_graph_amendments")
        filters: dict[str, Any] = {"graph_id": graph_id}
        if status:
            if status not in {"proposed", "approved", "rejected", "superseded"}:
                raise ProxyError(400, "status must be proposed, approved, rejected, or superseded", "invalid_request_error")
            filters["status"] = status
        data = await _memory_list(
            collection=collection,
            filters=filters,
            limit=safe_limit,
            sort_field="updated_at",
            sort_order="DESC",
        )
        return JSONResponse(
            {
                "ok": True,
                "collection": collection,
                "graph_id": graph_id,
                "count": data.get("count", 0),
                "total": data.get("total", data.get("count", 0)),
                "amendments": data.get("documents", []),
            }
        )

    @router.get("/exec/graph/amendments/{amendment_key}")
    async def exec_graph_amendment_get(request: Request, amendment_key: str):
        await auth(request)
        collection = os.environ.get("SCILLM_EXEC_AMENDMENT_COLLECTION", "scillm_exec_graph_amendments")
        document = await _memory_get(collection=collection, key=_safe_id(amendment_key))
        return JSONResponse({"ok": True, "collection": collection, "amendment": document})

    @router.post("/exec/graph/amendments/{amendment_key}/status")
    async def exec_graph_amendment_status(request: Request, amendment_key: str):
        await auth(request)
        body = await request.json()
        spec = ExecGraphAmendmentStatusRequest.model_validate(body)
        collection = os.environ.get("SCILLM_EXEC_AMENDMENT_COLLECTION", "scillm_exec_graph_amendments")
        key = _safe_id(amendment_key)
        existing = await _memory_get(collection=collection, key=key)
        now = _now()
        status_event = {
            "status": spec.status,
            "actor": spec.actor,
            "reason": spec.reason,
            "updated_at": now,
        }
        history = existing.get("status_history") if isinstance(existing.get("status_history"), list) else []
        patch = {
            "_key": key,
            "status": spec.status,
            "status_actor": spec.actor,
            "status_reason": spec.reason,
            "status_updated_at": now,
            "updated_at": now,
            "status_history": [*history, status_event],
        }
        memory_result = await _memory_upsert(collection=collection, documents=[patch])
        return JSONResponse(
            {
                "ok": True,
                "collection": collection,
                "amendment_key": key,
                "graph_id": existing.get("graph_id"),
                "status": spec.status,
                "memory": memory_result,
            }
        )

    @router.post("/exec/graph/amendments/{amendment_key}/apply")
    async def exec_graph_amendment_apply(request: Request, amendment_key: str):
        await auth(request)
        body = await request.json()
        spec = ExecGraphAmendmentApplyRequest.model_validate(body)
        collection = os.environ.get("SCILLM_EXEC_AMENDMENT_COLLECTION", "scillm_exec_graph_amendments")
        key = _safe_id(amendment_key)
        existing = await _memory_get(collection=collection, key=key)
        if existing.get("status") != "approved":
            raise ProxyError(409, "only approved amendments can be applied", "invalid_request_error")

        base_payload = existing.get("base_graph")
        draft_payload = existing.get("draft_graph")
        if not isinstance(base_payload, dict) or not isinstance(draft_payload, dict):
            raise ProxyError(409, "amendment must include base_graph and draft_graph payloads before apply", "invalid_request_error")

        try:
            base_graph = ExecGraphRequest.model_validate(base_payload)
            draft_graph = ExecGraphRequest.model_validate(draft_payload)
        except ValidationError as exc:
            raise _exec_validation_error(exc, "stored amendment graph") from exc

        graph_id = str(existing.get("graph_id") or draft_graph.graph_id)
        if graph_id != base_graph.graph_id or graph_id != draft_graph.graph_id:
            raise ProxyError(409, "stored amendment graph_id does not match base_graph and draft_graph", "invalid_request_error")

        base_nodes = [_apply_graph_defaults(base_graph, node) for node in base_graph.nodes]
        draft_nodes = [_apply_graph_defaults(draft_graph, node) for node in draft_graph.nodes]
        _validate_graph_contract(base_graph, base_nodes, label="base_graph")
        _validate_graph_contract(draft_graph, draft_nodes, label="draft_graph")
        _validate_graph(base_nodes)
        _validate_graph(draft_nodes)

        canonical_base = base_graph.model_dump(mode="json", exclude_none=True)
        canonical_draft = draft_graph.model_dump(mode="json", exclude_none=True)
        base_sha = _sha256_json(canonical_base)
        draft_sha = _sha256_json(canonical_draft)
        stored_base_sha = existing.get("base_graph_sha256")
        stored_draft_sha = existing.get("draft_graph_sha256")
        if stored_base_sha and stored_base_sha != base_sha:
            raise ProxyError(409, "stored base_graph_sha256 does not match base_graph payload", "invalid_request_error")
        if stored_draft_sha and stored_draft_sha != draft_sha:
            raise ProxyError(409, "stored draft_graph_sha256 does not match draft_graph payload", "invalid_request_error")
        if spec.expected_base_graph_sha256 and spec.expected_base_graph_sha256 != base_sha:
            raise ProxyError(409, "expected_base_graph_sha256 does not match amendment base graph", "invalid_request_error")

        if existing.get("apply_status") == "applied":
            if existing.get("applied_graph_sha256") and existing.get("applied_graph_sha256") != draft_sha:
                raise ProxyError(409, "amendment apply ledger conflicts with draft graph hash", "invalid_request_error")
            return JSONResponse(
                {
                    "ok": True,
                    "already_applied": True,
                    "collection": collection,
                    "amendment_key": key,
                    "graph_id": graph_id,
                    "apply_status": "applied",
                    "base_graph_sha256": base_sha,
                    "applied_graph_sha256": draft_sha,
                    "applied_graph": canonical_draft,
                    "applied_at": existing.get("applied_at"),
                    "applied_by": existing.get("applied_by"),
                }
            )

        now = _now()
        apply_event = {
            "status": "applied",
            "actor": spec.actor,
            "reason": spec.reason,
            "applied_at": now,
            "base_graph_sha256": base_sha,
            "applied_graph_sha256": draft_sha,
            "provenance": spec.provenance,
        }
        history = existing.get("apply_history") if isinstance(existing.get("apply_history"), list) else []
        patch = {
            "_key": key,
            "apply_status": "applied",
            "applied_by": spec.actor,
            "apply_reason": spec.reason,
            "applied_at": now,
            "updated_at": now,
            "base_graph_sha256": base_sha,
            "draft_graph_sha256": draft_sha,
            "applied_graph_sha256": draft_sha,
            "apply_provenance": spec.provenance,
            "apply_history": [*history, apply_event],
        }
        memory_result = await _memory_upsert(collection=collection, documents=[patch])
        return JSONResponse(
            {
                "ok": True,
                "already_applied": False,
                "collection": collection,
                "amendment_key": key,
                "graph_id": graph_id,
                "apply_status": "applied",
                "base_graph_sha256": base_sha,
                "applied_graph_sha256": draft_sha,
                "applied_graph": canonical_draft,
                "memory": memory_result,
            }
        )

    @router.get("/exec/{run_id}/status")
    async def exec_status(request: Request, run_id: str):
        await auth(request)
        path = _artifact_root() / _safe_id(run_id) / "status.json"
        if not path.exists():
            raise ProxyError(404, f"exec run not found: {run_id}", "not_found")
        return JSONResponse(json.loads(path.read_text()))

    @router.get("/exec/{run_id}/events")
    async def exec_events(request: Request, run_id: str, tail: int = 200):
        await auth(request)
        path = _artifact_root() / _safe_id(run_id) / "events.jsonl"
        if not path.exists():
            raise ProxyError(404, f"exec events not found: {run_id}", "not_found")
        lines = path.read_text(errors="replace").splitlines()
        if tail < 1:
            raise ProxyError(400, "tail must be >= 1", "invalid_request_error")
        tail = min(tail, 5000)
        events: list[dict[str, Any]] = []
        for line in lines[-tail:]:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return JSONResponse({"run_id": _safe_id(run_id), "events": events})

    @router.get("/exec/{run_id}/actions")
    async def exec_actions(request: Request, run_id: str):
        await auth(request)
        safe_run_id = _safe_id(run_id)
        async with _ACTIVE_LOCK:
            run = _ACTIVE_RUNS.get(safe_run_id)
        if run is not None:
            return JSONResponse(
                {
                    "ok": True,
                    "run_id": safe_run_id,
                    "active": True,
                    "runtime_actions": run._action_history,
                    "paused": run._has_pause_gate(),
                    "paused_node_ids": sorted(run._paused_node_ids),
                    "disabled_node_ids": sorted(run._disabled_node_ids),
                }
            )

        path = _artifact_root() / safe_run_id / "status.json"
        if not path.exists():
            raise ProxyError(404, f"exec run not found: {safe_run_id}", "not_found")
        status = json.loads(path.read_text())
        return JSONResponse(
            {
                "ok": True,
                "run_id": safe_run_id,
                "active": False,
                "runtime_actions": status.get("runtime_actions", []),
                "paused": status.get("paused", False),
                "paused_node_ids": status.get("paused_node_ids", []),
                "disabled_node_ids": status.get("disabled_node_ids", []),
            }
        )

    @router.post("/exec/{run_id}/actions")
    async def exec_action(request: Request, run_id: str):
        await auth(request)
        safe_run_id = _safe_id(run_id)
        body = await request.json()
        spec = ExecGraphActionRequest.model_validate(body)
        async with _ACTIVE_LOCK:
            run = _ACTIVE_RUNS.get(safe_run_id)
        if run is None:
            raise ProxyError(404, f"active exec run not found: {safe_run_id}", "not_found")
        return JSONResponse(await run.apply_action(spec))

    @router.post("/exec/{run_id}/cancel")
    async def exec_cancel(request: Request, run_id: str):
        await auth(request)
        safe_run_id = _safe_id(run_id)
        async with _ACTIVE_LOCK:
            run = _ACTIVE_RUNS.get(safe_run_id)
        if run is None:
            raise ProxyError(404, f"active exec run not found: {safe_run_id}", "not_found")
        killed = await run.cancel(actor=request.headers.get("x-caller-skill", "unknown"), reason="legacy cancel endpoint")
        return JSONResponse({"run_id": safe_run_id, "cancel_requested": True, "killed_processes": killed})

    return router


async def _load_review_catalog(skill: str) -> dict[str, Any]:
    safe_skill = _safe_catalog_name(skill)
    root = _review_catalog_root()
    agents = _load_catalog_entries(root / "agents" / safe_skill, kind="agent", skill=safe_skill)
    contracts = _load_catalog_entries(root / "contracts" / safe_skill, kind="contract", skill=safe_skill)
    collection = _review_catalog_collection()
    memory_status: dict[str, Any] = {"state": "not_checked", "collection": collection}
    try:
        data = await _memory_list(collection=collection, filters={"skill": safe_skill}, limit=500)
        documents = [doc for doc in data.get("documents", []) if isinstance(doc, dict)]
        agents = _merge_catalog_entries(
            agents,
            [doc["entry"] for doc in documents if doc.get("kind") == "agents" and isinstance(doc.get("entry"), dict)],
        )
        contracts = _merge_catalog_entries(
            contracts,
            [doc["entry"] for doc in documents if doc.get("kind") == "contracts" and isinstance(doc.get("entry"), dict)],
        )
        memory_status = {"state": "connected", "collection": collection, "count": len(documents)}
    except ProxyError as exc:
        memory_status = {
            "state": "unavailable",
            "collection": collection,
            "error_type": exc.error_type,
            "message": exc.message,
        }
    return {
        "ok": True,
        "schema_version": "scillm.exec.review_catalog.v1",
        "skill": safe_skill,
        "source_root": str(root),
        "catalog_store": {
            "runtime": "scillm",
            "durable_store": "memory",
            "collection": collection,
            "memory_status": memory_status,
        },
        "agents": agents,
        "contracts": contracts,
        "default_contracts": [entry["id"] for entry in contracts if entry.get("default") is True],
    }


def _save_review_catalog_entry(*, skill: str, kind: str, spec: ReviewCatalogSaveRequest) -> dict[str, Any]:
    safe_skill = _safe_catalog_name(skill)
    if kind not in {"agents", "contracts"}:
        raise ProxyError(400, "catalog kind must be agents or contracts", "invalid_request_error")
    entry_id = _safe_catalog_name(spec.id)
    root = _review_catalog_root()
    directory = root / kind / safe_skill
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{entry_id}.md"
    if path.exists() and not spec.overwrite:
        raise ProxyError(409, f"catalog entry already exists: {entry_id}", "conflict")
    path.write_text(_render_catalog_markdown(spec), encoding="utf-8")
    metadata, body = _read_catalog_markdown(path)
    return {
        "ok": True,
        "schema_version": "scillm.exec.review_catalog_entry.v1",
        "skill": safe_skill,
        "kind": kind,
        "entry": _with_catalog_identity({
            "id": entry_id,
            "version": str(metadata.get("version") or spec.version or "1"),
            "kind": "agent" if kind == "agents" else "contract",
            "label": str(metadata.get("label") or entry_id).strip(),
            "description": str(metadata.get("description") or "").strip(),
            "default_agent": _optional_str(metadata.get("default_agent")),
            "default_model": _optional_str(metadata.get("default_model")),
            "default_preset": _optional_str(metadata.get("default_preset")),
            "review_level": _optional_str(metadata.get("review_level")),
            "proof_level": _optional_str(metadata.get("proof_level")),
            "reducer_policy": _optional_str(metadata.get("reducer_policy")),
            "read_only": metadata.get("read_only") is not False,
            "evidence_required": metadata.get("evidence_required") is not False,
            "closure_authority": _optional_str(metadata.get("closure_authority")),
            "risk_triggers": _optional_str_list(metadata.get("risk_triggers")),
            "best_practice_skills": _optional_str_list(metadata.get("best_practice_skills")),
            "compatible_node_types": _optional_str_list(metadata.get("compatible_node_types")),
            "compatible_upstream_types": _optional_str_list(metadata.get("compatible_upstream_types")),
            "compatible_downstream_types": _optional_str_list(metadata.get("compatible_downstream_types")),
            "required_fields": _optional_str_list(metadata.get("required_fields")),
            "default": metadata.get("default") is True,
            "order": _optional_int(metadata.get("order")),
            "prompt": body.strip(),
            "source_path": str(path),
        }, skill=safe_skill, kind=kind),
    }


def _render_catalog_markdown(spec: ReviewCatalogSaveRequest) -> str:
    fields: list[tuple[str, object | None]] = [
        ("id", spec.id),
        ("version", spec.version),
        ("label", spec.label),
        ("description", spec.description),
        ("default_agent", spec.default_agent),
        ("default_model", spec.default_model),
        ("default_preset", spec.default_preset),
        ("review_level", spec.review_level),
        ("proof_level", spec.proof_level),
        ("reducer_policy", spec.reducer_policy),
        ("read_only", spec.read_only),
        ("evidence_required", spec.evidence_required),
        ("closure_authority", spec.closure_authority),
        ("risk_triggers", spec.risk_triggers),
        ("best_practice_skills", spec.best_practice_skills),
        ("compatible_node_types", spec.compatible_node_types),
        ("compatible_upstream_types", spec.compatible_upstream_types),
        ("compatible_downstream_types", spec.compatible_downstream_types),
        ("required_fields", spec.required_fields),
        ("default", spec.default),
        ("order", spec.order),
    ]
    frontmatter = ["---"]
    for key, value in fields:
        if value is None or value == "":
            continue
        frontmatter.append(f"{key}: {_frontmatter_value(value)}")
    frontmatter.append("---")
    return "\n".join(frontmatter) + "\n" + spec.prompt.strip() + "\n"


def _frontmatter_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return json.dumps(value)
    text = str(value)
    if re.fullmatch(r"[A-Za-z0-9_.:/ -]+", text):
        return text
    return json.dumps(text)


def _review_catalog_root() -> Path:
    configured = (
        os.environ.get("SCILLM_REVIEW_CATALOG_ROOT")
        or os.environ.get("AGENT_SKILLS_ROOT")
        or "/home/graham/workspace/experiments/agent-skills"
    )
    return Path(configured).expanduser().resolve()


def _review_catalog_collection() -> str:
    return os.environ.get("SCILLM_REVIEW_CATALOG_COLLECTION", "scillm_exec_review_catalog")


def _safe_catalog_name(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or not re.fullmatch(r"[A-Za-z0-9_.-]+", cleaned):
        raise ProxyError(400, "catalog skill must be a simple name", "invalid_request_error")
    return cleaned


def _load_catalog_entries(directory: Path, *, kind: str, skill: str) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    entries: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.md")):
        metadata, body = _read_catalog_markdown(path)
        entry_id = str(metadata.get("id") or path.stem).strip()
        if not entry_id:
            continue
        entries.append(
            _with_catalog_identity({
                "id": entry_id,
                "version": str(metadata.get("version") or "1"),
                "kind": kind,
                "label": str(metadata.get("label") or entry_id).strip(),
                "description": str(metadata.get("description") or "").strip(),
                "default_agent": _optional_str(metadata.get("default_agent")),
                "default_model": _optional_str(metadata.get("default_model")),
                "default_preset": _optional_str(metadata.get("default_preset")),
                "review_level": _optional_str(metadata.get("review_level")),
                "proof_level": _optional_str(metadata.get("proof_level")),
                "reducer_policy": _optional_str(metadata.get("reducer_policy")),
                "read_only": metadata.get("read_only") is not False,
                "evidence_required": metadata.get("evidence_required") is not False,
                "closure_authority": _optional_str(metadata.get("closure_authority")),
                "risk_triggers": _optional_str_list(metadata.get("risk_triggers")),
                "best_practice_skills": _optional_str_list(metadata.get("best_practice_skills")),
                "compatible_node_types": _optional_str_list(metadata.get("compatible_node_types")),
                "compatible_upstream_types": _optional_str_list(metadata.get("compatible_upstream_types")),
                "compatible_downstream_types": _optional_str_list(metadata.get("compatible_downstream_types")),
                "required_fields": _optional_str_list(metadata.get("required_fields")),
                "default": metadata.get("default") is True,
                "order": _optional_int(metadata.get("order")),
                "prompt": body.strip(),
                "source_path": str(path),
            }, skill=skill, kind=f"{kind}s" if kind in {"agent", "contract"} else kind)
        )
    return sorted(entries, key=lambda entry: (entry.get("order") is None, entry.get("order") or 0, entry["id"]))


def _with_catalog_identity(entry: dict[str, Any], *, skill: str, kind: str) -> dict[str, Any]:
    normalized = dict(entry)
    entry_kind = "agent" if kind == "agents" else "contract" if kind == "contracts" else str(normalized.get("kind") or kind)
    normalized["kind"] = entry_kind
    normalized["version"] = str(normalized.get("version") or "1")
    normalized["catalog_id"] = f"{skill}.{entry_kind}.{normalized['id']}"
    identity_payload = {k: v for k, v in normalized.items() if k not in {"source_path", "catalog_sha256"}}
    normalized["catalog_sha256"] = _sha256_json(identity_payload)
    return normalized


def _merge_catalog_entries(base: list[dict[str, Any]], overlays: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = {entry["id"]: entry for entry in base}
    for entry in overlays:
        entry_id = str(entry.get("id") or "").strip()
        if entry_id:
            merged[entry_id] = entry
    return sorted(merged.values(), key=lambda entry: (entry.get("order") is None, entry.get("order") or 0, entry["id"]))


def _build_review_catalog_document(*, skill: str, kind: str, entry: dict[str, Any], caller: str) -> dict[str, Any]:
    now = _now()
    version = str(entry.get("version") or "1")
    key = _safe_id(f"review_catalog_{skill}_{kind}_{entry['id']}_{version}")
    return {
        "_key": key,
        "schema_version": "scillm.exec.review_catalog.memory.v1",
        "skill": skill,
        "kind": kind,
        "entry_id": entry["id"],
        "catalog_id": entry.get("catalog_id"),
        "catalog_version": version,
        "catalog_sha256": entry.get("catalog_sha256"),
        "entry": entry,
        "caller": caller,
        "created_at": now,
        "updated_at": now,
        "tags": ["Precision", "Resilience", "scope=scillm", "exec_review_catalog"],
    }


def _read_catalog_markdown(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        return {}, raw
    end = raw.find("\n---", 4)
    if end == -1:
        return {}, raw
    metadata = _parse_simple_frontmatter(raw[4:end])
    body_start = end + len("\n---")
    if raw[body_start : body_start + 1] == "\n":
        body_start += 1
    return metadata, raw[body_start:]


def _parse_simple_frontmatter(raw: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if value.lower() == "true":
            metadata[key] = True
        elif value.lower() == "false":
            metadata[key] = False
        elif value.startswith("[") or value.startswith("{"):
            try:
                metadata[key] = json.loads(value)
            except json.JSONDecodeError:
                metadata[key] = value
        else:
            metadata[key] = value
    return metadata


def _optional_str(value: object) -> str | None:
    if value is None or value is False:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None or value is False:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _optional_str_list(value: object) -> list[str]:
    if value is None or value is False:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


class _registered_run:
    def __init__(self, run: ExecRun) -> None:
        self.run = run

    async def __aenter__(self) -> ExecRun:
        async with _ACTIVE_LOCK:
            _ACTIVE_RUNS[self.run.run_id] = self.run
        return self.run

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        async with _ACTIVE_LOCK:
            _ACTIVE_RUNS.pop(self.run.run_id, None)


async def _remove_worktree(node: ExecNode, cwd: Path) -> None:
    source_cwd = Path(node.cwd or os.getcwd()).expanduser().resolve()
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(source_cwd),
        "worktree",
        "remove",
        "--force",
        str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    if cwd.exists():
        shutil.rmtree(str(cwd), ignore_errors=True)


def _exec_request_to_node(spec: ExecRequest) -> ExecNode:
    data = spec.model_dump(exclude={"exec_version", "run_id", "stream"}, exclude_none=True)
    data.setdefault("graph_goal", spec.node_goal)
    return ExecNode.model_validate(data)


def _exec_validation_error(exc: ValidationError, label: str) -> ProxyError:
    errors = exc.errors()
    unsupported_types = [
        {
            "path": ".".join(str(part) for part in error.get("loc", [])),
            "value": error.get("input"),
        }
        for error in errors
        if error.get("type") == "literal_error" and (error.get("loc") or [])[-1:] == ("type",)
    ]
    message = f"Invalid {label}: {len(errors)} validation error(s)."
    if unsupported_types:
        values = ", ".join(f"{item['path']}={item['value']!r}" for item in unsupported_types[:8])
        message += (
            f" Unsupported semantic planner node type(s): {values}. "
            "/v1/scillm/exec/graph runs runtime nodes only "
            "(local_command, deterministic_verifier, deterministic_render, scillm_call, opencode_serve, "
            "scillm_batch, codex_exec, opencode_exec, pi_exec, kimi_exec, cursor_exec, claude_print). Record semantic phase DAGs with "
            "$plan-iterate or compile them to runtime nodes before execution."
        )
    return ProxyError(
        400,
        message,
        "invalid_request_error",
        details={"validation_errors": errors[:20]},
    )


def _apply_graph_defaults(graph: ExecGraphRequest, node: ExecNode) -> ExecNode:
    data = node.model_dump(exclude_none=True)
    data.setdefault("graph_goal", graph.graph_goal)
    data.setdefault("cwd", graph.cwd)
    data.setdefault("model", graph.model)
    data.setdefault("sandbox", graph.sandbox)
    data.setdefault("worktree", graph.worktree.model_dump())
    data["metadata"] = {**graph.metadata, **data.get("metadata", {})}
    if node.persona_ref and node.persona_ref in graph.personas:
        persona = graph.personas[node.persona_ref]
        data["persona_text"] = data.get("persona_text") or persona.get("prompt") or persona.get("description")
    return ExecNode.model_validate(data)


def _graph_children(nodes: list[ExecNode]) -> dict[str, set[str]]:
    children: dict[str, set[str]] = {node.id: set() for node in nodes}
    for node in nodes:
        for dep in node.depends_on:
            children.setdefault(dep, set()).add(node.id)
    return children


def _descendants(node_id: str, children: dict[str, set[str]]) -> set[str]:
    found: set[str] = set()
    stack = list(children.get(node_id, set()))
    while stack:
        child = stack.pop()
        if child in found:
            continue
        found.add(child)
        stack.extend(children.get(child, set()))
    return found


def _validate_graph(nodes: list[ExecNode]) -> None:
    ids = [node.id for node in nodes]
    if len(ids) != len(set(ids)):
        raise ProxyError(400, "duplicate node ids in exec graph", "invalid_request_error")
    known = set(ids)
    for node in nodes:
        for dep in node.depends_on:
            if dep not in known:
                raise ProxyError(400, f"node {node.id} depends on unknown node {dep}", "invalid_request_error")

    # Cycle detection by DFS.
    visiting: set[str] = set()
    visited: set[str] = set()
    by_id = {node.id: node for node in nodes}

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        if node_id in visiting:
            raise ProxyError(400, f"cycle detected at node {node_id}", "invalid_request_error")
        visiting.add(node_id)
        for dep in by_id[node_id].depends_on:
            visit(dep)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in ids:
        visit(node_id)


def _validate_graph_contract(graph: ExecGraphRequest, nodes: list[ExecNode], *, label: str) -> None:
    if not graph.graph_id.strip():
        raise ProxyError(400, f"{label}.graph_id must be non-empty", "invalid_request_error")
    if not graph.graph_goal.strip():
        raise ProxyError(400, f"{label}.graph_goal must be non-empty", "invalid_request_error")
    if not nodes:
        raise ProxyError(400, f"{label}.nodes must contain at least one node", "invalid_request_error")
    if graph.self_improvement_iterations is not None and graph.self_improvement_iterations < 1:
        raise ProxyError(400, f"{label}.self_improvement_iterations must be a positive integer", "invalid_request_error")
    for field_name, limits, minimum in [
        ("review_fanout_limits", graph.review_fanout_limits, 0),
        ("review_iteration_limits", graph.review_iteration_limits, 1),
    ]:
        for key in ["review_code", "review_design", "review_prompt"]:
            value = limits.get(key)
            if value is not None and (not isinstance(value, int) or value < minimum):
                raise ProxyError(400, f"{label}.{field_name}.{key} must be an integer >= {minimum}", "invalid_request_error")
    for node in nodes:
        if not node.id.strip():
            raise ProxyError(400, f"{label}.nodes contains a node with an empty id", "invalid_request_error")
        if not str(node.type).strip():
            raise ProxyError(400, f"{label}.nodes[{node.id}].type must be non-empty", "invalid_request_error")
        if not node.node_goal.strip():
            raise ProxyError(400, f"{label}.nodes[{node.id}].node_goal must be non-empty", "invalid_request_error")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_graph_amendment_document(spec: ExecGraphAmendmentRequest, *, caller: str) -> dict[str, Any]:
    base_graph = spec.base_graph.model_dump(mode="json", exclude_none=True)
    draft_graph = spec.draft_graph.model_dump(mode="json", exclude_none=True)
    base_sha = _sha256_json(base_graph)
    draft_sha = _sha256_json(draft_graph)
    deterministic_id = spec.amendment_id or hashlib.sha256(
        f"{spec.graph_id}:{base_sha}:{draft_sha}".encode("utf-8")
    ).hexdigest()[:32]
    now = _now()
    return {
        "_key": f"exec_graph_amendment_{deterministic_id}",
        "schema_version": "scillm.exec.graph.amendment.memory.v1",
        "amendment_version": spec.amendment_version,
        "graph_id": spec.graph_id,
        "run_id": spec.run_id,
        "status": spec.status,
        "actor": spec.actor,
        "caller": caller,
        "base_graph_sha256": base_sha,
        "draft_graph_sha256": draft_sha,
        "base_graph": base_graph,
        "draft_graph": draft_graph,
        "diff": spec.diff,
        "validation": spec.validation,
        "warning_acceptance": spec.warning_acceptance,
        "provenance": {
            "source": "scillm_exec_graph_editor",
            **spec.provenance,
        },
        "created_at": now,
        "updated_at": now,
        "tags": ["Precision", "Resilience", "scope=scillm", "exec_graph_amendment"],
    }


async def _memory_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    uds = os.environ.get("SCILLM_MEMORY_UDS", "/run/user/1000/embry/memory.sock")
    base_url = os.environ.get("SCILLM_MEMORY_URL", "http://127.0.0.1:8601").rstrip("/")
    timeout = httpx.Timeout(float(os.environ.get("SCILLM_MEMORY_TIMEOUT_S", "10")), connect=2.0)
    allow_tcp_fallback = os.environ.get("SCILLM_MEMORY_ALLOW_TCP_FALLBACK", "").lower() in {"1", "true", "yes", "on"}
    if uds and not Path(uds).exists() and not allow_tcp_fallback:
        raise ProxyError(
            502,
            f"memory socket unavailable: {uds}",
            "memory_backend_error",
            advice="Start the Memory daemon or set SCILLM_MEMORY_ALLOW_TCP_FALLBACK=true explicitly for non-UDS environments.",
        )
    transport = httpx.AsyncHTTPTransport(uds=uds) if uds and Path(uds).exists() else None
    client_kwargs: dict[str, Any] = {"timeout": timeout}
    if transport is not None:
        client_kwargs["transport"] = transport
        base_url = "http://localhost"

    try:
        async with httpx.AsyncClient(base_url=base_url, **client_kwargs) as client:
            response = await client.post(path, json=payload)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        raise ProxyError(
            502,
            f"memory {path} failed: {exc.response.status_code} {exc.response.text[:500]}",
            "memory_backend_error",
        ) from exc
    except httpx.HTTPError as exc:
        raise ProxyError(502, f"memory {path} failed: {exc}", "memory_backend_error") from exc

    return data if isinstance(data, dict) else {"response": data}


async def _memory_upsert(*, collection: str, documents: list[dict[str, Any]]) -> dict[str, Any]:
    return await _memory_post("/upsert", {"collection": collection, "documents": documents})


async def _memory_list(
    *,
    collection: str,
    filters: dict[str, Any],
    limit: int = 50,
    sort_field: str = "updated_at",
    sort_order: str = "DESC",
) -> dict[str, Any]:
    return await _memory_post(
        "/list",
        {
            "collection": collection,
            "filters": filters,
            "limit": limit,
            "sort_field": sort_field,
            "sort_order": sort_order,
        },
    )


async def _memory_get(*, collection: str, key: str) -> dict[str, Any]:
    data = await _memory_list(collection=collection, filters={"_key": key}, limit=1)
    documents = data.get("documents", [])
    if not documents:
        raise ProxyError(404, f"exec graph amendment not found: {key}", "not_found")
    document = documents[0]
    if not isinstance(document, dict):
        raise ProxyError(502, f"memory returned malformed amendment: {key}", "memory_backend_error")
    return document


def _compact_upstream_result(result: dict[str, Any]) -> dict[str, Any]:
    """Shrink a predecessor node result for fan-in prompts."""
    compact: dict[str, Any] = {
        "ok": result.get("ok"),
        "runner": result.get("runner"),
        "failure_type": result.get("failure_type"),
    }
    if result.get("result") is not None:
        compact["result"] = result.get("result")
    stdout_path = result.get("stdout_path")
    if stdout_path:
        path = Path(str(stdout_path))
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            parsed = _parse_jsonish(text)
            if parsed is not None:
                compact["stdout_json"] = parsed
            elif text.strip():
                compact["stdout_tail"] = text[-2000:]
    return compact


def _assemble_prompt(
    node: ExecNode,
    *,
    upstream_results: dict[str, dict[str, Any]] | None = None,
) -> str:
    if node.prompt_path:
        task_prompt = Path(node.prompt_path).expanduser().read_text(encoding="utf-8")
    else:
        task_prompt = node.prompt or ""

    messages_text = ""
    if node.messages:
        messages_text = "\n\nMESSAGES:\n" + json.dumps(node.messages, indent=2)

    upstream_text = ""
    if upstream_results:
        payload = json.dumps(upstream_results, indent=2, ensure_ascii=True)
        if len(payload) > 12000:
            payload = payload[:12000] + "\n... [truncated]"
        upstream_text = "\n\nUPSTREAM NODE RESULTS (completed dependencies):\n" + payload

    persona = f"\nPERSONA:\n{node.persona_text}\n" if node.persona_text else ""
    forbidden = "\n".join(f"- {item}" for item in node.forbidden_decisions)
    return f"""You are a bounded scillm exec worker.

GRAPH GOAL:
{node.graph_goal or ""}

NODE GOAL:
{node.node_goal}

PROTOCOL ROLE:
{node.protocol_role or "worker"}
{persona}
BOUNDARIES / FORBIDDEN DECISIONS:
{forbidden}

Rules:
- Do not change the graph goal.
- Do not change project or phase contracts.
- Do not declare project or phase completion.
- If blocked, return a structured blocked result with missing evidence or decision needed.

TASK:
{task_prompt}{upstream_text}
{messages_text}
""".strip()


def _load_schema(node: ExecNode) -> dict[str, Any] | None:
    if node.output_schema_path:
        return json.loads(Path(node.output_schema_path).expanduser().read_text(encoding="utf-8"))
    return node.output_schema


def _write_schema_if_needed(node: ExecNode, attempt_dir: Path) -> Path | None:
    if node.output_schema_path:
        return Path(node.output_schema_path).expanduser().resolve()
    if not node.output_schema:
        return None
    path = attempt_dir / "output_schema.json"
    path.write_text(json.dumps(node.output_schema, indent=2), encoding="utf-8")
    return path


def _load_items(node: ExecNode) -> list[dict[str, Any]]:
    if node.items is not None:
        return node.items
    if node.manifest_path:
        path = Path(node.manifest_path).expanduser()
        items: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                items.append(json.loads(line))
        return items
    raise ValueError("scillm_batch requires items or manifest_path")




def _resolve_codex_exec_profile(model: str | None) -> dict[str, str]:
    profile_name = model or "codex-gpt-5.5"
    canonical = _CODEX_EXEC_PROFILE_ALIASES.get(profile_name, profile_name)
    spec = _CODEX_EXEC_PROFILES.get(canonical)
    if spec is None:
        aliases = ", ".join(f"{alias!r}->{target!r}" for alias, target in sorted(_CODEX_EXEC_PROFILE_ALIASES.items()))
        raise ProxyError(
            400,
            (
                "unsupported codex_exec model profile "
                f"{profile_name!r}; use one of {sorted(_CODEX_EXEC_PROFILES)}"
                + (f" (aliases: {aliases})" if aliases else "")
                + ". codex_exec profiles are not scillm chat routes; use "
                "POST /v1/chat/completions with model gpt-5.5 for one-shot HTTP calls."
            ),
            "invalid_request_error",
        )
    model_id = os.environ.get(spec["model_env"], spec["default_model"])
    return {"profile": canonical, "model": model_id}


def _codex_binary() -> str:
    return os.environ.get("SCILLM_CODEX_BINARY", "codex")


def _codex_model_for_node(node: ExecNode, profile: dict[str, str]) -> str:
    override = node.metadata.get("codex_model")
    if isinstance(override, str) and override.strip():
        return override.strip()
    return profile["model"]


def _codex_reasoning_args(node: ExecNode) -> list[str]:
    effort = node.reasoning_effort
    if not effort and isinstance(node.metadata.get("reasoning_effort"), str):
        effort = str(node.metadata["reasoning_effort"]).strip() or None
    if not effort:
        return []
    return ["-c", f'reasoning.effort="{effort}"']


def _build_codex_exec_command(
    *,
    node: ExecNode,
    profile: dict[str, str],
    schema_path: Path | None,
    result_path: Path,
) -> list[str]:
    command = [
        _codex_binary(),
        "exec",
        "--json",
        "--sandbox",
        node.sandbox,
        "--model",
        _codex_model_for_node(node, profile),
        *_codex_reasoning_args(node),
    ]
    if schema_path is not None:
        command.extend(["--output-schema", str(schema_path), "-o", str(result_path)])
    command.append("-")
    return command

def _resolve_opencode_exec_profile(model: str | None) -> dict[str, str]:
    profile_name = model or "oc-chutes-deepseek"
    spec = _OPENCODE_EXEC_PROFILES.get(profile_name)
    if spec is None:
        raise ProxyError(
            400,
            (
                "unsupported opencode_exec model profile "
                f"{profile_name!r}; use one of {sorted(_OPENCODE_EXEC_PROFILES)}. "
                "Raw scillm chat profiles such as 'chutes-deepseek' and direct "
                "'chutes/...' model ids are rejected because opencode_exec must "
                "own the headless OpenCode permission boundary explicitly."
            ),
            "invalid_request_error",
        )
    model_id = os.environ.get(spec["model_env"], spec["default_model"])
    if not model_id.startswith(f"{spec['provider']}/"):
        raise ProxyError(
            400,
            f"{spec['model_env']} must be an OpenCode provider/model id starting with {spec['provider']}/",
            "invalid_request_error",
        )
    return {"profile": profile_name, "provider": spec["provider"], "model": model_id}


def _resolve_pi_exec_profile(model: str | None) -> dict[str, str]:
    profile_name = model or "pi-chutes-kimi"
    spec = _PI_EXEC_PROFILES.get(profile_name)
    if spec is None:
        raise ProxyError(
            400,
            (
                "unsupported pi_exec model profile "
                f"{profile_name!r}; use one of {sorted(_PI_EXEC_PROFILES)}. "
                "Raw scillm chat profiles such as 'chutes-kimi' and direct "
                "'chutes/...' model ids are rejected because pi_exec must "
                "own the headless Pi permission boundary explicitly."
            ),
            "invalid_request_error",
        )
    model_id = os.environ.get(spec["model_env"], spec["default_model"])
    if spec["provider"] == "chutes" and "/" not in model_id:
        raise ProxyError(
            400,
            f"{spec['model_env']} must be a Pi provider model id such as moonshotai/Kimi-K2.6-TEE",
            "invalid_request_error",
        )
    return {"profile": profile_name, "provider": spec["provider"], "model": model_id}


def _pi_binary() -> str:
    return os.environ.get("SCILLM_PI_BINARY", "/home/graham/bin/pi")


def _opencode_agent_name(node: ExecNode) -> str:
    if node.sandbox == "read-only":
        return "scillm-exec-readonly"
    if node.sandbox == "workspace-write":
        return "scillm-exec-workspace-write"
    raise ProxyError(
        400,
        "opencode_exec does not support sandbox='danger-full-access'; use workspace-write with metadata.allow_write_paths",
        "invalid_request_error",
    )


def _opencode_prompt(prompt: str, schema: dict[str, Any] | None) -> str:
    if schema is None:
        return prompt
    return (
        f"{prompt}\n\n"
        "OUTPUT CONTRACT:\n"
        "Return one JSON object only. Do not wrap it in markdown. It must satisfy this JSON Schema:\n"
        f"{json.dumps(schema, indent=2, sort_keys=True)}"
    )


def _write_opencode_exec_config(*, node: ExecNode, profile: dict[str, str], attempt_dir: Path) -> Path:
    readonly_permission = {
        "skill": "deny",
        "bash": "deny",
        "write": "deny",
        "edit": "deny",
        "patch": "deny",
        "webfetch": "deny",
        "question": "deny",
        "task": "deny",
        "todowrite": "deny",
    }
    readonly_tools = {
        "read": True,
        "grep": True,
        "glob": True,
        "write": False,
        "edit": False,
        "patch": False,
        "bash": False,
        "webfetch": False,
        "skill": False,
        "task": False,
        "todowrite": False,
        "question": False,
    }
    workspace_permission = {
        **readonly_permission,
        "write": "allow",
        "edit": "allow",
        "patch": "allow",
    }
    workspace_tools = {
        **readonly_tools,
        "write": True,
        "edit": True,
        "patch": True,
    }
    config = {
        "$schema": "https://opencode.ai/config.json",
        "enabled_providers": [profile["provider"]],
        "share": "disabled",
        "model": profile["model"],
        "small_model": profile["model"],
        "default_agent": _opencode_agent_name(node),
        "agent": {
            "scillm-exec-readonly": {
                "description": "Read-only scillm exec worker through OpenCode on Chutes.",
                "mode": "primary",
                "model": profile["model"],
                "permission": readonly_permission,
                "tools": readonly_tools,
                "prompt": (
                    "You are a bounded exec worker. Return only the requested result. "
                    "Do not modify files. Do not call skills, web fetch, shell commands, or subagents."
                ),
            },
            "scillm-exec-workspace-write": {
                "description": "Workspace-write scillm exec worker through OpenCode on Chutes.",
                "mode": "primary",
                "model": profile["model"],
                "permission": workspace_permission,
                "tools": workspace_tools,
                "prompt": (
                    "You are a bounded exec worker. Modify only the files explicitly named in the task. "
                    "Do not call skills, web fetch, shell commands, or subagents. Return only the requested result."
                ),
            },
        },
    }
    path = attempt_dir / "opencode.config.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def _opencode_allow_write_patterns(node: ExecNode) -> list[str]:
    raw = node.metadata.get("allow_write_paths")
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        raise ProxyError(
            400,
            "workspace-write exec runners require metadata.allow_write_paths as a non-empty list of relative files, directories, or globs",
            "invalid_request_error",
        )
    return [item.strip().lstrip("/") for item in raw]


def _snapshot_files(root: Path) -> dict[str, str]:
    skip_dirs = {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".ask_artifacts",
        ".plan-iterate",
    }
    snapshot: dict[str, str] = {}
    for path in root.rglob("*"):
        if any(part in skip_dirs for part in path.relative_to(root).parts):
            continue
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            snapshot[rel] = _sha256_file(path)
    return snapshot


def _audit_write_allowlist(
    *,
    cwd: Path,
    before: dict[str, str],
    allow_patterns: list[str],
    ignore_patterns: list[str] | None = None,
) -> dict[str, Any]:
    after = _snapshot_files(cwd)
    changed = sorted(
        rel
        for rel in set(before) | set(after)
        if before.get(rel) != after.get(rel)
    )
    ignored = ignore_patterns or []
    violations = [
        rel
        for rel in changed
        if not _path_ignored(rel, ignored) and not _path_allowed(rel, allow_patterns)
    ]
    return {
        "allow_write_paths": allow_patterns,
        "ignore_paths": ignored,
        "changed_paths": changed,
        "violations": violations,
    }


def _resolve_cursor_exec_profile(model: str | None) -> dict[str, Any]:
    profile_name = model or "cursor-auto"
    spec = _CURSOR_EXEC_PROFILES.get(profile_name)
    if spec is None:
        raise ProxyError(
            400,
            (
                "unsupported cursor_exec model profile "
                f"{profile_name!r}; use one of {sorted(_CURSOR_EXEC_PROFILES)}. "
                "cursor_exec profiles are not scillm chat models; do not pass "
                "cursor-auto through /v1/chat/completions."
            ),
            "invalid_request_error",
        )
    return {"profile": profile_name, **spec}


def _cursor_agent_binary() -> str:
    configured = os.environ.get("SCILLM_CURSOR_AGENT_BINARY", "/home/graham/.local/bin/agent").strip()
    candidate = Path(configured).expanduser()
    if candidate.is_symlink():
        candidate = candidate.resolve()
    if candidate.is_file() and candidate.name == "agent":
        node_bin = candidate.parent / "node"
        if not node_bin.exists():
            share_binary = _cursor_agent_share_binary()
            if share_binary:
                candidate = Path(share_binary)
    if candidate.exists():
        return str(candidate)
    share = _cursor_agent_share_binary()
    if share:
        return share
    return configured


def _cursor_agent_share_binary() -> str | None:
    version_roots = [
        Path("/home/graham/.local/share/cursor-agent/versions"),
        Path.home() / ".local" / "share" / "cursor-agent" / "versions",
    ]
    for versions_root in version_roots:
        if not versions_root.is_dir():
            continue
        for version_dir in sorted(versions_root.iterdir(), reverse=True):
            candidate = version_dir / "cursor-agent"
            if candidate.is_file():
                return str(candidate)
    return None


def _cursor_skills_root() -> Path:
    return Path(os.environ.get("SCILLM_CURSOR_SKILLS_ROOT", str(Path.home() / ".claude" / "skills"))).expanduser()


def _cursor_rule_name(node: ExecNode) -> str:
    override = node.metadata.get("cursor_rule_name")
    if isinstance(override, str) and override.strip():
        return override.strip()
    return os.environ.get("SCILLM_CURSOR_RULE_NAME", "scillm-exec-selected-skills")


def _cursor_selected_skills(node: ExecNode) -> list[str]:
    raw = node.metadata.get("skills")
    if raw is None:
        raw = node.metadata.get("cursor_skills")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return [item.strip() for item in raw if item.strip()]
    raise ProxyError(
        400,
        "metadata.skills must be a comma-separated string or list of skill directory names",
        "invalid_request_error",
    )


def _cursor_model_for_node(node: ExecNode, profile: dict[str, Any]) -> str:
    override = node.metadata.get("cursor_model")
    if isinstance(override, str) and override.strip():
        return override.strip()
    return str(profile["cursor_model"])


def _cursor_force_for_node(node: ExecNode, profile: dict[str, Any]) -> bool:
    if profile.get("mode") == "plan":
        return False
    if "cursor_force" in node.metadata:
        return bool(node.metadata.get("cursor_force"))
    return bool(profile.get("default_force"))


def _resolve_cursor_api_key() -> str:
    key = os.environ.get("CURSOR_API_KEY", "").strip()
    if key:
        return key
    for zshrc in (Path("/home/graham/.zshrc"), Path.home() / ".zshrc"):
        if not zshrc.exists():
            continue
        for line in zshrc.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("export CURSOR_API_KEY="):
                continue
            value = line.split("=", 1)[1].strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            if value:
                return value
    raise ProxyError(
        400,
        "CURSOR_API_KEY is required for cursor_exec (export in env or ~/.zshrc)",
        "invalid_request_error",
    )


def _cursor_prompt(prompt: str, schema: dict[str, Any] | None, skills: list[str], run_ctx: Path) -> str:
    if skills:
        manifest_rel = f".scillm/cursor-headless/{run_ctx.name}/selected-skills.md"
        prompt = (
            f"{prompt}\n\n"
            "Selected skill manifest:\n\n"
            f"`{manifest_rel}`\n\n"
            "Execution contract:\n"
            "1. Read the selected skill manifest.\n"
            "2. Read only the needed selected SKILL.md files.\n"
            "3. Follow the selected skill instructions.\n"
            "4. Do not load unlisted skills.\n"
            "5. Produce a concise final summary with files changed, tests run, and selected skills used."
        )
    if schema is None:
        return prompt
    return (
        f"{prompt}\n\n"
        "OUTPUT CONTRACT:\n"
        "Return one JSON object only. Do not wrap it in markdown. It must satisfy this JSON Schema:\n"
        f"{json.dumps(schema, indent=2, sort_keys=True)}"
    )


def _materialize_cursor_harness(
    *,
    cwd: Path,
    run_ctx: Path,
    skills: list[str],
    prompt: str,
    rule_name: str,
) -> dict[str, Any]:
    selected_skills_dir = run_ctx / "skills"
    manifest_path = run_ctx / "selected-skills.md"
    prompt_out = run_ctx / "prompt.md"
    rule_dir = cwd / ".cursor" / "rules" / rule_name
    skills_root = _cursor_skills_root()

    run_ctx.mkdir(parents=True, exist_ok=True)
    selected_skills_dir.mkdir(parents=True, exist_ok=True)
    rule_dir.mkdir(parents=True, exist_ok=True)

    manifest_lines = ["# Selected skills for Cursor headless run", ""]
    if skills:
        for skill in skills:
            src = skills_root / skill
            skill_md = src / "SKILL.md"
            if not skill_md.exists():
                raise ProxyError(400, f"Missing skill: {skill_md}", "invalid_request_error")
            dest = selected_skills_dir / skill
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
            rel = f".scillm/cursor-headless/{run_ctx.name}/skills/{skill}/SKILL.md"
            manifest_lines.append(f"- `{skill}`: `{rel}`")
        manifest_lines.append("")
        rule_dir.joinpath("RULE.md").write_text(
            "\n".join(
                [
                    "---",
                    "description: Harness-selected skills for the current scillm cursor_exec run.",
                    "alwaysApply: true",
                    "---",
                    "",
                    "You are running under a harness-selected skill set.",
                    "",
                    "Selected skill manifest:",
                    "",
                    f"`.scillm/cursor-headless/{run_ctx.name}/selected-skills.md`",
                    "",
                    "Rules:",
                    "- Only use skills listed in that manifest.",
                    "- Before using a skill, read its `SKILL.md`.",
                    "- You may use scripts, references, and assets only under the selected skill directories copied into this run context.",
                    "- Do not search or load unrelated skill directories.",
                    "- At the end, report which selected skills you used.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    manifest_path.write_text("\n".join(manifest_lines), encoding="utf-8")
    prompt_out.write_text(prompt, encoding="utf-8")
    return {
        "run_ctx": str(run_ctx),
        "prompt_out": str(prompt_out),
        "prompt_text": prompt_out.read_text(encoding="utf-8"),
        "manifest": str(manifest_path) if skills else None,
        "rule_dir": str(rule_dir) if skills else None,
        "skills_root": str(skills_root),
    }


def _cursor_write_ignore_patterns(rule_name: str) -> list[str]:
    return [
        ".scillm/*",
        f".cursor/rules/{rule_name}/*",
        f".cursor/rules/{rule_name}",
    ]


def _path_ignored(path: str, patterns: list[str]) -> bool:
    if path == ".scillm" or path.startswith(".scillm/"):
        return True
    return _path_allowed(path, patterns)




def _slim_exec_http_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Omit multi-megabyte cursor stream bodies from HTTP JSON (artifacts stay on disk)."""
    slim = dict(payload)
    result = slim.get("result")
    if not isinstance(result, dict):
        return slim
    result = dict(result)
    stream_text = result.pop("cursor_stream_text", None)
    if stream_text is not None:
        result["cursor_stream_text_omitted"] = True
        result["cursor_stream_bytes"] = len(stream_text.encode("utf-8", errors="replace"))
    slim["result"] = result
    return slim

def _new_cursor_stream_state() -> dict[str, Any]:
    return {
        "events": [],
        "text_parts": [],
        "session_id": None,
        "model": None,
        "api_key_source": None,
        "tool_count": 0,
        "result_event": None,
    }


def _parse_cursor_stream_line(line: str) -> dict[str, Any] | None:
    if not line.strip():
        return None
    try:
        event = json.loads(line)
    except Exception:
        return None
    return event if isinstance(event, dict) else None


def _ingest_cursor_stream_event(state: dict[str, Any], event: dict[str, Any]) -> None:
    state["events"].append(event)
    if event.get("type") == "system" and event.get("subtype") == "init":
        state["session_id"] = event.get("session_id") or state["session_id"]
        state["model"] = event.get("model") or state["model"]
        state["api_key_source"] = event.get("apiKeySource") or state["api_key_source"]
    if event.get("type") == "tool_call" and event.get("subtype") == "started":
        state["tool_count"] += 1
    message = event.get("message")
    if event.get("type") == "assistant" and isinstance(message, dict):
        for part in message.get("content", []):
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                state["text_parts"].append(part["text"])
    if event.get("type") == "result":
        state["result_event"] = event


def _finalize_cursor_stream_state(state: dict[str, Any]) -> dict[str, Any]:
    result_event = state.get("result_event")
    result_text = ""
    if isinstance(result_event, dict) and isinstance(result_event.get("result"), str):
        result_text = result_event["result"].strip()
    elif state["text_parts"]:
        result_text = "".join(state["text_parts"]).strip()
    events = state["events"]
    return {
        "schema_version": "scillm.exec.cursor_response.v1",
        "session_id": state["session_id"],
        "model": state["model"],
        "api_key_source": state["api_key_source"],
        "tool_call_count": state["tool_count"],
        "text": result_text,
        "result_event": result_event,
        "events": [
            {
                "type": event.get("type"),
                "subtype": event.get("subtype"),
                "timestamp_ms": event.get("timestamp_ms"),
            }
            for event in events[-20:]
        ],
    }


def _cursor_stream_event_is_liveness(event: dict[str, Any]) -> bool:
    return event.get("type") in {"system", "assistant", "thinking", "tool_call", "result"}


def _cursor_stream_terminal_status(event: dict[str, Any]) -> str | None:
    if event.get("type") != "result":
        return None
    if event.get("is_error") is True or event.get("subtype") == "error":
        return "error"
    if event.get("subtype") == "success":
        return "success"
    return None


def _parse_cursor_stream_json(text: str) -> dict[str, Any]:
    state = _new_cursor_stream_state()
    for line in text.splitlines():
        event = _parse_cursor_stream_line(line)
        if event is None:
            continue
        _ingest_cursor_stream_event(state, event)
    return _finalize_cursor_stream_state(state)



def _read_process_stderr(process_result: dict[str, Any]) -> str:
    stderr_path = Path(str(process_result.get("stderr_path") or ""))
    if stderr_path.is_file():
        return stderr_path.read_text(encoding="utf-8", errors="replace")
    nested = process_result.get("result")
    if isinstance(nested, dict):
        return str(nested.get("stderr_tail") or "")
    return ""


def _cursor_provider_error(stderr_text: str, extracted: dict[str, Any]) -> str | None:
    haystack = "\n".join(
        [
            stderr_text,
            str(extracted.get("text") or ""),
            json.dumps(extracted.get("result_event") or {}, ensure_ascii=False),
        ]
    ).strip()
    if not haystack:
        return None
    lowered = haystack.lower()
    if "out of usage" in lowered or "usage limit" in lowered:
        for line in haystack.splitlines():
            if "out of usage" in line.lower() or "usage limit" in line.lower():
                return line.strip()
        return "Cursor provider quota exhausted"
    if extracted.get("agent_exit_code") not in (None, 0):
        tail = stderr_text.strip().splitlines()
        if tail:
            return tail[-1][:500]
    return None



def _cursor_exec_terminal_failure(
    extracted: dict[str, Any],
    write_audit: dict[str, Any] | None,
    *,
    recovered_from_stream: bool = False,
    stream_completed: bool = False,
) -> str | None:
    result_event = extracted.get("result_event")
    if isinstance(result_event, dict):
        if result_event.get("is_error") is True or result_event.get("subtype") == "error":
            return "cursor_error"
        if (
            stream_completed
            or recovered_from_stream
            or (result_event.get("subtype") == "success" and result_event.get("is_error") is not True)
        ) and str(extracted.get("text") or "").strip():
            return None
    exit_code = extracted.get("agent_exit_code")
    if exit_code not in (None, 0) and not (stream_completed or recovered_from_stream):
        return "cursor_process_error"
    text = str(extracted.get("text") or "").strip()
    if write_audit is not None and not write_audit.get("changed_paths") and not text:
        return "empty_output_no_write"
    return None


def _path_allowed(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        normalized = pattern.rstrip("/")
        if pattern.endswith("/") and (path == normalized or path.startswith(f"{normalized}/")):
            return True
        if path == normalized or path.startswith(f"{normalized}/"):
            return True
        if fnmatch.fnmatch(path, pattern):
            return True
    return False


def _parse_opencode_json_events(text: str) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    text_parts: list[str] = []
    session_id: str | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)
        session_id = session_id or event.get("sessionID")
        part = event.get("part")
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            text_parts.append(part["text"])
    content = "".join(text_parts).strip()
    return {
        "schema_version": "scillm.exec.opencode_response.v1",
        "session_id": session_id,
        "text": content,
        "events": [
            {
                "type": event.get("type"),
                "timestamp": event.get("timestamp"),
                "part_type": event.get("part", {}).get("type") if isinstance(event.get("part"), dict) else None,
            }
            for event in events
        ],
    }


def _parse_pi_json_events(text: str) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    text_parts: list[str] = []
    session_id: str | None = None
    error: str | None = None
    final_message: dict[str, Any] | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)
        if event.get("type") == "session":
            session_id = session_id or event.get("id")
        message = event.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            final_message = message
            if message.get("errorMessage"):
                error = str(message.get("errorMessage"))
            if message.get("stopReason") == "error" and error is None:
                error = "pi assistant stopReason=error"
    if final_message:
        for part in final_message.get("content", []):
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
    return {
        "schema_version": "scillm.exec.pi_response.v1",
        "session_id": session_id,
        "text": "".join(text_parts).strip(),
        "error": error,
        "events": [
            {
                "type": event.get("type"),
                "timestamp": event.get("timestamp"),
                "message_role": event.get("message", {}).get("role") if isinstance(event.get("message"), dict) else None,
            }
            for event in events
        ],
        "usage": final_message.get("usage") if final_message else None,
        "stop_reason": final_message.get("stopReason") if final_message else None,
    }




def _resolve_kimi_exec_profile(model: str | None) -> dict[str, str]:
    profile_name = model or "kimi-k2.6"
    canonical = _KIMI_EXEC_PROFILE_ALIASES.get(profile_name, profile_name)
    spec = _KIMI_EXEC_PROFILES.get(canonical)
    if spec is None:
        aliases = ", ".join(f"{alias!r}->{target!r}" for alias, target in sorted(_KIMI_EXEC_PROFILE_ALIASES.items()))
        raise ProxyError(
            400,
            (
                "unsupported kimi_exec model profile "
                f"{profile_name!r}; use one of {sorted(_KIMI_EXEC_PROFILES)}"
                + (f" (aliases: {aliases})" if aliases else "")
                + ". kimi_exec profiles are not scillm chat routes; use "
                "POST /v1/chat/completions with opencode-go/kimi-* for one-shot HTTP chat."
            ),
            "invalid_request_error",
        )
    model_id = os.environ.get(spec["model_env"], spec["default_model"])
    return {"profile": canonical, "model": model_id}


def _kimi_binary() -> str:
    return os.environ.get("SCILLM_KIMI_BINARY", "kimi")


def _kimi_exec_env(node: ExecNode) -> dict[str, str]:
    env = dict(node.env)
    api_key = os.environ.get("KIMI_API_KEY", "").strip()
    if api_key:
        env.setdefault("KIMI_API_KEY", api_key)
    return env


def _kimi_model_for_node(node: ExecNode, profile: dict[str, str]) -> str:
    override = node.metadata.get("kimi_model")
    if isinstance(override, str) and override.strip():
        return override.strip()
    return profile["model"]


def _kimi_exec_output_mode(node: ExecNode) -> str:
    raw = node.metadata.get("kimi_output_mode") or os.environ.get("SCILLM_KIMI_EXEC_OUTPUT_MODE", "agent")
    mode = str(raw).strip().lower()
    if mode not in {"agent", "print"}:
        raise ProxyError(
            400,
            "kimi_exec output mode must be 'agent' or 'print' via metadata.kimi_output_mode or SCILLM_KIMI_EXEC_OUTPUT_MODE",
            "invalid_request_error",
        )
    return mode


def _build_kimi_exec_command(
    *,
    node: ExecNode,
    profile: dict[str, str],
    cwd: Path,
    prompt: str,
) -> list[str]:
    mode = _kimi_exec_output_mode(node)
    command = [
        _kimi_binary(),
        "--work-dir",
        str(cwd),
        "--model",
        _kimi_model_for_node(node, profile),
    ]
    if mode == "print":
        # Clean stdout for deterministic gates on kimi-cli builds where -p lacks stream-json.
        command.extend(
            [
                "--print",
                "--final-message-only",
                "--output-format",
                "stream-json",
                "-p",
                prompt,
            ]
        )
    else:
        # Full-agent headless lane: kimi -p uses auto permission policy with all tools.
        command.extend(["-p", prompt])
    return command


_KIMI_RESUME_SESSION_RE = re.compile(r"^To resume this session:\s*kimi\s+-r\s+\S+\s*$", re.MULTILINE)
_KIMI_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_kimi_ansi(text: str) -> str:
    return _KIMI_ANSI_RE.sub("", text)


def _parse_kimi_stream_json_events(stdout: str) -> dict[str, Any]:
    texts: list[str] = []
    events: list[dict[str, Any]] = []
    error: str | None = None
    for line in _strip_kimi_ansi(stdout).splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        events.append(obj)
        role = obj.get("role")
        content = obj.get("content")
        if role == "assistant":
            if isinstance(content, str) and content.strip():
                texts.append(content.strip())
            elif isinstance(content, list):
                parts = [
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
                ]
                joined = "".join(parts).strip()
                if joined:
                    texts.append(joined)
        elif isinstance(content, str) and content.strip():
            texts.append(content.strip())
        if isinstance(obj.get("error"), str) and obj["error"].strip():
            error = obj["error"].strip()
    return {"text": texts[-1] if texts else "", "error": error, "events": events}


def _parse_kimi_agent_transcript(stdout: str) -> dict[str, Any]:
    clean = _strip_kimi_ansi(stdout)
    bullets: list[str] = []
    for line in clean.splitlines():
        if line.startswith("• "):
            bullets.append(line[2:].strip())
        elif line.startswith("  ") and bullets:
            bullets[-1] = f"{bullets[-1]} {line.strip()}".strip()
    text = bullets[-1].strip() if bullets else clean.strip()
    return {"text": text, "error": None, "events": [], "bullets": bullets}


def _parse_kimi_exec_output(stdout: str, *, mode: str) -> dict[str, Any]:
    session_hint: str | None = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("To resume this session:"):
            session_hint = stripped
            break
    body = _KIMI_RESUME_SESSION_RE.sub("", stdout).strip()
    if mode == "print":
        extracted = _parse_kimi_stream_json_events(body)
    else:
        extracted = _parse_kimi_agent_transcript(body)
        if not extracted.get("text"):
            extracted = _parse_kimi_stream_json_events(body)
    extracted["session_hint"] = session_hint
    extracted["output_mode"] = mode
    return extracted


def _kimi_exec_terminal_failure(extracted: dict[str, Any], write_audit: dict[str, Any] | None) -> str | None:
    text = str(extracted.get("text") or "").strip()
    if extracted.get("error"):
        return "kimi_error"
    if write_audit is not None and not write_audit.get("changed_paths") and not text:
        return "empty_output_no_write"
    return None

def _pi_exec_terminal_failure(extracted: dict[str, Any], write_audit: dict[str, Any] | None) -> str | None:
    text = str(extracted.get("text") or "").strip()
    if extracted.get("error"):
        return "pi_error"
    if extracted.get("stop_reason") == "length" and not text:
        return "pi_length_no_output"
    if write_audit is not None and not write_audit.get("changed_paths") and not text:
        return "empty_output_no_write"
    return None


def _reject_dangerous_command(command: str | list[str]) -> None:
    text = command if isinstance(command, str) else " ".join(shlex.quote(part) for part in command)
    if _DANGEROUS_COMMAND_RE.search(text):
        raise ProxyError(
            400,
            "dangerous local_command rejected; set sandbox='danger-full-access' only for explicitly reviewed commands",
            "invalid_request_error",
        )


def _parse_jsonish(text: Any) -> Any | None:
    if text is None:
        return None
    if isinstance(text, (dict, list)):
        return _unwrap_jsonish(text)
    if not isinstance(text, str):
        return None

    stripped = text.strip()
    if stripped:
        try:
            return _unwrap_jsonish(json.loads(stripped))
        except Exception:
            pass
        repaired = _repair_jsonish_string(stripped)
        if repaired is not None:
            return _unwrap_jsonish(repaired)

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return _unwrap_jsonish(json.loads(text[start : end + 1]))
        except Exception:
            pass

    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, (dict, list)):
            return _unwrap_jsonish(parsed)

    return None


def _terminal_exit_marker(transcript: str, marker: str = "__SCILLM_EXIT") -> int | None:
    matches = re.findall(rf"{re.escape(marker)}:(\d+)", transcript)
    if not matches:
        return None
    return int(matches[-1])


def _terminal_stdout_from_transcript(transcript: str, marker: str = "__SCILLM_EXIT") -> str:
    """Best-effort stdout extraction from a tmux pane transcript.

    The full transcript remains the evidence source. This helper only derives a
    convenient result payload for local_command compatibility.
    """

    normalized = transcript.replace("\r\n", "\n").replace("\r", "\n")
    marker_match = re.search(rf"\n?{re.escape(marker)}:\d+", normalized)
    if marker_match:
        normalized = normalized[: marker_match.start()]
    lines = normalized.splitlines()
    if lines and ("__SCILLM_EXIT" in lines[0] or "printf" in lines[0]):
        lines = lines[1:]
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    return ("\n".join(lines) + "\n") if lines else ""


def _repair_jsonish_string(text: str) -> Any | None:
    try:
        from services.memory.graph_memory.extras.json_utils import clean_json_string
    except Exception:
        return None

    try:
        repaired = clean_json_string(text, return_dict=True)
    except Exception:
        return None
    if isinstance(repaired, (dict, list)):
        return repaired
    return None


def _unwrap_jsonish(parsed: Any) -> Any:
    if isinstance(parsed, dict):
        for key in ("result", "final", "content", "response"):
            if set(parsed) == {key} and parsed[key]:
                value = parsed[key]
                if isinstance(value, str):
                    nested = _parse_jsonish(value)
                    return nested if nested is not None else value
                return value
    return parsed


def _artifact_root() -> Path:
    root = Path(os.environ.get("SCILLM_EXEC_OUTPUT_DIR", "/tmp/scillm-exec")).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._:-]+", "-", value.strip())
    return safe[:160] or f"exec-{uuid.uuid4().hex[:12]}"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
