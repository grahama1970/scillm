from __future__ import annotations

"""Shutdown helpers for paved-path users.

This module keeps side effects out of scillm.paved.__init__ while providing
explicit, opt-in helpers for disabling background logging and closing shared
clients.
"""

import os
from typing import Optional


def maybe_disable_paved_logging_from_env() -> bool:
    """Disable litellm/scillm async logging if env opts are set.

    Honors any of these flags (case-insensitive):
      - SCILLM_PAVED_DISABLE_LOGGING=1/true/yes/on
      - LITELLM_LOGGING=0
      - SPARTA_LITELLM_DISABLE_BG=1/true/yes/on

    Returns True if a disable was attempted; False otherwise.
    """

    flags = {
        os.getenv("SCILLM_PAVED_DISABLE_LOGGING", "0").lower(),
        os.getenv("SPARTA_LITELLM_DISABLE_BG", "0").lower(),
    }
    litellm_disabled = os.getenv("LITELLM_LOGGING", "").strip() == "0"
    should_disable = litellm_disabled or any(v in {"1", "true", "yes", "on"} for v in flags)
    if not should_disable:
        return False
    try:
        from scillm import disable_background_services  # type: ignore

        disable_background_services()
    except Exception:
        pass
    return True


def shutdown() -> bool:
    """Explicit paved-path shutdown helper.

    Actions:
    - Disables litellm/scillm async logging queue (idempotent).
    - Closes shared httpx/aiohttp clients used by scillm/litellm.

    Safe to call multiple times (e.g., at process exit or after a batch).
    """

    try:
        from scillm import disable_background_services as _disable_bg_services  # type: ignore

        _disable_bg_services()
    except Exception:
        pass
    try:
        from scillm import shutdown as _shutdown  # type: ignore

        _shutdown()
    except Exception:
        pass
    return True

