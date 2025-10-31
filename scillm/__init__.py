# Re-export litellm surface for convenience when installed as 'scillm'.
from litellm import *  # noqa: F401,F403
import litellm as _litellm
from urllib.parse import urlparse as _urlparse
import time as _time
from litellm.exceptions import AuthenticationError as _AuthErr
try:
    from litellm.llms.openai_like.common_utils import OpenAILikeError as _OAILikeErr
except Exception:  # pragma: no cover
    class _OAILikeErr(Exception):
        pass

# Optional: initialize LiteLLM cache automatically when requested via env.
# Keeps caller code minimal: set SCILLM_CACHE=1 and REDIS_* if available.
import os as _os
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler as _AsyncHTTPHandler
# Suppress noisy debug banners by default
try:
    _litellm.suppress_debug_info = True
except Exception:
    pass
try:  # best-effort; never fail import
    if (_os.getenv("SCILLM_CACHE") or "").strip().lower() in {"1", "true", "yes", "on"}:
        from litellm.extras import initialize_litellm_cache  # type: ignore
        initialize_litellm_cache()
except Exception:
    pass

# Optional: force httpx transport (no aiohttp) to avoid rare hangs/unclosed-session warnings
# Set SCILLM_DISABLE_AIOHTTP=1 before importing scillm to apply globally.
try:  # best-effort; never fail import
    if (_os.getenv("SCILLM_DISABLE_AIOHTTP") or "").strip().lower() in {"1", "true", "yes", "on"}:
        # Force httpx everywhere; belt-and-suspenders env for any subpaths
        _os.environ.setdefault("DISABLE_AIOHTTP_TRANSPORT", "True")
        _litellm.disable_aiohttp_transport = True
        # Rebuild the module-level async client with the new transport policy
        _litellm.module_level_aclient = _AsyncHTTPHandler(timeout=_litellm.request_timeout, client_alias="module level aclient")
except Exception:
    pass

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

_orig_completion = _litellm.completion
_orig_acompletion = _litellm.acompletion


class EmptyContentError(APIError):
    def __init__(self, model: str, provider: str):
        super().__init__(502, "Empty response content", provider, model)
        self.reason = "empty_content"
        self.retry_after = None


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


def completion(*args, **kwargs):  # type: ignore[no-redef]
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
            resp = _orig_completion(*args, **kwargs)
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
                resp = _orig_completion(*args, **kwargs_alt)
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

async def acompletion(*args, **kwargs):  # type: ignore[no-redef]
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
            resp = await _orig_acompletion(*args, **kwargs)
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
                resp = await _orig_acompletion(*args, **kwargs_alt)
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
            await asyncio.sleep(empty_backoff_ms / 1000.0)
    if last_exc:
        raise last_exc

# Ensure Router and any code calling litellm.completion/acompletion see the wrapper
try:
    _litellm.completion = completion  # type: ignore[assignment]
    _litellm.acompletion = acompletion  # type: ignore[assignment]
except Exception:
    pass

# Best‑effort cleanup of any lingering aiohttp/httpx clients at interpreter exit
try:
    import atexit, asyncio

    def _run_coro_sync(coro):
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            try:
                fut = asyncio.run_coroutine_threadsafe(coro, loop)
                return fut.result(timeout=5)
            except Exception:
                try:
                    fut.cancel()
                except Exception:
                    pass

        if loop and not loop.is_closed():
            try:
                return loop.run_until_complete(coro)
            except Exception:
                pass

        new_loop = asyncio.new_event_loop()
        try:
            return new_loop.run_until_complete(coro)
        finally:
            new_loop.close()

    def _scillm_cleanup():
        try:
            from litellm.main import base_llm_aiohttp_handler  # type: ignore

            async def _close_aiohttp():
                try:
                    await base_llm_aiohttp_handler.close()  # type: ignore
                except Exception:
                    pass

            _run_coro_sync(_close_aiohttp())
        except Exception:
            pass

        try:
            acl = getattr(_litellm, "module_level_aclient", None)
            if acl is not None and hasattr(acl, "close"):
                async def _close_httpx():
                    try:
                        await acl.close()
                    except Exception:
                        pass

                _run_coro_sync(_close_httpx())
        except Exception:
            pass

    atexit.register(_scillm_cleanup)

    def shutdown_clients():
        """Public entry point to close global HTTP clients immediately."""
        _scillm_cleanup()

    shutdown = shutdown_clients
except Exception:
    pass
