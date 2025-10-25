"""
SciLLM extras namespace.

Re-exports helpers from litellm.extras for convenience, so callers can use
`from scillm.extras.multi_agents import ...`.
Also sets safe defaults for this stack (drop unsupported params unless strict).
"""

import os
import litellm  # type: ignore

# Default: drop unsupported params to avoid provider hard-failures.
_strict = os.getenv("SCILLM_STRICT", "").lower() in {"1", "true", "yes"}
if not _strict and not getattr(litellm, "drop_params", False):
    litellm.drop_params = True  # type: ignore[attr-defined]
    if os.getenv("SCILLM_DEBUG"):
        try:
            print("[scillm][debug] litellm.drop_params=True (default)")
        except Exception:
            pass

from .multi_agents import *  # noqa: F401,F403
from litellm.extras.codex_bootstrap import *  # re-export ensure_codex_agent
# Optional: codex-cloud helpers (disabled by default to avoid import-time failures)
try:
    if os.getenv("SCILLM_ENABLE_CODEX_CLOUD", "").lower() in {"1", "true", "yes"}:
        from .codex_cloud import generate_variants_cloud, variants_to_scillm  # type: ignore # noqa: F401
except Exception:
    # Keep extras import resilient
    pass

# Optional: register chutes provider when explicitly enabled (avoids surprise costs)
try:
    if os.getenv("SCILLM_ENABLE_CHUTES_AUTOSTART", "").lower() in {"1", "true", "yes"}:
        import litellm.llms.chutes  # noqa: F401
except Exception:
    pass
