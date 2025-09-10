"""
Central registry for experimental / soft‑launch feature flags.

All flags default to 'off' so new features are inert unless explicitly enabled
via environment variable. This lets us merge experimental surfaces safely.

Usage elsewhere:
    from litellm.experimental_flags import ENABLE_PARALLEL_ACOMPLETIONS
    if ENABLE_PARALLEL_ACOMPLETIONS:
        ...
"""

import os

ENABLE_PARALLEL_ACOMPLETIONS: bool = os.getenv(
    "LITELLM_ENABLE_PARALLEL_ACOMPLETIONS", "0"
).lower() in ("1", "true", "yes")
