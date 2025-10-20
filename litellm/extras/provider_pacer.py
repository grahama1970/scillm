from __future__ import annotations

import asyncio
import os
import time
from typing import Dict, Optional

# Lightweight, process-local pacing helpers for OpenAI-compatible providers (e.g., Chutes).
# - Token-bucket style QPS pacing
# - Optional global cool-down window after a 429 (env-tunable)
#
# Usage (inside async call path):
#   await throttle_async(base_url)
# Usage (sync path):
#   throttle_sync(base_url)


_STATE: Dict[str, Dict[str, float]] = {}
_LOCK = asyncio.Lock()


def _norm_base(base: Optional[str]) -> Optional[str]:
    if not base:
        return None
    b = base.strip()
    if b.endswith("/v1"):
        b = b[:-3]
    return b.rstrip("/")


def _enabled_for(base: Optional[str]) -> bool:
    # Enable only for Chutes/OpenAI-compatible gateway context
    if os.getenv("SCILLM_CHUTES_MODE", "0").lower() in {"1", "true", "yes"}:
        return True
    chutes = os.getenv("CHUTES_API_BASE")
    b = (base or "").lower()
    if chutes and chutes.rstrip("/").lower().startswith(b):
        return True
    if "chutes.ai" in b:
        return True
    return False


def _qps() -> float:
    try:
        return float(os.getenv("SCILLM_RATE_LIMIT_QPS", "2.5"))
    except Exception:
        return 2.5


def _cooldown_s() -> float:
    try:
        return float(os.getenv("SCILLM_COOLDOWN_429_S", "120"))
    except Exception:
        return 120.0


async def throttle_async(base: Optional[str]) -> None:
    base_n = _norm_base(base)
    if not _enabled_for(base_n):
        return
    rate = _qps()
    if rate <= 0:
        return
    interval = 1.0 / rate
    now = time.monotonic()
    async with _LOCK:
        st = _STATE.setdefault(base_n or "default", {"next": 0.0, "cool_until": 0.0})
        # honor cool-down
        if now < st["cool_until"]:
            await asyncio.sleep(max(0.0, st["cool_until"] - now))
            now = time.monotonic()
        wait = max(0.0, st["next"] - now)
        if wait > 0:
            # Sleep outside the lock to avoid head-of-line blocking
            pass
    if wait > 0:
        await asyncio.sleep(wait)
        now = time.monotonic()
    # set next scheduled slot
    async with _LOCK:
        st = _STATE.setdefault(base_n or "default", {"next": 0.0, "cool_until": 0.0})
        st["next"] = max(now, st.get("next", 0.0)) + interval


def throttle_sync(base: Optional[str]) -> None:
    base_n = _norm_base(base)
    if not _enabled_for(base_n):
        return
    rate = _qps()
    if rate <= 0:
        return
    interval = 1.0 / rate
    now = time.monotonic()
    # Use a poor-man's lock by piggybacking on asyncio one when possible
    # For simplicity in sync path, we accept minor race conditions — pacing still smooths bursts.
    st = _STATE.setdefault(base_n or "default", {"next": 0.0, "cool_until": 0.0})
    if now < st["cool_until"]:
        sleep_s = max(0.0, st["cool_until"] - now)
        if sleep_s:
            import time as _t
            _t.sleep(sleep_s)
            now = time.monotonic()
    wait = max(0.0, st["next"] - now)
    if wait > 0:
        import time as _t
        _t.sleep(wait)
        now = time.monotonic()
    st["next"] = max(now, st.get("next", 0.0)) + interval


def note_429(base: Optional[str]) -> None:
    """Record a 429 event to enforce a global cool-down window for this base."""
    base_n = _norm_base(base)
    cool = _cooldown_s()
    now = time.monotonic()
    st = _STATE.setdefault(base_n or "default", {"next": 0.0, "cool_until": 0.0})
    st["cool_until"] = max(st.get("cool_until", 0.0), now + cool)
