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

import atexit as _atexit
import asyncio as _asyncio
import importlib.util as _ilu
import os as _os
import sys as _sys
import time as _time
from contextlib import contextmanager as _contextmanager, asynccontextmanager as _asynccontextmanager
from typing import Any as _Any, Dict as _Dict, Tuple as _Tuple
from urllib.parse import urlparse as _urlparse

# ---------------------------------------------------------------------------
# Package aliasing for sibling loaders
# ---------------------------------------------------------------------------
# Some downstream repos (e.g., extractor) vendor a minimal shim that loads this
# file via ``importlib.util.spec_from_file_location("_scillm_sibling", …)``. In
# that mode Python does not record us as the real ``scillm`` package, so relative
# imports such as ``from .http_openai_like import …`` would fail and the shim
# would conclude that ``scillm.completion`` is unavailable. To keep the paved
# path intact we retroactively register the canonical package + search path when
# we detect that we were imported under an alternate name.
if "__path__" not in globals():
    __path__ = [_os.path.dirname(__file__)]  # type: ignore[assignment]


def _import_local_module(alias: str, relative_filename: str):
    path = _os.path.join(_os.path.dirname(__file__), relative_filename)
    spec = _ilu.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(f"Unable to load {relative_filename} from {path}")
    module = _ilu.module_from_spec(spec)
    _sys.modules.setdefault(alias, module)
    spec.loader.exec_module(module)
    return module

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


class JsonParseError(Exception):
    def __init__(self, model: str, provider: str, sample: str, raw_len: int):
        super().__init__(f"json_parse_failed sample='{sample}'")
        self.reason = "json_parse_failed"
        self.model = model
        self.provider = provider
        self.sample = sample
        self.raw_len = raw_len


def _sc_strict_json_enabled(kwargs: dict) -> bool:
    env = str(_os.getenv("SCILLM_JSON_STRICT", "0")).lower() in {"1", "true", "yes", "on"}
    flag = bool(kwargs.get("strict_json")) or bool(kwargs.get("_sc_strict_json"))
    return env or flag


def _sc_validate_json_content(resp, model: str, provider: str):
    """
    Strict JSON validation (opt-in via SCILLM_JSON_STRICT=1 or strict_json=True).
    Raises JsonParseError with sample + raw length for easy debugging.
    """
    try:
        import json as _json
        content = getattr(resp.choices[0].message, "content", None)
        if not isinstance(content, str) or not content.strip():
            raise JsonParseError(model=model, provider=provider, sample=str(content)[:240], raw_len=len(content or ""))  # type: ignore[arg-type]
        try:
            _json.loads(content)
            return  # success
        except Exception:
            sample = content.strip()[:240]
            raise JsonParseError(model=model, provider=provider, sample=sample, raw_len=len(content))
    except JsonParseError as jpe:
        try:
            setattr(jpe, "scillm_meta", {"reason": jpe.reason, "model": model, "provider": provider, "raw_len": jpe.raw_len, "sample": jpe.sample})
        except Exception:
            pass
        raise
    except Exception:
        # Do not block callers on validator failure
        return


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

try:  # pragma: no cover - prefer standard package import
    from .http_openai_like import direct_openai_chat as _direct_openai_chat  # type: ignore
except (ModuleNotFoundError, ImportError):
    _http_mod = _import_local_module("_scillm_http_openai_like", "http_openai_like.py")
    _direct_openai_chat = getattr(_http_mod, "direct_openai_chat")


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
        provider = kwargs.get("custom_llm_provider") or "openai_like"
        # 5xx retry knobs (opt-in by default)
        retry_5xx = bool(kwargs.pop("retry_5xx", str(_os.getenv("SCILLM_RETRY_5XX", "1")).lower() in {"1","true","yes","on"}))
        max_retries_5xx = int(kwargs.pop("max_retries_5xx", _os.getenv("SCILLM_MAX_RETRIES_5XX", "3")) or 0)
        backoff_base_5xx = float(kwargs.pop("backoff_base_5xx", _os.getenv("SCILLM_BACKOFF_BASE_5XX", "0.5")) or 0.5)
        backoff_cap_5xx = float(kwargs.pop("backoff_cap_5xx", _os.getenv("SCILLM_BACKOFF_CAP_5XX", "8")) or 8.0)
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
                result = _sc_postprocess_require_nonempty(resp)
                if _sc_strict_json_enabled(kwargs):
                    _sc_validate_json_content(result, model=model or "unknown", provider=provider)
                return result
            if not _sc_response_is_empty(resp):
                result = _sc_postprocess_require_nonempty(resp)
                if _sc_strict_json_enabled(kwargs):
                    _sc_validate_json_content(result, model=model or "unknown", provider=provider)
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
        provider = kwargs.get("custom_llm_provider") or "openai_like"
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
                if retry_5xx and transient_5xx and attempts <= max_retries_5xx:
                    delay = min(backoff_cap_5xx, backoff_base_5xx * (2 ** max(0, attempts - 1)))
                    try:
                        _time.sleep(delay)
                    except Exception:
                        pass
                    continue
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
                result = _sc_postprocess_require_nonempty(resp)
                if _sc_strict_json_enabled(kwargs):
                    _sc_validate_json_content(result, model=model or "unknown", provider=provider)
                return result
            if not _sc_response_is_empty(resp):
                result = _sc_postprocess_require_nonempty(resp)
                if _sc_strict_json_enabled(kwargs):
                    _sc_validate_json_content(result, model=model or "unknown", provider=provider)
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
    """Disable litellm background logging worker (idempotent, no stray coroutines)."""

    import inspect
    global _BG_DISABLED
    ltl = _ensure_litellm()
    try:
        from litellm.litellm_core_utils import logging_worker as _lw  # type: ignore

        # Best-effort: stop the existing worker and clear its queue to avoid
        # "coroutine was never awaited" warnings on shutdown.
        try:
            worker = getattr(_lw, "GLOBAL_LOGGING_WORKER", None)
            if worker is not None:
                async def _stop():
                    with _asyncio.CancelledError:  # type: ignore
                        pass
                    try:
                        await worker.flush()  # type: ignore
                    except Exception:
                        pass
                    try:
                        await worker.stop()  # type: ignore
                    except Exception:
                        pass
                _run_coro_sync(_stop())
        except Exception:
            pass

        class _NullLoggingWorker(_lw.LoggingWorker):  # type: ignore
            def _ensure_queue(self):  # noqa: D401
                # Minimal queue to keep interface stable
                self._queue = None

            def start(self):  # noqa: D401
                return

            def enqueue(self, coroutine):  # type: ignore[override]
                # Immediately close/consume coroutine to avoid runtime warnings
                try:
                    if inspect.iscoroutine(coroutine):
                        coroutine.close()
                except Exception:
                    pass
                return

            def ensure_initialized_and_enqueue(self, async_coroutine):  # type: ignore[override]
                return self.enqueue(async_coroutine)

            async def stop(self):  # type: ignore[override]
                return

            async def flush(self):  # type: ignore[override]
                return

            async def clear_queue(self):  # type: ignore[override]
                return

        _lw.GLOBAL_LOGGING_WORKER = _NullLoggingWorker()  # type: ignore
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
try:
    _atexit.register(shutdown_clients)
except Exception:
    pass


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


try:  # pragma: no cover - prefer package import
    from .batch import (
        parallel_acompletions,
        parallel_acompletions_env,
        parallel_acompletions_iter,
        parallel_acompletions_simple,
        parallel_acompletions_simple_env,
    )
except (ModuleNotFoundError, ImportError):
    _batch_mod = _import_local_module("_scillm_batch", "batch.py")
    parallel_acompletions = getattr(_batch_mod, "parallel_acompletions")
    parallel_acompletions_env = getattr(_batch_mod, "parallel_acompletions_env")
    parallel_acompletions_iter = getattr(_batch_mod, "parallel_acompletions_iter")
    parallel_acompletions_simple = getattr(_batch_mod, "parallel_acompletions_simple")
    parallel_acompletions_simple_env = getattr(_batch_mod, "parallel_acompletions_simple_env")


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


# -------------------- Quota/Cap signaling + usage helpers ------------------------


class QuotaExceededError(Exception):
    """Raised when Chutes/SciLLM determines quota/budget is exhausted.

    This is intentionally stricter than a generic 429: we only promote to
    QuotaExceededError when the underlying error (HTTP status / JSON body)
    clearly indicates quota/credits exhaustion, not transient capacity.
    """


class CapacityExceededError(Exception):
    """Raised when the provider signals capacity/rate-limit (HTTP 429)."""

    def __init__(self, message: str, *, model: str | None = None, provider: str | None = None, status: int | None = None, retry_after: float | None = None, reason: str = "capacity_exhausted"):
        super().__init__(message)
        self.model = model
        self.provider = provider
        self.status = status
        self.retry_after = retry_after
        self.reason = reason


def _normalize_quota_exception(exc: Exception) -> Exception:
    """
    Best-effort normalization of provider errors into QuotaExceededError.

    Strategy:
    - Inspect HTTP status and JSON error body (when available) for explicit
      quota/budget signals (e.g. type == "budget_exhausted", "insufficient_quota").
    - Fall back to keyword heuristics on the message as a last resort.
    - Leave capacity/infra 429s and generic 5xx alone so callers can treat
      them as transient.
    """
    try:
        try:
            from litellm.exceptions import RateLimitError as _RateLimitError  # type: ignore
        except Exception:  # pragma: no cover - litellm not importable in some slim envs
            _RateLimitError = None  # type: ignore

        # Prefer structured signals when litellm exposes an httpx.Response
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        body_type = None
        body_code = None
        body_msg = None
        resp = getattr(exc, "response", None)
        if status is None and hasattr(resp, "status_code"):
            try:
                status = resp.status_code
            except Exception:
                pass
        if resp is not None:
            # httpx.Response or similar
            try:
                if hasattr(resp, "json"):
                    data = resp.json()
                else:
                    data = None
            except Exception:
                data = None
            if isinstance(data, dict):
                err = data.get("error") or data
                body_type = str(err.get("type") or "").lower() if isinstance(err, dict) else None
                body_code = str(err.get("code") or "").lower() if isinstance(err, dict) else None
                body_msg = str(err.get("message") or "") if isinstance(err, dict) else None
        msg = str(getattr(exc, "message", exc))
        txt = (body_msg or msg or "").lower()

        # Explicit quota/budget signals from body
        quota_keywords = ("quota", "limit exceeded", "out of credits", "insufficient_quota", "budget_exhausted")
        if body_type and any(k in body_type for k in ("budget_exhausted", "quota", "insufficient_quota")):
            return QuotaExceededError(msg)
        if body_code and any(k in body_code for k in ("quota", "insufficient_quota")):
            return QuotaExceededError(msg)

        # Status-based hint: 402 is reserved for billing/quota on Chutes
        if isinstance(status, int) and status == 402:
            return QuotaExceededError(msg)

        # Last-resort heuristic on message text (kept for backwards compatibility)
        if any(k in txt for k in quota_keywords):
            return QuotaExceededError(msg)

        # Capacity / rate-limit normalization (429) to keep surfaced exceptions clean
        if _RateLimitError is not None and isinstance(exc, _RateLimitError):
            reason = "capacity_exhausted"
            retry_after = getattr(exc, "retry_after", None)
            provider = getattr(exc, "llm_provider", None) or getattr(exc, "provider", None)
            model = getattr(exc, "model", None)
            cap = CapacityExceededError(msg, model=model, provider=provider, status=status, retry_after=retry_after, reason=reason)
            try:
                setattr(cap, "scillm_meta", {
                    "reason": reason,
                    "status": status,
                    "retry_after": retry_after,
                    "provider": provider,
                    "model": model,
                })
            except Exception:
                pass
            return cap
        return exc
    except Exception:
        # Never fail normalization; fall back to original exception
        return exc


def budget_status() -> _Dict:
    """
    Return a lightweight budget/usage snapshot for Chutes.

    Delegates to chutes.middleware.budget_guard.budget_metadata() when
    CHUTES_API_BASE / CHUTES_API_KEY are configured; otherwise returns {}.

    Fields (when available):
      - plan: str
      - daily_limit: int
      - used_today: int
      - remaining: int
      - reset_at: ISO8601 string
      - credits_balance: float|null
    """
    try:
        from chutes.middleware.budget_guard import budget_metadata  # type: ignore
    except Exception:
        return {}
    try:
        return budget_metadata() or {}
    except Exception:
        return {}


def payg_status() -> _Dict:
    """
    Return a coarse PAYG status snapshot for Chutes.

    Delegates to chutes.middleware.budget_guard.payg_snapshot().
    Fields (when available):
      - ok: bool
      - payg_active: bool
      - used: int
      - limit: int
      - remaining: int
      - cost_usd_today: float|null
      - reset_at: ISO8601 string
    """
    try:
        from chutes.middleware.budget_guard import payg_snapshot  # type: ignore
    except Exception:
        return {}
    try:
        snap = payg_snapshot()
        return snap or {}
    except Exception:
        return {}
