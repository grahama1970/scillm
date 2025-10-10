"""Codex CLI sidecar exposing an OpenAI-like HTTP endpoint.

This module mirrors the reference implementation from the Codex hybrid
proposal.  It keeps the Codex CLI as the only component that talks to the
remote service, while exposing a stable local HTTP API for LiteLLM.

The server is intentionally lightweight and is started on-demand by the
Codex agent provider if no ``CODEX_AGENT_API_BASE`` is configured.  The
default host/port can be overridden via ``CODEX_SIDECAR_HOST`` and
``CODEX_SIDECAR_PORT`` environment variables.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import AsyncIterator, Dict, List, Optional, Tuple

import shutil

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class _Settings:
    codex_cmd: List[str]
    input_mode: str
    prompt_flag: str
    max_concurrency: int
    abs_timeout_s: float
    idle_timeout_s: float
    retries: int
    log_level: str

    @staticmethod
    def _resolve_codex_cmd() -> List[str]:
        """Resolve the Codex CLI command, preferring CODEX_CMD when set.

        Falls back to common installation paths (cxplus symlink, repo dist build,
        global codex). Raises RuntimeError when no candidate is executable so the
        sidecar fails fast with a meaningful error instead of surfacing opaque
        FileNotFoundError traces at request time.
        """

        raw_candidates: List[str] = []
        env_spec = os.getenv("CODEX_CMD")
        if env_spec:
            raw_candidates.append(env_spec)

        home = Path.home()
        fallback_bases = [
            home / "workspace" / "experiments" / "codex" / "dist" / "bin" / "codex",
            home / ".local" / "bin" / "cxplus",
            home / ".local" / "bin" / "codex",
            "cxplus",
            "codex",
        ]
        for base in fallback_bases:
            raw_candidates.append(f"{base} exec --json")

        seen: set[str] = set()
        for spec in raw_candidates:
            if not spec:
                continue
            expanded = os.path.expanduser(spec.strip())
            if not expanded or expanded in seen:
                continue
            seen.add(expanded)
            try:
                parts = shlex.split(expanded)
            except ValueError:
                continue
            if not parts:
                continue
            head = parts[0]
            if os.path.isabs(head):
                if Path(head).exists():
                    return parts
            else:
                if shutil.which(head):
                    return parts
        raise RuntimeError(
            "Codex CLI not found. Set CODEX_CMD to a working codex exec command "
            "or install codex/cxplus so it is on PATH."
        )

    @staticmethod
    def load() -> "_Settings":
        try:
            codex_cmd = _Settings._resolve_codex_cmd()
        except RuntimeError as exc:  # pragma: no cover - configuration error
            raise RuntimeError(str(exc))

        input_mode = os.getenv("CODEX_INPUT_MODE", "prompt").strip().lower()
        prompt_flag = os.getenv("CODEX_PROMPT_FLAG", "--prompt")
        max_concurrency = int(os.getenv("MAX_CONCURRENCY", "4"))
        abs_timeout_s = float(os.getenv("ABS_TIMEOUT_S", "90"))
        idle_timeout_s = float(os.getenv("IDLE_TIMEOUT_S", "25"))
        retries = int(os.getenv("RETRIES", "1"))
        log_level = os.getenv("LOG_LEVEL", "info").lower()
        return _Settings(
            codex_cmd=codex_cmd,
            input_mode=input_mode,
            prompt_flag=prompt_flag,
            max_concurrency=max_concurrency,
            abs_timeout_s=abs_timeout_s,
            idle_timeout_s=idle_timeout_s,
            retries=retries,
            log_level=log_level,
        )


SETTINGS = _Settings.load()


def _vlog(*args) -> None:
    if SETTINGS.log_level == "debug":
        print("[DEBUG]", *args, file=sys.stderr)


def _ilog(*args) -> None:
    print("[INFO]", *args, file=sys.stderr)


# ---------------------------------------------------------------------------
# API models
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = Field(..., description="Model name (e.g., gpt-5).")
    messages: List[ChatMessage] = Field(..., description="OpenAI-style messages array.")
    stream: bool = Field(default=False)


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


class ProcessError(Exception):
    def __init__(self, code: int, stderr: str):
        super().__init__(f"Process exited with code {code}: {stderr[:2000]}")
        self.code = code
        self.stderr = stderr


async def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    try:
        if sys.platform != "win32":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass
    except Exception as exc:  # pragma: no cover - best effort cleanup
        _vlog("kill error", exc)


async def _run_codex_once(
    prompt: str,
    model: str,
    input_mode: str,
    args: List[str],
    prompt_flag: str,
    abs_timeout_s: float,
    idle_timeout_s: float,
) -> Tuple[int, str, str]:
    argv = list(args)
    stdin_data: Optional[bytes] = None

    if input_mode == "flag":
        argv += [prompt_flag, prompt]
    elif input_mode == "json":
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        stdin_data = (json.dumps(payload) + "\n").encode("utf-8")
    else:
        stdin_data = (prompt + "\n").encode("utf-8")

    _vlog("spawning", argv)

    preexec = os.setsid if sys.platform != "win32" else None
    creationflags = 0x00000200 if sys.platform == "win32" else 0

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=preexec if sys.platform != "win32" else None,
        creationflags=creationflags,
    )

    # Large Codex responses can exceed asyncio's default 64 KiB read limit and
    # trigger "Separator is not found" errors.  Increase the per-stream limit so
    # long JSON payloads are handled without tripping the guard rails.
    try:
        if proc.stdout is not None and getattr(proc.stdout, "_limit", None):
            proc.stdout._limit = max(proc.stdout._limit, 2_000_000)  # ~2 MiB
        if proc.stderr is not None and getattr(proc.stderr, "_limit", None):
            proc.stderr._limit = max(proc.stderr._limit, 2_000_000)
    except Exception:  # pragma: no cover - best effort only
        pass

    async def _writer() -> None:
        if stdin_data is not None and proc.stdin:
            try:
                proc.stdin.write(stdin_data)
                await proc.stdin.drain()
            except Exception:
                pass
        if proc.stdin:
            try:
                proc.stdin.close()
            except Exception:
                pass

    async def _reader() -> Tuple[str, str]:
        try:
            stdout_task = asyncio.create_task(proc.stdout.read()) if proc.stdout else None
            stderr_task = asyncio.create_task(proc.stderr.read()) if proc.stderr else None
            tasks = [t for t in (stdout_task, stderr_task) if t is not None]
            if not tasks:
                return "", ""
            done, pending = await asyncio.wait(
                tasks,
                timeout=abs_timeout_s,
                return_when=asyncio.ALL_COMPLETED,
            )
            if pending:
                for p in pending:
                    p.cancel()
                await _kill_process_tree(proc)
                raise TimeoutError(f"No output before {abs_timeout_s}s timeout")
            stdout_bytes = stdout_task.result() if stdout_task else b""
            stderr_bytes = stderr_task.result() if stderr_task else b""
        except TimeoutError:
            raise
        except Exception as exc:
            await _kill_process_tree(proc)
            raise exc
        stdout_text = stdout_bytes.decode("utf-8", errors="ignore") if isinstance(stdout_bytes, (bytes, bytearray)) else str(stdout_bytes or "")
        stderr_text = stderr_bytes.decode("utf-8", errors="ignore") if isinstance(stderr_bytes, (bytes, bytearray)) else str(stderr_bytes or "")
        return stdout_text, stderr_text

    try:
        writer_task = asyncio.create_task(_writer())
        reader_task = asyncio.create_task(_reader())
        done, pending = await asyncio.wait(
            {writer_task, reader_task},
            timeout=abs_timeout_s,
            return_when=asyncio.ALL_COMPLETED,
        )
        if pending:
            await _kill_process_tree(proc)
            for p in pending:
                p.cancel()
            raise TimeoutError(f"Absolute timeout after {abs_timeout_s}s")
        stdout_text, stderr_text = await reader_task
        code = await proc.wait()
        return code, stdout_text, stderr_text
    finally:  # pragma: no cover - cleanup path
        if proc.returncode is None:
            await _kill_process_tree(proc)


def _messages_to_prompt(messages: List[ChatMessage]) -> str:
    parts: List[str] = []
    for m in messages:
        role = (m.role or "").strip().lower()
        content = m.content or ""
        if role in ("system", "user"):
            parts.append(content)
        elif role == "assistant":
            parts.append(f"Assistant: {content}")
        else:
            parts.append(content)
    return "\n".join(parts).strip()


def _is_transient_exit(code: int) -> bool:
    return code in (70, 75)


_SEM = asyncio.Semaphore(SETTINGS.max_concurrency)


def _extract_codex_content(raw: str) -> str:
    """Extract the assistant message from Codex CLI JSONL output."""

    stripped = raw.strip()
    if not stripped:
        return stripped
    lines = [line for line in stripped.splitlines() if line.strip()]
    parsed = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            # fall back to raw payload if any line is not JSON
            return stripped

    reasoning_chunks: List[str] = []
    messages: List[str] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "item.completed":
            continue
        item = entry.get("item") or {}
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        if item.get("type") == "reasoning":
            reasoning_chunks.append(text.strip())
        elif item.get("type") == "agent_message":
            messages.append(text.strip())

    if messages:
        if reasoning_chunks:
            return "\n\n".join(reasoning_chunks + [messages[-1]]).strip()
        return messages[-1]
    if reasoning_chunks:
        return "\n\n".join(reasoning_chunks).strip()
    return stripped


# ---------------------------------------------------------------------------
# HTTP app
# ---------------------------------------------------------------------------


app = FastAPI(title="Codex Sidecar", version="0.1.0")


@app.get("/healthz")
async def healthz() -> Dict[str, object]:
    home = os.getenv("HOME", "/root")
    auth_path = os.getenv("CODEX_AUTH_PATH", os.path.join(home, ".codex", "auth.json"))
    auth_present = os.path.exists(auth_path)
    return {"ok": True, "concurrency": SETTINGS.max_concurrency, "auth_present": auth_present}


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request):
    # Optional echo mode for environments without Codex CLI installed
    try:
        if os.getenv("CODEX_SIDECAR_ECHO", "") == "1" or os.getenv("CODEX_ECHO_MODE", "") == "1":
            body = {
                "id": "chatcmpl-sidecar",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": req.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "shim ok"},
                        "finish_reason": "stop",
                    }
                ],
            }
            return JSONResponse(body)
    except Exception:
        pass
    # Preflight: require auth.json when not in echo mode to avoid opaque CLI failures
    try:
        echo = os.getenv("CODEX_SIDECAR_ECHO", "") == "1" or os.getenv("CODEX_ECHO_MODE", "") == "1"
        home = os.getenv("HOME", "/root")
        auth_path = os.getenv("CODEX_AUTH_PATH", os.path.join(home, ".codex", "auth.json"))
        if not echo and not os.path.exists(auth_path):
            raise HTTPException(
                status_code=401,
                detail=(
                    "codex-agent auth missing: expected credentials at "
                    f"{auth_path}. Mount ~/.codex/auth.json into the container (read-only), or set "
                    "CODEX_AUTH_PATH to an alternate location. For stub testing only, set CODEX_SIDECAR_ECHO=1."
                ),
            )
    except HTTPException:
        raise
    except Exception:
        # Best-effort preflight; continue to runtime call
        pass
    prompt = _messages_to_prompt(req.messages)
    if not prompt:
        raise HTTPException(status_code=400, detail="Empty prompt")

    async def _invoke_nonstream() -> JSONResponse:
        async with _SEM:
            retries = SETTINGS.retries
            attempt = 0
            last_err: Optional[str] = None
            while True:
                attempt += 1
                try:
                    code, stdout_text, stderr_text = await _run_codex_once(
                        prompt=prompt,
                        model=req.model,
                        input_mode=SETTINGS.input_mode,
                        args=SETTINGS.codex_cmd,
                        prompt_flag=SETTINGS.prompt_flag,
                        abs_timeout_s=SETTINGS.abs_timeout_s,
                        idle_timeout_s=SETTINGS.idle_timeout_s,
                    )
                    if code != 0:
                        if _is_transient_exit(code) and attempt <= retries + 1:
                            _ilog(f"Transient exit ({code}). retry {attempt}/{retries}")
                            await asyncio.sleep(min(1.0 * attempt, 3.0))
                            continue
                        raise ProcessError(code, stderr_text)
                    _vlog(f"codex stdout chars={len(stdout_text)} stderr chars={len(stderr_text)}")
                    content = _extract_codex_content(stdout_text)
                    body = {
                        "id": "chatcmpl-sidecar",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": req.model,
                        "choices": [
                            {
                                "index": 0,
                                # Always return a string per OpenAI compat; never null
                                "message": {"role": "assistant", "content": content if isinstance(content, str) else (str(content) if content is not None else "")},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                    return JSONResponse(body)
                except TimeoutError as exc:
                    last_err = str(exc)
                    if attempt <= retries + 1:
                        _ilog(f"Timeout, retry {attempt}/{retries}")
                        continue
                    raise HTTPException(status_code=504, detail=f"Timeout: {last_err}")
                except ProcessError as exc:
                    raise HTTPException(status_code=502, detail=f"Codex error ({exc.code}): {exc.stderr[:800]}")
                except FileNotFoundError as exc:  # pragma: no cover - guard
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            "Codex CLI not found (attempted: "
                            + " ".join(shlex.quote(part) for part in SETTINGS.codex_cmd)
                            + "). Set CODEX_CMD to an executable codex/cxplus binary."
                        ),
                    ) from exc
                except Exception as exc:  # noqa: BLE001
                    last_err = str(exc)
                    # If echo mode requested or Codex CLI missing, return a benign shim response
                    if os.getenv("CODEX_SIDECAR_ECHO", "") == "1" or os.getenv("CODEX_ECHO_MODE", "") == "1":
                        body = {
                            "id": "chatcmpl-sidecar",
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": req.model,
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": "shim ok"},
                                    "finish_reason": "stop",
                                }
                            ],
                        }
                        return JSONResponse(body)
                    raise HTTPException(status_code=500, detail=f"Unexpected: {last_err[:800]}")

    async def _invoke_stream() -> StreamingResponse:
        async def gen() -> AsyncIterator[bytes]:
            async with _SEM:
                retries = SETTINGS.retries
                attempt = 0
                while True:
                    attempt += 1
                    try:
                        code, stdout_text, stderr_text = await _run_codex_once(
                            prompt=prompt,
                            model=req.model,
                            input_mode=SETTINGS.input_mode,
                            args=SETTINGS.codex_cmd,
                            prompt_flag=SETTINGS.prompt_flag,
                            abs_timeout_s=SETTINGS.abs_timeout_s,
                            idle_timeout_s=SETTINGS.idle_timeout_s,
                        )
                        if code != 0:
                            if _is_transient_exit(code) and attempt <= retries + 1:
                                _ilog(f"Transient exit ({code}). retry {attempt}/{retries}")
                                await asyncio.sleep(min(1.0 * attempt, 3.0))
                                continue
                            err = {"error": {"message": f"Codex error ({code})"}}
                            yield f"data: {json.dumps(err)}\n\n".encode("utf-8")
                            return
                        for line in stdout_text.splitlines():
                            if not line:
                                continue
                            chunk = {
                                "id": "chatcmpl-sidecar",
                                "object": "chat.completion.chunk",
                                "model": req.model,
                                "choices": [{"index": 0, "delta": {"content": line + "\n"}}],
                            }
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                        yield b"data: [DONE]\n\n"
                        return
                    except TimeoutError as exc:
                        if attempt <= retries + 1:
                            _ilog(f"Timeout, retry {attempt}/{retries}")
                            await asyncio.sleep(min(1.0 * attempt, 3.0))
                            continue
                        err = {"error": {"message": f"Timeout: {str(exc)}"}}
                        yield f"data: {json.dumps(err)}\n\n".encode("utf-8")
                        return
                    except Exception as exc:  # noqa: BLE001
                        err = {"error": {"message": f"Unexpected: {str(exc)}"}}
                        yield f"data: {json.dumps(err)}\n\n".encode("utf-8")
                        return

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    if not req.stream:
        return await _invoke_nonstream()
    return await _invoke_stream()


@app.get("/v1/models")
async def list_models() -> Dict[str, object]:
    """Minimal stub for OpenAI-compatible model listing.

    Sidecar does not maintain a dynamic registry; provide a stable placeholder
    so clients that probe /v1/models don't fail.
    """
    return {"object": "list", "data": [{"id": "gpt-5", "object": "model"}]}


# ---------------------------------------------------------------------------
# Entrypoint helpers
# ---------------------------------------------------------------------------


def serve(host: str = "127.0.0.1", port: int = 8077) -> None:
    """Run the sidecar using uvicorn."""

    _ilog("Codex sidecar starting with config:")
    _ilog(f"  CODEX_CMD        = {SETTINGS.codex_cmd}")
    _ilog(f"  CODEX_INPUT_MODE = {SETTINGS.input_mode}")
    _ilog(f"  MAX_CONCURRENCY  = {SETTINGS.max_concurrency}")
    _ilog(f"  ABS_TIMEOUT_S    = {SETTINGS.abs_timeout_s}")
    _ilog(f"  IDLE_TIMEOUT_S   = {SETTINGS.idle_timeout_s}")
    _ilog(f"  RETRIES          = {SETTINGS.retries}")
    _ilog(f"  LOG_LEVEL        = {SETTINGS.log_level}")

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - runtime requirement
        print("uvicorn is required to run the Codex sidecar", file=sys.stderr)
        raise exc

    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":  # pragma: no cover - manual invocation only
    host = os.getenv("CODEX_SIDECAR_HOST", "127.0.0.1")
    port = int(os.getenv("CODEX_SIDECAR_PORT", "8077"))
    serve(host, port)
