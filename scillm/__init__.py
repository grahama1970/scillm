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
    try:
        resp = _orig_completion(*args, **kwargs)
        return _sc_postprocess_require_nonempty(resp)
    except (_AuthErr, _OAILikeErr) as e:
        msg = str(getattr(e, "message", e))
        if "401" in msg or "Unauthorized" in msg or "invalid auth" in msg.lower():
            # Retry with alternate header style and cache winner
            token = (headers or {}).get("Authorization") or (headers or {}).get("x-api-key") or api_key or ""
            token = str(token)
            if token.lower().startswith("bearer "):
                token = token.split(" ",1)[-1]
            alt_headers = dict(headers or {})
            # Flip to x-api-key
            alt_headers.pop("Authorization", None); alt_headers.pop("authorization", None)
            alt_headers["x-api-key"] = token
            # Bypass canon for retry to avoid converting back
            kwargs_alt = dict(kwargs)
            kwargs_alt["extra_headers"] = alt_headers
            kwargs_alt["_sc_no_canon"] = True
            resp = _orig_completion(*args, **kwargs_alt)
            if _sc_is_chutes_base(api_base):
                _SC_WINNERS[str(api_base)] = ("x-api-key", _time.time() + 300.0)
            return _sc_postprocess_require_nonempty(resp)
        raise

async def acompletion(*args, **kwargs):  # type: ignore[no-redef]
    api_base = kwargs.get("api_base")
    api_key = kwargs.get("api_key")
    headers = kwargs.get("extra_headers")
    api_key, headers = _sc_canon_headers_for_chutes(api_base, api_key, headers)
    if headers is not None:
        kwargs["extra_headers"] = headers
    try:
        resp = await _orig_acompletion(*args, **kwargs)
        return _sc_postprocess_require_nonempty(resp)
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
            return _sc_postprocess_require_nonempty(resp)
        raise

# Ensure Router and any code calling litellm.completion/acompletion see the wrapper
try:
    _litellm.completion = completion  # type: ignore[assignment]
    _litellm.acompletion = acompletion  # type: ignore[assignment]
except Exception:
    pass

# Best‑effort cleanup of any lingering aiohttp/httpx clients at interpreter exit
try:
    import atexit, asyncio

    def _scillm_cleanup():
        # Close aiohttp base handler if present
        try:
            from litellm.main import base_llm_aiohttp_handler  # type: ignore

            async def _close_aiohttp():
                try:
                    await base_llm_aiohttp_handler.close()  # type: ignore
                except Exception:
                    pass

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(_close_aiohttp())
                else:
                    loop.run_until_complete(_close_aiohttp())
            except Exception:
                try:
                    asyncio.run(_close_aiohttp())
                except Exception:
                    pass
        except Exception:
            pass
        # Close module-level httpx async client if present
        try:
            acl = getattr(_litellm, "module_level_aclient", None)
            if acl is not None and hasattr(acl, "close"):
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        loop.create_task(acl.close())
                    else:
                        loop.run_until_complete(acl.close())
                except Exception:
                    try:
                        asyncio.run(acl.close())
                    except Exception:
                        pass
        except Exception:
            pass

    atexit.register(_scillm_cleanup)
except Exception:
    pass
