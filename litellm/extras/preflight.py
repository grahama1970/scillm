from __future__ import annotations

import os
import time
from typing import Dict, Optional, Set, Any

import json
from urllib import request as _urlreq

# In-memory, process-scoped model catalog per API base
_CATALOG: Dict[str, Dict[str, object]] = {}
_CACHE_PATH = os.path.expanduser("~/.cache/scillm/catalog.json")


def _now() -> float:
    return time.time()


def _normalize_base(api_base: str) -> str:
    base = (api_base or "").strip()
    if base.endswith("/v1"):
        base = base[:-3]
    return base.rstrip("/")


def preflight_models(
    *,
    api_base: str,
    api_key: Optional[str] = None,
    ttl_s: Optional[int] = None,
    soft: bool = False,
    timeout_s: float = 10.0,
) -> Set[str]:
    """Fetch and cache /v1/models once per session (with optional TTL).

    - api_base: OpenAI-compatible base (with or without /v1); trailing /v1 is stripped.
    - api_key: optional; sent as Bearer if provided.
    - ttl_s: seconds before refresh; defaults from SCILLM_MODEL_PREFLIGHT_TTL_S or 1800s.
    - soft: if True, swallow failures and return any existing cache (may be empty).
    - timeout_s: HTTP timeout for the fetch.
    """
    base = _normalize_base(api_base)
    ttl = int(os.getenv("SCILLM_MODEL_PREFLIGHT_TTL_S", "1800") or "1800")
    if ttl_s is not None:
        ttl = int(ttl_s)

    # serve from non-expired cache
    cur = _CATALOG.get(base)
    if cur and isinstance(cur.get("ts"), (int, float)) and (cur["ts"] + ttl) > _now():
        return set(cur.get("ids", set()))  # type: ignore

    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    url = base + "/v1/models"
    try:
        req = _urlreq.Request(url=url, headers=headers, method="GET")
        with _urlreq.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ids = {d.get("id") for d in (data.get("data") or []) if isinstance(d, dict) and d.get("id")}
        _CATALOG[base] = {"ts": _now(), "ids": ids, "ttl": ttl}
        try:
            _persist_catalog(base, _CATALOG[base])
        except Exception:
            pass
        return set(ids)
    except Exception:
        if soft:
            # keep existing (possibly empty or expired)
            if cur:
                return set(cur.get("ids", set()))
            snap = _load_persisted_catalog(base)
            if snap:
                _CATALOG[base] = snap
                return set(snap.get("ids", set()))
            return set()
        raise


def catalog_for(api_base: str) -> Set[str]:
    base = _normalize_base(api_base)
    cur = _CATALOG.get(base)
    if not cur:
        snap = _load_persisted_catalog(base)
        if snap:
            _CATALOG[base] = snap
            cur = snap
    return set(cur.get("ids", set())) if cur else set()


def check_model_available(api_base: str, model: str, *, soft: bool = False) -> bool:
    base = _normalize_base(api_base)
    cur = _CATALOG.get(base)
    ids = set(cur.get("ids", set())) if cur else set()
    ok = model in ids
    if not ok and not soft:
        raise ValueError(f"Unknown model '{model}' for base '{base}'. Run preflight_models() or disable SCILLM_MODEL_PREFLIGHT.")
    return ok


def _persist_catalog(base: str, snap: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
    data: Dict[str, Any] = {}
    if os.path.exists(_CACHE_PATH):
        try:
            with open(_CACHE_PATH, 'r', encoding='utf-8') as r:
                data = json.load(r)
        except Exception:
            data = {}
    data[base] = snap
    with open(_CACHE_PATH, 'w', encoding='utf-8') as w:
        json.dump(data, w)


def _load_persisted_catalog(base: str) -> Optional[Dict[str, Any]]:
    try:
        with open(_CACHE_PATH, 'r', encoding='utf-8') as r:
            data = json.load(r)
        snap = data.get(base)
        if isinstance(snap, dict) and snap.get('ids'):
            return snap
    except Exception:
        return None
    return None
