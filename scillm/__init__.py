"""
scillm: Light-weight shim over the local litellm codebase
with minimal import-time side effects.

Goals
- Do not import litellm (and its optional integrations) at import time.
- Lazy-load litellm on first call and honor background-control env flags.
- Provide small helpers (JSON mode, preflight, shutdown/context) without
  forcing optional deps like Jinja2.
"""

from __future__ import annotations

import asyncio as _asyncio
import os as _os
import time as _time
from contextlib import contextmanager as _contextmanager, asynccontextmanager as _asynccontextmanager
from typing import Any as _Any, Dict as _Dict, Tuple as _Tuple
from urllib.parse import urlparse as _urlparse

# --- Internal lazy import helpers -------------------------------------------------

_LTL = None  # cached litellm module
_WRAPPED = False
_BG_DISABLED = False
_PENDING_BG_DISABLE = str(_os.getenv("LITELLM_LOGGING", "")).strip() == "0" or (
    str(_os.getenv("SPARTA_LITELLM_DISABLE_BG", "")).lower() in {"1", "true", "yes", "on"}
)
_FORCE_HTTPX = str(_os.getenv("SCILLM_DISABLE_AIOHTTP", "")).lower() in {"1", "true", "yes", "on"}
_AUTO_CACHE = str(_os.getenv("SCILLM_CACHE", "")).lower() in {"1", "true", "yes", "on"}


def _ensure_litellm() -> "module":
    global _LTL, _WRAPPED
    if _LTL is None:
        import importlib as _importlib

        _LTL = _importlib.import_module("litellm")
        try:
            _LTL.suppress_debug_info = True
        except Exception:
            pass

        # Apply forced httpx transport if requested
        if _FORCE_HTTPX:
            try:
                _os.environ.setdefault("DISABLE_AIOHTTP_TRANSPORT", "True")
                from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler as _AsyncHTTPHandler  # type: ignore

                _LTL.disable_aiohttp_transport = True
                _LTL.module_level_aclient = _AsyncHTTPHandler(
                    timeout=_LTL.request_timeout, client_alias="module level aclient"
                )
            except Exception:
                pass

        if _AUTO_CACHE:
            try:
                from litellm.extras import initialize_litellm_cache  # type: ignore

                initialize_litellm_cache()
            except Exception:
                pass

        if _PENDING_BG_DISABLE:
            try:
                disable_background_services()
            except Exception:
                pass

        # Defer patching completion until first call
        _WRAPPED = False
    return _LTL


def _patch_wrappers_if_needed():
    global _WRAPPED
    if _WRAPPED:
        return
    ltl = _ensure_litellm()
    # Save originals for retry logic
    globals()["_orig_completion"] = ltl.completion
    globals()["_orig_acompletion"] = ltl.acompletion
    # Patch litellm so Router and others see wrappers
    ltl.completion = completion  # type: ignore
    ltl.acompletion = acompletion  # type: ignore
    _WRAPPED = True

# Lightweight Router passthrough so extras can import Router from scillm
def Router(*args, **kwargs):  # type: ignore
    ltl = _ensure_litellm()
    return ltl.Router(*args, **kwargs)

# ---------------------------------------------------------------------------
# Transitional auth canonicalization for Chutes /v1 (until upstream normalizes)
# ---------------------------------------------------------------------------

def _sc_is_chutes_base(api_base: str | None) -> bool:
    if not api_base:
        return False
    try:
        host = _urlparse(api_base).netloc.lower()
    except Exception:
        return False
    return host.endswith("chutes.ai")

_SC_WINNERS: dict[str, tuple[str, float]] = {}  # base -> (style, expiry_ts)

def _sc_canon_headers_for_chutes(api_base: str | None, api_key: str | None, headers: dict | None, _no_canon: bool = False):
    """If base is Chutes and canonicalization is enabled, ensure Authorization: Bearer <token>.
    Rules:
    - Prefer existing Bearer token
    - Else use x-api-key
    - Else use raw Authorization
    - Else fall back to api_key
    Returns (api_key, headers) possibly modified.
    """
    if not _sc_is_chutes_base(api_base) or _no_canon:
        return api_key, headers
    if str(_os.getenv("SCILLM_CHUTES_CANONICALIZE_OPENAI_AUTH", "1")).lower() not in {"1","true","yes","on"}:
        return api_key, headers
    h = dict(headers or {})
    base = (api_base or "").strip()
    # Winner cache (5 minutes)
    sty, exp = _SC_WINNERS.get(base, ("", 0.0)) if base else ("", 0.0)
    now = _time.time()
    if exp > now and sty in {"bearer","x-api-key"}:
        token = (h.get("Authorization") or h.get("authorization") or h.get("x-api-key") or h.get("X-API-Key") or api_key or "").strip()
        if token:
            if sty == "bearer":
                if token.lower().startswith("bearer "):
                    h["Authorization"] = token
                else:
                    h["Authorization"] = f"Bearer {token}"
                h.pop("x-api-key", None); h.pop("X-API-Key", None)
            else:
                # prefer x-api-key
                # strip possible Bearer
                if isinstance(token, str) and token.lower().startswith("bearer "):
                    token = token.split(" ",1)[-1]
                h["x-api-key"] = token
                h.pop("Authorization", None); h.pop("authorization", None)
            return api_key, h
    token = None
    auth = h.get("Authorization") or h.get("authorization")
    if isinstance(auth, str) and auth.strip().lower().startswith("bearer "):
        return api_key, h  # already canonical
    # derive token from headers or api_key
    if isinstance(auth, str) and auth.strip():
        token = auth.strip().split(" ", 1)[-1]
    if not token:
        xk = h.get("x-api-key") or h.get("X-API-Key")
        if isinstance(xk, str) and xk.strip():
            token = xk.strip()
    if not token and api_key:
        token = str(api_key).strip()
    if token:
        # Default winner is bearer (safer across endpoints)
        _SC_WINNERS[base] = ("bearer", now + 300.0)
        h.pop("x-api-key", None); h.pop("X-API-Key", None)
        h["Authorization"] = f"Bearer {token}"
        # Keep api_key as-is; litellm may also add Bearer, which is fine
        return api_key, h
    return api_key, h

class EmptyContentError(Exception):
    def __init__(self, model: str, provider: str):
        super().__init__("Empty response content")
        self.reason = "empty_content"
        self.retry_after = None
        self.model = model
        self.provider = provider


def _sc_allow_empty_responses() -> bool:
    return _os.getenv("SCILLM_ALLOW_EMPTY_RESPONSES", "0").lower() in {"1", "true", "yes", "y"}


def _sc_messages_have_prompt(messages: list[dict]) -> bool:
    for msg in messages or []:
        role = (msg.get("role") or "").lower()
        if role not in {"user", "system"}:
            continue
        content = msg.get("content")
        if _sc_has_substantive_content(content):
            return True
    return False


def _sc_has_substantive_content(content) -> bool:
    if content is None:
        return False
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text" and _sc_has_substantive_content(part.get("text")):
                    return True
                if "content" in part and _sc_has_substantive_content(part.get("content")):
                    return True
            elif isinstance(part, str) and part.strip():
                return True
        return False
    if isinstance(content, dict):
        for value in content.values():
            if _sc_has_substantive_content(value):
                return True
        return False
    return False


def _sc_response_is_empty(resp) -> bool:
    try:
        choice = resp.choices[0]
        msg = choice.message
        payload = getattr(msg, "content", None)
        # Fallback for providers that deliver text in reasoning_content when content is null
        if (payload is None or payload == "") and hasattr(msg, "get"):
            try:
                rc = msg.get("reasoning_content")
                if isinstance(rc, str) and rc.strip():
                    payload = rc
            except Exception:
                pass
        if payload is None and hasattr(msg, "get"):
            try:
                payload = msg.get("content")
            except Exception:
                payload = None
        return not _sc_has_substantive_content(payload)
    except Exception:
        return False

def _sc_postprocess_require_nonempty(resp):
    """Optional: map empty strings to null for selected JSON keys in content.
    Controlled by env:
      - SCILLM_REQUIRE_NONEMPTY: 1/true to apply to all top-level string fields
      - SCILLM_REQUIRE_NONEMPTY_KEYS: comma-separated keys (e.g., "title,number")
    Never raises.
    """
    try:
        import json as _json
        need_all = str(_os.getenv("SCILLM_REQUIRE_NONEMPTY", "0")).lower() in {"1","true","yes","on"}
        keys_env = (_os.getenv("SCILLM_REQUIRE_NONEMPTY_KEYS") or "").strip()
        keys = {k.strip() for k in keys_env.split(",") if k.strip()}
        if not need_all and not keys:
            return resp
        content = getattr(resp.choices[0].message, "content", None)
        if isinstance(content, str) and content.strip():
            try:
                obj = _json.loads(content)
            except Exception:
                return resp
            if not isinstance(obj, dict):
                return resp
            changed = False
            if need_all and not keys:
                for k, v in list(obj.items()):
                    if isinstance(v, str) and v == "":
                        obj[k] = None
                        changed = True
            else:
                for k in keys:
                    if k in obj and isinstance(obj[k], str) and obj[k] == "":
                        obj[k] = None
                        changed = True
            if changed:
                resp.choices[0].message["content"] = _json.dumps(obj, ensure_ascii=False)
    except Exception:
        pass
    return resp

from .http_openai_like import direct_openai_chat as _direct_openai_chat  # factored out


def completion(*args, **kwargs):  # type: ignore[no-redef]
    _patch_wrappers_if_needed()
    try:
        from litellm.exceptions import AuthenticationError as _AuthErr  # type: ignore
    except Exception:  # pragma: no cover - fallback when mocking
        class _AuthErr(Exception):
            pass
    try:
        from litellm.llms.openai_like.common_utils import OpenAILikeError as _OAILikeErr  # type: ignore
    except Exception:  # pragma: no cover - fallback when extra not present
        class _OAILikeErr(Exception):
            pass
    try:
        api_base = kwargs.get("api_base")
        api_key = kwargs.get("api_key")
        headers = kwargs.get("extra_headers")
        api_key, headers = _sc_canon_headers_for_chutes(api_base, api_key, headers)
        if headers is not None:
            kwargs["extra_headers"] = headers
        retry_on_empty = kwargs.pop("retry_on_empty", True)
        empty_retries = int(kwargs.pop("empty_retries", 1) or 0)
        empty_backoff_ms = int(kwargs.pop("empty_backoff_ms", 100))
        bump_tokens = bool(kwargs.pop("bump_max_tokens_on_empty", False))
        bump_amount = int(kwargs.pop("max_tokens_bump", 160))
        model = args[0] if args else kwargs.get("model")
        messages = kwargs.get("messages") or []
        attempts = 0
        base_max_tokens = kwargs.get("max_tokens")
        last_exc = None
        while True:
            attempts += 1
            try:
                resp = globals()["_orig_completion"](*args, **kwargs)
            except (_AuthErr, _OAILikeErr) as e:
                msg = str(getattr(e, "message", e))
                if "401" in msg or "Unauthorized" in msg or "invalid auth" in msg.lower():
                    token = (headers or {}).get("Authorization") or (headers or {}).get("x-api-key") or api_key or ""
                    token = str(token)
                    if token.lower().startswith("bearer "):
                        token = token.split(" ",1)[-1]
                    alt_headers = dict(headers or {})
                    alt_headers.pop("Authorization", None); alt_headers.pop("authorization", None)
                    alt_headers["x-api-key"] = token
                    kwargs_alt = dict(kwargs)
                    kwargs_alt["extra_headers"] = alt_headers
                    kwargs_alt["_sc_no_canon"] = True
                    resp = globals()["_orig_completion"](*args, **kwargs_alt)
                    if _sc_is_chutes_base(api_base):
                        _SC_WINNERS[str(api_base)] = ("x-api-key", _time.time() + 300.0)
                else:
                    raise
            if not (retry_on_empty and not _sc_allow_empty_responses() and _sc_messages_have_prompt(messages)):
                return _sc_postprocess_require_nonempty(resp)
            if not _sc_response_is_empty(resp):
                result = _sc_postprocess_require_nonempty(resp)
                try:
                    meta = dict(getattr(result, "scillm_meta", {}) or {})
                    meta.setdefault("attempts", attempts)
                    setattr(result, "scillm_meta", meta)
                except Exception:
                    pass
                return result
            if attempts > empty_retries + 1:
                last_exc = EmptyContentError(model=model or "unknown", provider="chutes")
                try:
                    setattr(last_exc, "scillm_meta", {"reason": "empty_content", "attempts": attempts})
                except Exception:
                    pass
                break
            if bump_tokens and kwargs.get("max_tokens") is not None:
                try:
                    kwargs["max_tokens"] = int(kwargs.get("max_tokens") or base_max_tokens or 0) + bump_amount
                except Exception:
                    pass
            if empty_backoff_ms > 0:
                _time.sleep(empty_backoff_ms / 1000.0)
        if last_exc:
            raise last_exc
        return _sc_postprocess_require_nonempty(resp)
    except Exception as e:
        raise _normalize_quota_exception(e)

async def acompletion(*args, **kwargs):  # type: ignore[no-redef]
    _patch_wrappers_if_needed()
    try:
        from litellm.exceptions import AuthenticationError as _AuthErr  # type: ignore
    except Exception:  # pragma: no cover
        class _AuthErr(Exception):
            pass
    try:
        from litellm.llms.openai_like.common_utils import OpenAILikeError as _OAILikeErr  # type: ignore
    except Exception:  # pragma: no cover
        class _OAILikeErr(Exception):
            pass
    try:
        api_base = kwargs.get("api_base")
        api_key = kwargs.get("api_key")
        headers = kwargs.get("extra_headers")
        api_key, headers = _sc_canon_headers_for_chutes(api_base, api_key, headers)
        if headers is not None:
            kwargs["extra_headers"] = headers
        retry_on_empty = kwargs.pop("retry_on_empty", True)
        empty_retries = int(kwargs.pop("empty_retries", 1) or 0)
        empty_backoff_ms = int(kwargs.pop("empty_backoff_ms", 100))
        bump_tokens = bool(kwargs.pop("bump_max_tokens_on_empty", False))
        bump_amount = int(kwargs.pop("max_tokens_bump", 160))
        model = args[0] if args else kwargs.get("model")
        messages = kwargs.get("messages") or []
        attempts = 0
        base_max_tokens = kwargs.get("max_tokens")
        last_exc = None
        while True:
            attempts += 1
            try:
                resp = await globals()["_orig_acompletion"](*args, **kwargs)
            except (_AuthErr, _OAILikeErr) as e:
                msg = str(getattr(e, "message", e))
                if "401" in msg or "Unauthorized" in msg or "invalid auth" in msg.lower():
                    token = (headers or {}).get("Authorization") or (headers or {}).get("x-api-key") or api_key or ""
                    token = str(token)
                    if token.lower().startswith("bearer "):
                        token = token.split(" ",1)[-1]
                    alt_headers = dict(headers or {})
                    alt_headers.pop("Authorization", None); alt_headers.pop("authorization", None)
                    alt_headers["x-api-key"] = token
                    kwargs_alt = dict(kwargs)
                    kwargs_alt["extra_headers"] = alt_headers
                    kwargs_alt["_sc_no_canon"] = True
                    resp = await globals()["_orig_acompletion"](*args, **kwargs_alt)
                    if _sc_is_chutes_base(api_base):
                        _SC_WINNERS[str(api_base)] = ("x-api-key", _time.time() + 300.0)
                else:
                    # For Chutes bases, attempt a direct HTTP call (curl-equivalent)
                    if _sc_is_chutes_base(api_base):
                        try:
                            payload = {
                                "model": model,
                                "messages": messages,
                                "max_tokens": kwargs.get("max_tokens"),
                                "temperature": kwargs.get("temperature"),
                            }
                            if kwargs.get("response_format"):
                                payload["response_format"] = kwargs.get("response_format")
                            resp = _direct_openai_chat(api_base, api_key, payload=payload, timeout=float(kwargs.get("timeout") or 20.0))
                        except Exception:
                            pass
                    if resp is None:
                        raise
            except Exception as e_all:
                # Network/5xx fallback path for Chutes
                status = getattr(e_all, "status", None) or getattr(e_all, "status_code", None) or getattr(e_all, "http_status", None)
                msg_low = str(e_all).lower()
                transient_5xx = (status in {500,502,503,504}) or ("service unavailable" in msg_low) or ("temporarily" in msg_low)
                if _sc_is_chutes_base(api_base) and transient_5xx:
                    try:
                        payload = {
                            "model": model,
                            "messages": messages,
                            "max_tokens": kwargs.get("max_tokens"),
                            "temperature": kwargs.get("temperature"),
                        }
                        if kwargs.get("response_format"):
                            payload["response_format"] = kwargs.get("response_format")
                        resp = _direct_openai_chat(api_base, api_key, payload=payload, timeout=float(kwargs.get("timeout") or 20.0))
                    except Exception:
                        raise e_all
                else:
                    raise e_all
            if not (retry_on_empty and not _sc_allow_empty_responses() and _sc_messages_have_prompt(messages)):
                return _sc_postprocess_require_nonempty(resp)
            if not _sc_response_is_empty(resp):
                result = _sc_postprocess_require_nonempty(resp)
                try:
                    meta = dict(getattr(result, "scillm_meta", {}) or {})
                    meta.setdefault("attempts", attempts)
                    setattr(result, "scillm_meta", meta)
                except Exception:
                    pass
                return result
            if attempts > empty_retries + 1:
                last_exc = EmptyContentError(model=model or "unknown", provider="chutes")
                try:
                    setattr(last_exc, "scillm_meta", {"reason": "empty_content", "attempts": attempts})
                except Exception:
                    pass
                break
            if bump_tokens and kwargs.get("max_tokens") is not None:
                try:
                    kwargs["max_tokens"] = int(kwargs.get("max_tokens") or base_max_tokens or 0) + bump_amount
                except Exception:
                    pass
            if empty_backoff_ms > 0:
                await _asyncio.sleep(empty_backoff_ms / 1000.0)
        if last_exc:
            # Final direct-call fallback for Chutes if still empty
            if _sc_is_chutes_base(api_base):
                try:
                    payload = {
                        "model": model,
                        "messages": messages,
                        "max_tokens": kwargs.get("max_tokens"),
                        "temperature": kwargs.get("temperature"),
                    }
                    if kwargs.get("response_format"):
                        payload["response_format"] = kwargs.get("response_format")
                    resp2 = _direct_openai_chat(api_base, api_key, payload=payload, timeout=float(kwargs.get("timeout") or 20.0))
                    return _sc_postprocess_require_nonempty(resp2)
                except Exception:
                    pass
            raise last_exc
    except Exception as e:
        raise _normalize_quota_exception(e)

# ---------------- Background controls & shutdown ----------------------------------

def disable_background_services() -> None:
    """Disable litellm background logging worker (idempotent)."""
    global _BG_DISABLED
    ltl = _ensure_litellm()
    try:
        from litellm.litellm_core_utils import logging_worker as _lw  # type: ignore

        class _NoopLoggingWorker(_lw.LoggingWorker):  # type: ignore
            def _ensure_queue(self):
                return

            def start(self):  # noqa: D401
                return

            def enqueue(self, coroutine):  # type: ignore[override]
                return

            def ensure_initialized_and_enqueue(self, async_coroutine):  # type: ignore[override]
                return

            async def stop(self):  # type: ignore[override]
                return

            async def flush(self):  # type: ignore[override]
                return

            async def clear_queue(self):  # type: ignore[override]
                return

        _lw.GLOBAL_LOGGING_WORKER = _NoopLoggingWorker()  # type: ignore
        _BG_DISABLED = True
    except Exception:
        pass


def _run_coro_sync(coro):
    try:
        loop = _asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        fut = _asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return fut.result(timeout=5)
        except Exception:
            pass
    if loop and not loop.is_closed():
        return loop.run_until_complete(coro)
    new_loop = _asyncio.new_event_loop()
    try:
        return new_loop.run_until_complete(coro)
    finally:
        new_loop.close()


def shutdown_clients() -> None:
    """Close global httpx/aiohttp clients and logging queues."""
    try:
        ltl = _ensure_litellm()
    except Exception:
        return
    # Close aiohttp base handler
    try:
        from litellm.main import base_llm_aiohttp_handler  # type: ignore

        async def _close_aiohttp():
            with _asyncio.CancelledError:  # type: ignore
                pass
            try:
                await base_llm_aiohttp_handler.close()  # type: ignore
            except Exception:
                pass

        _run_coro_sync(_close_aiohttp())
    except Exception:
        pass
    # Close module-level httpx
    try:
        acl = getattr(ltl, "module_level_aclient", None)
        if acl is not None and hasattr(acl, "close"):
            async def _close_httpx():
                try:
                    await acl.close()
                except Exception:
                    pass

            _run_coro_sync(_close_httpx())
    except Exception:
        pass
    # Attempt to stop logging worker if present
    try:
        from litellm.litellm_core_utils import logging_worker as _lw  # type: ignore

        worker = getattr(_lw, "GLOBAL_LOGGING_WORKER", None)
        if worker is not None and hasattr(worker, "stop"):
            _run_coro_sync(worker.stop())  # type: ignore
    except Exception:
        pass


shutdown = shutdown_clients


@_contextmanager
def scoped():
    """Sync context: use scillm, then shutdown cleanly."""
    try:
        yield
    finally:
        shutdown()


@_asynccontextmanager
async def ascoped():
    """Async context: use scillm, then shutdown cleanly."""
    try:
        yield
    finally:
        await _asyncio.to_thread(shutdown)


# -------------------- JSON mode + preflight helpers -------------------------------

def _bearer_headers(api_key: str | None, extra_headers: _Dict | None = None) -> _Dict:
    h = dict(extra_headers or {})
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


def completion_json(model: str, messages: list[dict], *, max_tokens: int | None = None, api_base: str | None = None, api_key: str | None = None, **kwargs):
    """Sync JSON-mode helper mirroring OpenAI-compatible response."""
    kwargs = dict(kwargs)
    kwargs["response_format"] = {"type": "json_object"}
    kwargs["extra_headers"] = _bearer_headers(api_key, kwargs.get("extra_headers"))
    return completion(model=model, messages=messages, max_tokens=max_tokens, api_base=api_base, api_key=api_key, **kwargs)


async def acompletion_json(model: str, messages: list[dict], *, max_tokens: int | None = None, api_base: str | None = None, api_key: str | None = None, **kwargs):
    kwargs = dict(kwargs)
    kwargs["response_format"] = {"type": "json_object"}
    kwargs["extra_headers"] = _bearer_headers(api_key, kwargs.get("extra_headers"))
    return await acompletion(model=model, messages=messages, max_tokens=max_tokens, api_base=api_base, api_key=api_key, **kwargs)


from .batch import (
    parallel_acompletions,
    parallel_acompletions_env,
    parallel_acompletions_iter,
    parallel_acompletions_simple,
    parallel_acompletions_simple_env,
)


def models_probe(api_base: str, api_key: str | None = None) -> _Dict:
    import httpx as _httpx

    t0 = _time.time()
    try:
        resp = _httpx.get(f"{api_base.rstrip('/')}/v1/models", headers=_bearer_headers(api_key), timeout=10)
        return {"ok": resp.status_code == 200, "status": resp.status_code, "elapsed_ms": int((_time.time()-t0)*1000), "body_head": resp.text[:256]}
    except Exception as e:
        return {"ok": False, "status": None, "elapsed_ms": int((_time.time()-t0)*1000), "error": str(e)[:256]}


def chat_probe_json(api_base: str, api_key: str | None, model: str) -> _Dict:
    import httpx as _httpx
    t0 = _time.time()
    try:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Return {\"ok\":true}"}],
            "response_format": {"type": "json_object"},
            "max_tokens": 16,
        }
        resp = _httpx.post(f"{api_base.rstrip('/')}/v1/chat/completions", headers=_bearer_headers(api_key), json=payload, timeout=20)
        return {"ok": resp.status_code == 200, "status": resp.status_code, "elapsed_ms": int((_time.time()-t0)*1000), "body_head": resp.text[:256]}
    except Exception as e:
        return {"ok": False, "status": None, "elapsed_ms": int((_time.time()-t0)*1000), "error": str(e)[:256]}


# -------------------- Quota/Cap signaling ----------------------------------------

class QuotaExceededError(Exception):
    pass


def _normalize_quota_exception(exc: Exception) -> Exception:
    txt = str(getattr(exc, "message", exc)).lower()
    if any(k in txt for k in ("quota", "cap", "limit exceeded", "out of credits", "insufficient_quota")):
        return QuotaExceededError(str(exc))
    return exc
