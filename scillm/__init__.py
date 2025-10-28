# Re-export litellm surface for convenience when installed as 'scillm'.
from litellm import *  # noqa: F401,F403

# Optional: initialize LiteLLM cache automatically when requested via env.
# Keeps caller code minimal: set SCILLM_CACHE=1 and REDIS_* if available.
import os as _os
import litellm as _litellm
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
        _litellm.disable_aiohttp_transport = True
        # Rebuild the module-level async client with the new transport policy
        _litellm.module_level_aclient = _AsyncHTTPHandler(timeout=_litellm.request_timeout, client_alias="module level aclient")
except Exception:
    pass
