from __future__ import annotations

import os
import threading
import time

_lock = threading.Lock()
_last_call_ts = 0.0
_cooldown_until = 0.0


def _get_qps() -> float:
    try:
        v = os.getenv("SCILLM_RATE_LIMIT_QPS")
        return float(v) if v else 0.0
    except Exception:
        return 0.0


def _get_cooldown_secs() -> float:
    try:
        v = os.getenv("SCILLM_COOLDOWN_429_S")
        return float(v) if v else 0.0
    except Exception:
        return 0.0


def wait_if_needed() -> float:
    """Enforce simple process-wide QPS and cooldown. Returns slept seconds."""
    slept = 0.0
    qps = _get_qps()
    min_interval = (1.0 / qps) if qps and qps > 0 else 0.0

    with _lock:
        now = time.time()
        # Cooldown takes precedence
        if _cooldown_until > now:
            to_sleep = _cooldown_until - now
            if to_sleep > 0:
                time.sleep(to_sleep)
                slept += to_sleep
                now = time.time()
        # QPS pacing
        if min_interval > 0:
            elapsed = now - _last_call_ts
            if elapsed < min_interval:
                to_sleep = min_interval - elapsed
                time.sleep(to_sleep)
                slept += to_sleep
                now = time.time()
        # Mark last call
        globals()['_last_call_ts'] = now
    return slept


def note_429_capacity() -> None:
    """Start a cooldown window after a 429/capacity event."""
    cd = _get_cooldown_secs()
    if cd <= 0:
        return
    with _lock:
        globals()['_cooldown_until'] = max(time.time() + cd, globals().get('_cooldown_until', 0.0))

