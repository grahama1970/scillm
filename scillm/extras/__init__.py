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
# Re-export common JSON helpers from our canonical module
try:
    from .json_utils import (  # type: ignore
        clean_json_string,
        parse_json,
        save_json_to_file,
        load_json_file,
        json_serialize,
        PathEncoder,
    )
except Exception:
    pass
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
from .json_utils import *  # re-export cleaners
from .json_chat import (
    json_chat,
    strict_json_chat,
    strict_json_completion,  # preferred name
)
from .utilization_ranker import *
from .model_selector import *
from .fallback_infer import *
from .auto_router import *
from .attribution import *
from .auth_policy import *
from .hedged import *
from .preflight import *
from .hedged_json_chat import hedged_json_chat, hedged_json_completion
