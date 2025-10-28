from __future__ import annotations

import json
import time
from typing import Dict, Iterable, Tuple

import httpx

_CACHE: Dict[str, float] = {}


def warm_chutes_caches(entries: Iterable[Dict], ttl_s: int = 300) -> Tuple[int, int]:
    """
    Best-effort: for each Router model_list entry, hit /v1/models (and utilization) to warm caches.
    Returns (models_ok, util_ok) counts.
    """
    m_ok = u_ok = 0
    for e in entries:
        p = e.get("litellm_params", {}) or {}
        base = (p.get("api_base") or "").rstrip("/")
        hdrs = (p.get("extra_headers") or {})
        if not base:
            continue
        if _CACHE.get(base, 0) > time.time():
            continue
        try:
            with httpx.Client(timeout=8) as cx:
                r = cx.get(f"{base}/models", headers=hdrs)
                if r.status_code == 200:
                    m_ok += 1
                try:
                    r2 = cx.get(f"{base}/chutes/utilization", headers=hdrs)
                    if r2.status_code == 200:
                        u_ok += 1
                except Exception:
                    pass
        except Exception:
            pass
        _CACHE[base] = time.time() + ttl_s
    return m_ok, u_ok


__all__ = ["warm_chutes_caches"]

