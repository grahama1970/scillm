from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple

import httpx

_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _now() -> float:
    import time

    return time.time()


def _ttl_get(key: str) -> Optional[Dict[str, Any]]:
    ent = _CACHE.get(key)
    if not ent:
        return None
    exp, val = ent
    if exp > _now():
        return val
    return None


def _ttl_put(key: str, val: Dict[str, Any], ttl_s: int) -> None:
    _CACHE[key] = (_now() + max(5, ttl_s), val)


def _from_env() -> Optional[Dict[str, Any]]:
    raw = os.getenv("CHUTES_PRICING_JSON", "").strip()
    if not raw:
        # Global per‑1k fallback
        pp = os.getenv("CHUTES_PRICE_PROMPT_USD_PERK")
        cp = os.getenv("CHUTES_PRICE_COMPLETION_USD_PERK") or os.getenv("CHUTES_PRICE_OUTPUT_USD_PERK")
        if pp or cp:
            try:
                return {"__global__": {
                    "prompt_per_1k": float(pp) if pp else None,
                    "completion_per_1k": float(cp) if cp else None,
                }}
            except Exception:
                return None
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _from_file() -> Optional[Dict[str, Any]]:
    path = os.getenv("CHUTES_PRICING_FILE", "").strip()
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _from_api(ttl_s: int = 3600) -> Optional[Dict[str, Any]]:
    # Disabled by default per Chutes support (no public pricing API).
    # Enable only if explicitly allowed for future compatibility.
    if (os.getenv("CHUTES_PRICING_ALLOW_API", "0").strip().lower() not in {"1", "true", "yes"}):
        return None
    base = os.getenv("CHUTES_API_BASE", "").rstrip("/")
    key = os.getenv("CHUTES_API_KEY", "").strip()
    if not base or not key:
        return None
    ck = f"pricing:{base}"
    cached = _ttl_get(ck)
    if cached:
        return cached
    hdr = {"Authorization": f"Bearer {key}"}
    # Try OpenAI‑like /models route first; fall back to /pricing if present
    try:
        with httpx.Client(timeout=10.0) as cx:
            r = cx.get(f"{base}/models", headers=hdr)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
                j = r.json()
                out: Dict[str, Any] = {}
                for item in (j.get("data") or []):
                    mid = item.get("id") or item.get("model")
                    if not mid:
                        continue
                    pr = item.get("pricing") or {}
                    prompt = pr.get("prompt_per_1k") or pr.get("prompt_1k_usd") or pr.get("input_per_1k_usd")
                    comp = pr.get("completion_per_1k") or pr.get("completion_1k_usd") or pr.get("output_per_1k_usd")
                    try:
                        out[mid] = {
                            "prompt_per_1k": (float(prompt) if prompt is not None else None),
                            "completion_per_1k": (float(comp) if comp is not None else None),
                        }
                    except Exception:
                        continue
                if out:
                    _ttl_put(ck, out, ttl_s)
                    return out
    except Exception:
        pass
    # Optional explicit /pricing endpoint
    try:
        with httpx.Client(timeout=10.0) as cx:
            r = cx.get(f"{base}/pricing", headers=hdr)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
                j = r.json()
                if isinstance(j, dict):
                    _ttl_put(ck, j, ttl_s)
                    return j
    except Exception:
        pass
    return None


def get_pricing(ttl_s: int = 3600) -> Dict[str, Any]:
    env = _from_env()
    if env:
        return env
    f = _from_file()
    if f:
        return f
    api = _from_api(ttl_s=ttl_s)
    return api or {}


def get_model_pricing(model_id: str) -> Tuple[Optional[float], Optional[float]]:
    pr = get_pricing()
    if model_id in pr:
        p = pr[model_id] or {}
        return p.get("prompt_per_1k"), p.get("completion_per_1k")
    g = pr.get("__global__") or {}
    return g.get("prompt_per_1k"), g.get("completion_per_1k")


def estimate_cost_usd(model_id: str, prompt_tokens: Optional[int], completion_tokens: Optional[int]) -> Optional[float]:
    pp, cp = get_model_pricing(model_id)
    if pp is None and cp is None:
        return None
    pt = max(0, int(prompt_tokens or 0))
    ct = max(0, int(completion_tokens or 0))
    cost = 0.0
    if pp is not None:
        cost += (pt / 1000.0) * float(pp)
    if cp is not None:
        cost += (ct / 1000.0) * float(cp)
    return round(cost, 6)
