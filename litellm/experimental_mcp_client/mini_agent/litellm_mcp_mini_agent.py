from __future__ import annotations

import asyncio
import sys
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .call_wrapper import arouter_call


class MCPInvoker:
    """Minimal contract to bridge MCP-style tools to OpenAI function tools.

    Required methods:
    - list_openai_tools() -> List[OpenAI function tool dicts]
    - call_openai_tool(openai_tool) -> str (tool text result)
    """

    async def list_openai_tools(self) -> List[Dict[str, Any]]:  # pragma: no cover - interface
        raise NotImplementedError

    async def call_openai_tool(self, openai_tool: Dict[str, Any]) -> str:  # pragma: no cover - interface
        raise NotImplementedError


class EchoMCP(MCPInvoker):
    """Built-in test tool: echo(text) -> text."""

    async def list_openai_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "Echo the provided text.",
                    "parameters": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                },
            }
        ]

    async def call_openai_tool(self, openai_tool: Dict[str, Any]) -> str:
        fn = openai_tool.get("function", {}) or {}
        name = fn.get("name")
        args_str = fn.get("arguments", "{}")
        try:
            args = json.loads(args_str)
        except Exception:
            args = {}
        if name == "echo":
            return str(args.get("text", ""))
        raise ValueError(f"tool_not_found:{name}")


@dataclass
class AgentConfig:
    model: str
    max_iterations: int = 8
    max_wallclock_seconds: int = 60
    max_tools_per_iter: int = 4
    stagnation_window: int = 3
    temperature: float = 0.2
    tool_allowlist: Optional[List[str]] = None
    tool_choice: str = "auto"  # "auto" or "required"
    tool_concurrency: int = 1  # >1 enables bounded parallel tool execution
    # Recovery + messaging strategy
    enable_repair: bool = True
    observation_strategy: str = "assistant_append"  # "assistant_append" | "none"
    max_history_messages: int = 50  # non-system messages to retain during pruning
    # Research on uncertainty
    research_on_unsure: bool = True
    max_research_hops: int = 2
    # Escalation policy
    max_failures_before_research: int = 2
    research_after_failures: bool = True
    # Optional: periodic system refresh/summarization
    summarize_every: int = 0  # 0 disables; when >0, insert/update a brief system summary every N iters


@dataclass
class IterLog:
    index: int
    plan_or_msg: str
    tool_invocations: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class AgentResult:
    final_answer: str
    stopped_reason: str  # "success" | "max_iterations" | "budget" | "stagnation" | "error" | "cancelled"
    iterations: List[IterLog]
    messages: List[Dict[str, Any]]


def _hash_norm(s: str) -> str:
    return hashlib.sha256(" ".join((s or "").split()).encode()).hexdigest()[:12]


def _get_tool_calls(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    tcs = message.get("tool_calls") or []
    return [tc for tc in tcs if (tc.get("type") == "function" and "function" in tc)]


_UNSURE_MARKERS = (
    "i'm not sure",
    "i am not sure",
    "not sure",
    "uncertain",
    "unknown",
    "can't find",
    "cannot find",
)


def _looks_unsure(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    if any(m in t for m in _UNSURE_MARKERS):
        return True
    # Heuristic: short answer ending with '?'
    return (len(t) < 120) and t.endswith("?")


def _truncate(text: str, n: int = 800) -> str:
    t = text or ""
    return t if len(t) <= n else (t[: n // 2] + "\n...\n" + t[- n // 2 :])


def _prune_history(messages: List[Dict[str, Any]], max_non_system: int) -> List[Dict[str, Any]]:
    if max_non_system <= 0:
        return messages
    systems = [m for m in messages if m.get("role") == "system"]
    non_systems = [m for m in messages if m.get("role") != "system"]
    if len(non_systems) <= max_non_system:
        return messages
    kept = non_systems[-max_non_system:]
    return systems + kept


def _prune_history_preserve_pair(
    messages: List[Dict[str, Any]],
    max_non_system: int,
    *,
    hard_char_budget: int = 12000,
) -> List[Dict[str, Any]]:
    """Prune like _prune_history, but guarantee the last assistant(tool_calls)
    and its subsequent tool messages survive. Also apply a simple char budget.

    This keeps behavior stable without adding new config knobs.
    """
    out: List[Dict[str, Any]]
    if max_non_system <= 0:
        out = messages[:]
    else:
        # Find the last assistant message with tool_calls
        anchor = None
        for idx in range(len(messages) - 1, -1, -1):
            m = messages[idx]
            if m.get("role") == "assistant" and _get_tool_calls(m):
                anchor = idx
                break
        systems = [m for m in messages if m.get("role") == "system"]
        non_systems = [m for m in messages if m.get("role") != "system"]
        if len(non_systems) <= max_non_system:
            out = messages[:]
        else:
            kept_ns = non_systems[-max_non_system:]
            if anchor is not None:
                kept_tail = messages[anchor:]
                # Merge while avoiding duplicates
                merged: List[Dict[str, Any]] = []
                seen = set()
                # systems first
                for m in systems:
                    merged.append(m)
                # then the kept non-systems excluding ones already in tail
                for m in kept_ns:
                    key = id(m)
                    if m in kept_tail or key in seen:
                        continue
                    merged.append(m)
                    seen.add(key)
                # finally ensure tail (assistant tool_call + tool replies) is present
                for m in kept_tail:
                    key = id(m)
                    if key in seen:
                        continue
                    merged.append(m)
                    seen.add(key)
                out = merged
            else:
                out = systems + kept_ns
    # Apply a rough character budget on content to avoid runaway context
    if hard_char_budget > 0:
        total = 0
        trimmed: List[Dict[str, Any]] = []
        for m in reversed(out):
            total += len(str(m.get("content", "")))
            trimmed.append(m)
            if total >= hard_char_budget:
                break
        out = list(reversed(trimmed))
    return out


def _make_observation_block(invocations: List[Dict[str, Any]]) -> Optional[str]:
    if not invocations:
        return None
    parts = []
    for inv in invocations:
        name = inv.get("name")
        rc = inv.get("rc")
        stdout_tail = inv.get("stdout_tail")
        stderr_tail = inv.get("stderr_tail")
        if inv.get("ok"):
            line = f"{name}: ok" + (f" (rc={rc})" if rc is not None else "")
            if stdout_tail:
                line += f"\nstdout:\n{_truncate(str(stdout_tail), 300)}"
            if stderr_tail:
                line += f"\nstderr:\n{_truncate(str(stderr_tail), 300)}"
            parts.append(line)
        else:
            err = inv.get("error") or "error"
            line = f"{name}: error" + (f" (rc={rc})" if rc is not None else "") + f"\n{_truncate(str(err), 400)}"
            if stdout_tail:
                line += f"\nstdout:\n{_truncate(str(stdout_tail), 300)}"
            if stderr_tail:
                line += f"\nstderr:\n{_truncate(str(stderr_tail), 300)}"
            parts.append(line)
    return "\n\n".join(parts)


def _append_observation_if_needed(
    convo: List[Dict[str, Any]],
    invocations: List[Dict[str, Any]],
    cfg: AgentConfig,
) -> None:
    if not cfg.enable_repair or cfg.observation_strategy != "assistant_append":
        return
    obs = _make_observation_block(invocations)
    if not obs:
        return
    content = (
        "Observation from last tool run(s):\n" + obs +
        "\n\nIf a command failed, propose a corrected next step (or choose a research tool) and proceed."
    )
    convo.append({"role": "assistant", "content": content})


async def arun_mcp_mini_agent(
    messages: List[Dict[str, Any]],
    mcp: MCPInvoker,
    cfg: AgentConfig,
    **llm_kwargs: Any,
) -> AgentResult:
    """Run a minimal in-process MCP mini-agent with deterministic guardrails."""

    start = time.time()
    iterations: List[IterLog] = []
    plan_hashes: List[str] = []

    tools = await mcp.list_openai_tools()
    if cfg.tool_allowlist:
        allowed = set(cfg.tool_allowlist)
        tools = [
            t
            for t in tools
            if t.get("type") == "function" and t.get("function", {}).get("name") in allowed
        ]

    if cfg.tool_choice == "required" and not tools:
        return AgentResult("(partial) no_tools_available", "error", [], list(messages))

    convo: List[Dict[str, Any]] = list(messages)

    research_hops = 0
    total_failures = 0
    try:
        for i in range(cfg.max_iterations):
            if time.time() - start > cfg.max_wallclock_seconds:
                return AgentResult("(partial) budget:wallclock", "budget", iterations, convo)

            resp = await arouter_call(
                model=cfg.model,
                messages=convo,
                tools=tools if tools else None,
                tool_choice=cfg.tool_choice,
                temperature=cfg.temperature,
                **llm_kwargs,
            )
            msg = resp["choices"][0]["message"]
            convo.append(msg)

            tool_calls = _get_tool_calls(msg)
            content = (msg.get("content") or "").strip()
            iterations.append(IterLog(index=i, plan_or_msg=content))

            if not tool_calls:
                # If answer looks unsure and research is enabled, steer the model to use research tools
                if cfg.research_on_unsure and research_hops < cfg.max_research_hops and _looks_unsure(content):
                    research_hops += 1
                    tool_names = [t.get("function", {}).get("name") for t in tools if t.get("type") == "function"]
                    hints = ", ".join(n for n in tool_names if n and ("research" in n or "search" in n))
                    directive = (
                        "You seem uncertain. Use available research tools"
                        + (f" ({hints})" if hints else "")
                        + " to gather evidence and proceed with citations."
                    )
                    convo.append({"role": "assistant", "content": directive})
                    continue
                return AgentResult(content, "success", iterations, convo)

            # Avoid false stagnation on tool-only turns; include tool names
            tc_names = ",".join([tc.get("function", {}).get("name", "") for tc in tool_calls]) if tool_calls else ""
            if content or tc_names:
                plan_hashes.append(_hash_norm(f"{content}|{tc_names}"))
            if len(plan_hashes) >= cfg.stagnation_window:
                recent = plan_hashes[-cfg.stagnation_window :]
                if len(set(recent)) == 1:
                    return AgentResult("(partial) stagnation", "stagnation", iterations, convo)

            calls = tool_calls[: cfg.max_tools_per_iter]
            this_iter_invocations: List[Dict[str, Any]] = []
            if cfg.tool_concurrency <= 1:
                for tc in calls:
                    fn = tc["function"]
                    name = fn.get("name")

                    if cfg.tool_allowlist and name not in cfg.tool_allowlist:
                        err = {"name": name, "error": "tool_not_allowed"}
                        iterations[-1].tool_invocations.append(err)
                        this_iter_invocations.append(err)
                        convo.append(
                            {"role": "tool", "tool_call_id": tc["id"], "content": "[tool_error] tool_not_allowed"}
                        )
                        continue

                    try:
                        result_text = await asyncio.wait_for(mcp.call_openai_tool(tc), timeout=30)
                        preview = None
                        is_err = False
                        rc = None
                        stdout_tail = None
                        stderr_tail = None
                        try:
                            data = json.loads(result_text)
                            if isinstance(data, dict):
                                stdout_tail = (data.get("stdout") or "")
                                stderr_tail = (data.get("stderr") or "")
                                preview = _truncate(str(stdout_tail or data.get("text") or ""), 300)
                                rc = data.get("rc")
                                is_err = (rc not in (None, 0)) or (data.get("ok") is False)
                        except Exception:
                            pass
                        if is_err:
                            err = {"name": name, "error": preview or "rc!=0", "rc": rc, "stdout_tail": stdout_tail, "stderr_tail": stderr_tail}
                            iterations[-1].tool_invocations.append(err)
                            this_iter_invocations.append(err)
                        else:
                            ok = {"name": name, "ok": True, "rc": rc, **({"preview": preview} if preview else {}), **({"stdout_tail": stdout_tail} if stdout_tail else {}), **({"stderr_tail": stderr_tail} if stderr_tail else {})}
                            iterations[-1].tool_invocations.append(ok)
                            this_iter_invocations.append(ok)
                        convo.append({"role": "tool", "tool_call_id": tc["id"], "content": result_text})
                    except Exception as e:
                        err = {"name": name, "error": str(e)[:200]}
                        iterations[-1].tool_invocations.append(err)
                        this_iter_invocations.append(err)
                        convo.append(
                            {"role": "tool", "tool_call_id": tc["id"], "content": f"[tool_error] {e}"}
                        )
            else:
                sem = asyncio.Semaphore(max(1, int(cfg.tool_concurrency)))

                async def run_one(j: int, tc: dict):
                    fn = tc["function"]
                    name = fn.get("name")
                    if cfg.tool_allowlist and name not in (cfg.tool_allowlist or []):
                        return (j, name, None, "tool_not_allowed")
                    try:
                        async with sem:
                            out = await asyncio.wait_for(mcp.call_openai_tool(tc), timeout=30)
                        return (j, name, out, None)
                    except Exception as e:  # pragma: no cover - rarely hit in smokes
                        return (j, name, None, str(e)[:200])

                results = await asyncio.gather(
                    *(run_one(j, tc) for j, tc in enumerate(calls)), return_exceptions=False
                )
                # Deterministic ordering by original index j
                for (j, name, out, err), tc in zip(sorted(results, key=lambda x: x[0]), calls):
                    if err:
                        eobj = {"name": name, "error": err}
                        iterations[-1].tool_invocations.append(eobj)
                        this_iter_invocations.append(eobj)
                        convo.append({"role": "tool", "tool_call_id": tc["id"], "content": f"[tool_error] {err}"})
                    else:
                        # Try to decode JSON to capture rc/stdout/stderr
                        rc = None; stdout_tail=None; stderr_tail=None
                        try:
                            data = json.loads(out or "")
                            if isinstance(data, dict):
                                rc = data.get("rc"); stdout_tail=data.get("stdout"); stderr_tail=data.get("stderr")
                        except Exception:
                            pass
                        pobj = {"name": name, "ok": True, **({"preview": _truncate(out or "", 300)} if out else {}), **({"rc": rc} if rc is not None else {}), **({"stdout_tail": stdout_tail} if stdout_tail else {}), **({"stderr_tail": stderr_tail} if stderr_tail else {})}
                        iterations[-1].tool_invocations.append(pobj)
                        this_iter_invocations.append(pobj)
                        convo.append({"role": "tool", "tool_call_id": tc["id"], "content": out or ""})

            # Escalation counter
            if any((not inv.get("ok") and inv.get("error")) for inv in this_iter_invocations):
                total_failures += 1

            # Append observation and prune history if configured
            _append_observation_if_needed(convo, this_iter_invocations, cfg)
            if cfg.max_history_messages:
                convo[:] = _prune_history_preserve_pair(convo, cfg.max_history_messages)

            # Periodic system summary (cheap heuristic to avoid extra LLM calls)
            if cfg.summarize_every and (i + 1) % cfg.summarize_every == 0:
                recent = []
                for m in reversed(convo):
                    if m.get("role") in ("user", "assistant"):
                        txt = str(m.get("content") or "").strip()
                        if txt:
                            recent.append(txt)
                    if len(recent) >= 6:
                        break
                summary = _truncate(" | ".join(reversed(recent)), 600)
                found_idx = None
                for idx, m in enumerate(convo):
                    if m.get("role") == "system" and str(m.get("content") or "").startswith("[Context Summary]"):
                        found_idx = idx; break
                system_summary = {"role": "system", "content": f"[Context Summary] {summary}"}
                if found_idx is None:
                    convo.insert(1, system_summary)
                else:
                    convo[found_idx] = system_summary

            # Failure → research escalation
            if (
                cfg.research_after_failures
                and total_failures >= max(1, cfg.max_failures_before_research)
                and research_hops < cfg.max_research_hops
            ):
                research_hops += 1
                tool_names = [t.get("function", {}).get("name") for t in tools if t.get("type") == "function"]
                hints = ", ".join(n for n in tool_names if n and ("research" in n or "search" in n))
                convo.append(
                    {
                        "role": "assistant",
                        "content": (
                            "Repeated failures detected. Switch to research tools"
                            + (f" ({hints})" if hints else "")
                            + " to unblock with evidence, then continue."
                        ),
                    }
                )
                total_failures = 0

    except asyncio.CancelledError:  # pragma: no cover
        return AgentResult("(partial) cancelled", "cancelled", iterations, convo)
    except Exception as e:  # pragma: no cover - defensive
        iterations.append(IterLog(index=len(iterations), plan_or_msg=f"[error] {e}"))
        return AgentResult("(partial) error", "error", iterations, convo)

    return AgentResult("(partial) max_iterations", "max_iterations", iterations, convo)


def run_mcp_mini_agent(
    messages: List[Dict[str, Any]],
    mcp: MCPInvoker,
    cfg: AgentConfig,
    **llm_kwargs: Any,
) -> AgentResult:
    """Sync wrapper for environments without asyncio entrypoints."""

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():  # pragma: no cover - not used in smokes
        # Run in a task and wait
        fut = asyncio.run_coroutine_threadsafe(
            arun_mcp_mini_agent(messages, mcp=mcp, cfg=cfg, **llm_kwargs), loop
        )
        return fut.result()
    else:
        return asyncio.run(arun_mcp_mini_agent(messages, mcp=mcp, cfg=cfg, **llm_kwargs))


class LocalMCPInvoker(MCPInvoker):
    """Local tools: exec_python, exec_shell, research stubs.

    Safety for exec_shell via allowlist prefixes.
    """

    def __init__(self, *, shell_allow_prefixes: Optional[List[str]] = None) -> None:
        self.shell_allow_prefixes = shell_allow_prefixes or ["echo", "python", "pip"]

    async def list_openai_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "exec_python",
                    "description": "Execute short Python code in a subprocess; returns JSON with rc/stdout/stderr.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string"},
                            "timeout_s": {"type": "number"},
                        },
                        "required": ["code"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "exec_shell",
                    "description": "Run a short shell command with a strict allowlist; returns JSON with rc/stdout/stderr.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "cmd": {"type": "string"},
                            "timeout_s": {"type": "number"},
                        },
                        "required": ["cmd"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "research_echo",
                    "description": "Stub research tool: echoes the query as evidence JSON (for tests).",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    async def call_openai_tool(self, openai_tool: Dict[str, Any]) -> str:
        fn = openai_tool.get("function", {}) or {}
        name = fn.get("name")
        args_str = fn.get("arguments", "{}")
        try:
            args = json.loads(args_str) if isinstance(args_str, str) else (args_str or {})
        except Exception:
            args = {}
        if name == "exec_python":
            code = str(args.get("code", ""))
            timeout_s = float(args.get("timeout_s", 15))
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    code,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
                    rc = proc.returncode
                    out = {
                        "ok": rc == 0,
                        "rc": rc,
                        "stdout": stdout_bytes.decode("utf-8", "replace"),
                        "stderr": stderr_bytes.decode("utf-8", "replace"),
                    }
                    return json.dumps(out, ensure_ascii=False)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        await proc.wait()
                    except Exception:
                        pass
                    return json.dumps({"ok": False, "rc": None, "stderr": "timeout", "stdout": ""})
            except Exception as e:
                return json.dumps({"ok": False, "rc": None, "stderr": str(e), "stdout": ""})
        if name == "exec_shell":
            import shlex

            cmd = str(args.get("cmd", "")).strip()
            timeout_s = float(args.get("timeout_s", 15))
            if not cmd:
                return json.dumps({"ok": False, "rc": None, "stderr": "empty command", "stdout": ""})
            allowed = any(cmd.startswith(p + " ") or cmd == p for p in (self.shell_allow_prefixes or []))
            if not allowed:
                return json.dumps({"ok": False, "rc": None, "stderr": "command not allowed", "stdout": ""})
            try:
                proc = await asyncio.create_subprocess_exec(
                    *shlex.split(cmd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
                    rc = proc.returncode
                    return json.dumps(
                        {
                            "ok": rc == 0,
                            "rc": rc,
                            "stdout": stdout_bytes.decode("utf-8", "replace"),
                            "stderr": stderr_bytes.decode("utf-8", "replace"),
                        },
                        ensure_ascii=False,
                    )
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        await proc.wait()
                    except Exception:
                        pass
                    return json.dumps({"ok": False, "rc": None, "stderr": "timeout", "stdout": ""})
            except Exception as e:
                return json.dumps({"ok": False, "rc": None, "stderr": str(e), "stdout": ""})

        if name == "research_echo":
            return json.dumps({"ok": True, "text": f"EVIDENCE: {args.get('query','')}"}, ensure_ascii=False)
        raise ValueError(f"tool_not_found:{name}")
