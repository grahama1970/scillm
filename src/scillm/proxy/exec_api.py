"""scillm exec worker runtime endpoints.

This module adds a small, artifacted execution layer on top of the existing
scillm model proxy.  It is intentionally a runtime substrate, not a project
planner: callers such as plan-iterate or ask own goals, contracts, review
verdicts, and iteration policy.  scillm exec owns bounded worker execution,
status, events, and result artifacts.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Literal

import httpx
import jsonschema
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from scillm.proxy.errors import ProxyError

AuthCheck = Callable[[Request], str | None]

RunnerKind = Literal[
    "scillm_call",
    "scillm_batch",
    "codex_exec",
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
    cwd: str | None = None
    sandbox: SandboxMode = "read-only"
    worktree: WorktreeSpec = Field(default_factory=WorktreeSpec)
    env: dict[str, str] = Field(default_factory=dict)

    messages: list[dict[str, Any]] | None = None
    prompt: str | None = None
    prompt_path: str | None = None
    command: str | list[str] | None = None

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

    metadata: dict[str, Any] = Field(default_factory=dict)
    personas: dict[str, dict[str, Any]] = Field(default_factory=dict)
    nodes: list[ExecNode]


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
        self._cancel_requested = False
        self.run_dir.mkdir(parents=True, exist_ok=True)

    async def cancel(self) -> int:
        self._cancel_requested = True
        killed = 0
        for proc in list(self._processes):
            if proc.returncode is None:
                proc.terminate()
                killed += 1
        await self.emit("cancel_requested", killed_processes=killed)
        await self.write_status(state="cancel_requested")
        return killed

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
        (self.run_dir / "graph.request.json").write_text(graph.model_dump_json(indent=2), encoding="utf-8")

        nodes = [_apply_graph_defaults(graph, node) for node in graph.nodes]
        _validate_graph(nodes)

        pending = {node.id: node for node in nodes}
        running: dict[str, asyncio.Task[dict[str, Any]]] = {}
        completed: set[str] = set()
        failed: set[str] = set()
        semaphore = asyncio.Semaphore(max(1, int(graph.max_concurrency)))

        async def launch(node: ExecNode) -> dict[str, Any]:
            async with semaphore:
                return await self._run_node(node)

        while pending or running:
            if self._cancel_requested:
                for task in running.values():
                    task.cancel()
                break

            ready = [node for node in pending.values() if all(dep in completed for dep in node.depends_on)]
            blocked_by_failed = [
                node
                for node in pending.values()
                if any(dep in failed for dep in node.depends_on) and not node.allow_failure
            ]

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

            for node in ready:
                pending.pop(node.id)
                running[node.id] = asyncio.create_task(launch(node))
                await self.emit("node_scheduled", node_id=node.id, depends_on=node.depends_on)

            if not running:
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
            "node_results": self.node_results,
            "artifacts": self._artifact_summary(),
        }
        (self.run_dir / "execution_result.json").write_text(json.dumps(final, indent=2), encoding="utf-8")
        await self.emit("graph_finished", status=status, completed=len(completed), failed=len(failed))
        await self.write_status(state=status)
        return final

    async def _run_node(self, node: ExecNode) -> dict[str, Any]:
        node_dir = self.run_dir / "nodes" / _safe_id(node.id)
        node_dir.mkdir(parents=True, exist_ok=True)
        (node_dir / "node.request.json").write_text(node.model_dump_json(indent=2), encoding="utf-8")

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

        prompt = _assemble_prompt(node)
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
        except Exception as exc:
            return {
                "node_id": node.id,
                "ok": False,
                "failure_type": "process_error",
                "error": str(exc),
                "elapsed_s": round(time.monotonic() - started, 3),
            }
        finally:
            if cleanup:
                await _remove_worktree(node, cwd)

    async def _prepare_cwd(self, node: ExecNode, attempt_dir: Path) -> tuple[Path, bool]:
        cwd = Path(node.cwd or os.getcwd()).expanduser().resolve()
        if not node.worktree.enabled:
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
        return await self._run_process(
            command=node.command,
            cwd=cwd,
            attempt_dir=attempt_dir,
            timeout_s=node.timeout_s,
            idle_timeout_s=node.idle_timeout_s,
            shell=isinstance(node.command, str),
        )

    async def _run_scillm_call(self, node: ExecNode, prompt: str, attempt_dir: Path) -> dict[str, Any]:
        url = os.environ.get("SCILLM_INTERNAL_CHAT_URL", "http://127.0.0.1:4001/v1/chat/completions")
        messages = node.messages or [{"role": "user", "content": prompt}]
        schema = _load_schema(node)
        payload: dict[str, Any] = {
            "model": node.model or "text",
            "messages": messages,
            "scillm_metadata": {
                **node.metadata,
                "run_id": self.run_id,
                "node_id": node.id,
                "runner": node.type,
            },
        }
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
        schema_path = _write_schema_if_needed(node, attempt_dir)
        result_path = attempt_dir / "final.json"
        template = os.environ.get(
            "SCILLM_CODEX_EXEC_TEMPLATE",
            "codex exec --json --sandbox {sandbox} {schema_args} -",
        )
        schema_args = f"--output-schema {shlex.quote(str(schema_path))} -o {shlex.quote(str(result_path))}" if schema_path else ""
        command = template.format(sandbox=shlex.quote(node.sandbox), schema_args=schema_args, result_path=shlex.quote(str(result_path)))
        return await self._run_process(
            command=command,
            cwd=cwd,
            attempt_dir=attempt_dir,
            timeout_s=node.timeout_s,
            idle_timeout_s=node.idle_timeout_s,
            stdin=prompt,
            shell=True,
            final_json_path=result_path if schema_path else None,
        )

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
    ) -> dict[str, Any]:
        stdout_path = attempt_dir / "stdout.log"
        stderr_path = attempt_dir / "stderr.log"
        proc_events_path = attempt_dir / "events.jsonl"
        env = {**os.environ, "SCILLM_EXEC_RUN_ID": self.run_id}

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
                line = await stream.readline()
                if not line:
                    break
                last_output_at = time.monotonic()
                text = line.decode(errors="replace")
                sink.append(text)
                event = {"type": label, "text": text.rstrip("\n"), "ts": _now()}
                with proc_events_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(event, sort_keys=True) + "\n")
                await self.emit(label, text=text.rstrip("\n"))

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
            "artifacts": self._artifact_summary(),
        }
        tmp = self.status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, indent=2), encoding="utf-8")
        tmp.replace(self.status_path)

    def _artifact_summary(self) -> dict[str, str]:
        return {
            "run_dir": str(self.run_dir),
            "events_jsonl": str(self.events_path),
            "status_json": str(self.status_path),
            "execution_result_json": str(self.run_dir / "execution_result.json"),
        }


def create_exec_router(check_auth: AuthCheck) -> APIRouter:
    router = APIRouter()

    async def auth(request: Request) -> str:
        err = check_auth(request)
        if err:
            raise ProxyError(401, err, "authentication_error")
        return request.headers.get("authorization", "")

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
            async with _registered_run(run):
                return StreamingResponse(
                    run.stream_single(node),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
        async with _registered_run(run):
            return JSONResponse(await run.run_single(node))

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
            async with _registered_run(run):
                return StreamingResponse(
                    run.stream_graph(graph),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
        async with _registered_run(run):
            return JSONResponse(await run.run_graph(graph))

    @router.post("/exec/graph")
    async def exec_graph(request: Request):
        auth_header = await auth(request)
        body = await request.json()
        spec = ExecGraphRequest.model_validate(body)
        run = ExecRun(
            run_id=spec.graph_id,
            artifact_root=_artifact_root(),
            auth_header=auth_header,
            caller_skill=request.headers.get("x-caller-skill", "scillm-exec-graph"),
        )
        if spec.stream:
            async with _registered_run(run):
                return StreamingResponse(
                    run.stream_graph(spec),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
        async with _registered_run(run):
            return JSONResponse(await run.run_graph(spec))

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
        events = [json.loads(line) for line in lines[-tail:] if line.strip()]
        return JSONResponse({"run_id": _safe_id(run_id), "events": events})

    @router.post("/exec/{run_id}/cancel")
    async def exec_cancel(request: Request, run_id: str):
        await auth(request)
        safe_run_id = _safe_id(run_id)
        async with _ACTIVE_LOCK:
            run = _ACTIVE_RUNS.get(safe_run_id)
        if run is None:
            raise ProxyError(404, f"active exec run not found: {safe_run_id}", "not_found")
        killed = await run.cancel()
        return JSONResponse({"run_id": safe_run_id, "cancel_requested": True, "killed_processes": killed})

    return router


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


def _assemble_prompt(node: ExecNode) -> str:
    if node.prompt_path:
        task_prompt = Path(node.prompt_path).expanduser().read_text(encoding="utf-8")
    else:
        task_prompt = node.prompt or ""

    messages_text = ""
    if node.messages:
        messages_text = "\n\nMESSAGES:\n" + json.dumps(node.messages, indent=2)

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
{task_prompt}
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
        return text
    if not isinstance(text, str):
        return None

    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            for key in ("result", "final", "content", "response"):
                if key in parsed and parsed[key]:
                    return parsed[key]
        return parsed

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            return None
    return None


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
