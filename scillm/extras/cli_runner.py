from __future__ import annotations

import asyncio
import os
import shlex
from typing import List, Tuple


def _resolve_cmd() -> List[str]:
    """Resolve codex CLI command from env or common fallbacks.

    Returns a argv list suitable for asyncio.create_subprocess_exec.
    """
    env = os.getenv("CODEX_CMD")
    if env:
        return shlex.split(env)
    home = os.path.expanduser("~")
    fallbacks = [
        f"{home}/workspace/experiments/codex/dist/bin/codex exec --json --skip-git-repo-check",
        "cxplus exec --json",
        "codex exec --json",
    ]
    for spec in fallbacks:
        parts = shlex.split(spec)
        if parts:
            return parts
    # Last resort: let OS resolve it
    return ["codex", "exec", "--json"]


def _messages_to_prompt(messages: List[dict]) -> str:
    parts: List[str] = []
    for m in messages:
        role = (m.get("role") or "").strip().lower()
        content = m.get("content") or ""
        if role in ("system", "user"):
            parts.append(str(content))
        elif role == "assistant":
            parts.append(f"Assistant: {content}")
        else:
            parts.append(str(content))
    return "\n".join(parts).strip()


async def acli_chat(model: str, messages: List[dict], *, timeout: float = 90.0) -> Tuple[int, str, str]:
    """Run codex CLI with a prompt built from messages. Returns (exit, stdout, stderr)."""
    argv = _resolve_cmd()
    prompt_flag = os.getenv("CODEX_PROMPT_FLAG", "--prompt")
    cmd = argv + ["--model", model, prompt_flag, _messages_to_prompt(messages)]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        done = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return 124, "", "timeout"
    out = (done[0] or b"").decode("utf-8", "ignore")
    err = (done[1] or b"").decode("utf-8", "ignore")
    return proc.returncode or 0, out, err


def cli_chat(model: str, messages: List[dict], *, timeout: float = 90.0) -> Tuple[int, str, str]:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        raise RuntimeError("cli_chat() called inside running event loop; use acli_chat().")
    return asyncio.run(acli_chat(model, messages, timeout=timeout))


__all__ = ["acli_chat", "cli_chat"]

