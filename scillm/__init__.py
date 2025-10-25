# Re-export litellm surface for convenience when installed as 'scillm'.
from litellm import *  # noqa: F401,F403

# Optional: initialize LiteLLM cache automatically when requested via env.
# Keeps caller code minimal: set SCILLM_CACHE=1 and REDIS_* if available.
import os as _os
try:  # best-effort; never fail import
    if (_os.getenv("SCILLM_CACHE") or "").strip().lower() in {"1", "true", "yes", "on"}:
        from litellm.extras import initialize_litellm_cache  # type: ignore
        initialize_litellm_cache()
except Exception:
    pass
