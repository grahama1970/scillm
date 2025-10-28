import os
import time
from typing import Dict, List, Optional, Set, Tuple

import httpx


# TTLs and thresholds are configurable via env without changing call sites
_TTL_S = int(os.getenv("SCILLM_UTIL_TTL_S", "45"))
_UTIL_HI = float(os.getenv("SCILLM_UTIL_HI", "0.85"))
_UTIL_LO = float(os.getenv("SCILLM_UTIL_LO", "0.50"))

_models_cache: Dict[str, Tuple[float, Set[str]]] = {}
_util_cache: Dict[str, Tuple[float, Optional[float]]] = {}
_tier_memory: Dict[str, Tuple[int, int]] = {}  # base -> (last_tier, consecutive_count)
_CONSISTENT_K = int(os.getenv("SCILLM_UTIL_K", "2"))


def _now() -> float:
    return time.time()


def _cached_get(cache: Dict, key: str, ttl_s: int) -> Optional[Tuple[float, object]]:
    entry = cache.get(key)
    if not entry:
        return None
    ts, value = entry
    if _now() - ts < ttl_s:
        return entry
    return None


def _get_models(api_base: str, headers: Dict[str, str]) -> Set[str]:
    base = (api_base or "").rstrip("/")
    cached = _cached_get(_models_cache, base, _TTL_S)
    if cached:
        return cached[1]  # type: ignore[return-value]
    ids: Set[str] = set()
    url = f"{base}/models"
    with httpx.Client(timeout=10) as cx:
        r = cx.get(url, headers=headers)
        r.raise_for_status()
        js = r.json() or {}
        data = js.get("data") or []
        for m in data:
            if isinstance(m, dict):
                mid = m.get("id")
                if isinstance(mid, str):
                    ids.add(mid)
    _models_cache[base] = (_now(), ids)
    return ids


def _get_utilization(api_base: str, headers: Dict[str, str]) -> Optional[float]:
    base = (api_base or "").rstrip("/")
    cached = _cached_get(_util_cache, base, _TTL_S)
    if cached:
        return cached[1]  # type: ignore[return-value]
    url_candidates = (
        f"{base}/chutes/utilization",
        f"{base}/chutes/utilization_legacy",
    )
    util: Optional[float] = None
    for url in url_candidates:
        try:
            with httpx.Client(timeout=5) as cx:
                r = cx.get(url, headers=headers)
                if r.status_code != 200:
                    continue
                js = r.json() or {}
                raw = js.get("utilization")
                if raw is None:
                    continue
                util_val = float(raw)
                if 0.0 <= util_val <= 1.0:
                    util = util_val
                    break
        except Exception:
            # Treat errors as unknown utilization
            util = None
            break
    _util_cache[base] = (_now(), util)
    return util


def rank_chutes_by_availability_and_utilization(model_list: List[Dict], *, target_model_env: Optional[str] = None) -> List[Dict]:
    """
    Returns a new model_list sorted by:
      1) Availability: /v1/models contains the target model id → preferred tier
      2) Utilization (optional soft signal): lower utilization first

    Inputs follow Router model_list shape, where each entry is:
      {"model_name": str, "litellm_params": {"api_base": str, "extra_headers": {..}, "model": str, ...}}

    target_model_env: when provided, acts as a fallback source for the model id
                      if not present in the entry's litellm_params.
    """

    def score(entry: Dict) -> Tuple[int, float]:
        p = entry.get("litellm_params", {}) or {}
        base = (p.get("api_base") or "").strip()
        headers = (p.get("extra_headers") or {})
        target_model = (p.get("model") or (os.getenv(target_model_env) if target_model_env else "")) or ""

        # 1) Availability gate via /v1/models
        present = False
        try:
            present = bool(target_model) and (target_model in _get_models(base, headers))
        except Exception:
            present = False

        # Tier: 0 = preferred, 1 = mid, 2 = avoid/last
        tier = 0 if present else 2

        # 2) Utilization as soft ordering signal within tier
        util = None
        try:
            util = _get_utilization(base, headers)
        except Exception:
            util = None

        if util is None:
            # unknown utilization; keep neutral position
            return (1 if tier != 0 else 0, 0.0)

        # Prefer lower utilization (higher score)
        # Nudge tiers based on thresholds, but never override availability gate
        proposed = tier
        if util >= _UTIL_HI:
            proposed = max(tier, 2)
        elif util <= _UTIL_LO:
            proposed = min(tier, 0)
        # Hysteresis: only change tier after K consistent observations
        last, count = _tier_memory.get(base, (proposed, 0))
        if proposed == last:
            count = min(count + 1, _CONSISTENT_K)
        else:
            count = 1
            last = proposed
        _tier_memory[base] = (last, count)
        if count < _CONSISTENT_K:
            # Not yet stable; keep current availability tier
            return (tier, 1.0 - util)
        tier = last

        return (tier, 1.0 - util)

    return sorted(model_list, key=score)


__all__ = [
    "rank_chutes_by_availability_and_utilization",
]
