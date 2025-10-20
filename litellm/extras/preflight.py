from __future__ import annotations

import os
import time
import json
import threading
import logging
from pathlib import Path
from typing import Dict, Optional, Set, Any, Tuple

from urllib import request as _urlreq
from urllib.error import HTTPError, URLError

# In-memory, process-scoped model catalog per API base
_CATALOG: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()
_LOG = logging.getLogger(__name__)

# Cache for discovered auth styles per base
# value: {"ts": float, "style": str, "ttl": int}
_AUTH_STYLE: Dict[str, Dict[str, Any]] = {}


def _now() -> float:
    return time.time()


def _normalize_base(api_base: str) -> str:
    base = (api_base or "").strip()
    if base.endswith("/v1"):
        base = base[:-3]
    return base.rstrip("/")


def _bool_env(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() not in ("0", "false", "no", "")


def _cache_path() -> Path:
    # XDG preferred, fallback to ~/.cache/scillm
    root = os.getenv("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(root) / "scillm" / "catalog.json"


def load_catalog() -> None:
    """Load persisted catalog from disk into _CATALOG (best-effort)."""
    if not _bool_env("SCILLM_MODEL_PREFLIGHT_PERSIST", True):
        return
    path = _cache_path()
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            with _LOCK:
                _CATALOG.clear()
                for base, entry in (data or {}).items():
                    if not isinstance(entry, dict):
                        continue
                    ids = set(entry.get("ids", []) or [])
                    ts = float(entry.get("ts", 0.0) or 0.0)
                    ttl = int(entry.get("ttl", 0) or 0)
                    _CATALOG[base] = {"ts": ts, "ids": ids, "ttl": ttl}
    except Exception as e:
        _LOG.debug("SCILLM preflight: failed to load catalog %s: %s", path, e)


def save_catalog() -> None:
    """Persist _CATALOG to disk (best-effort)."""
    if not _bool_env("SCILLM_MODEL_PREFLIGHT_PERSIST", True):
        return
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            serializable = {
                b: {
                    "ts": v.get("ts", 0.0),
                    "ttl": v.get("ttl", 0),
                    "ids": sorted([i for i in v.get("ids", set()) if i]),
                }
                for b, v in _CATALOG.items()
            }
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(serializable, f, separators=(",", ":"), ensure_ascii=False)
        tmp.replace(path)
    except Exception as e:
        _LOG.debug("SCILLM preflight: failed to save catalog %s: %s", path, e)


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
    Persistence:
      When SCILLM_MODEL_PREFLIGHT_PERSIST is truthy (default), the catalog is loaded
      from and saved to ~/.cache/scillm/catalog.json (or $XDG_CACHE_HOME/scillm/catalog.json).
    """
    base = _normalize_base(api_base)
    ttl = int(os.getenv("SCILLM_MODEL_PREFLIGHT_TTL_S", "1800") or "1800")
    if ttl_s is not None:
        ttl = int(ttl_s)

    # serve from non-expired cache
    with _LOCK:
        cur = _CATALOG.get(base)
        if cur and isinstance(cur.get("ts"), (int, float)) and (cur["ts"] + ttl) > _now():
            return set(cur.get("ids", set()))

    headers = {"accept": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    url = base + "/v1/models"
    try:
        req = _urlreq.Request(url=url, headers=headers, method="GET")
        with _urlreq.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Unexpected models payload type: {type(data).__name__}")
        ids = {
            d.get("id")
            for d in (data.get("data") or [])
            if isinstance(d, dict) and d.get("id")
        }
        with _LOCK:
            _CATALOG[base] = {"ts": _now(), "ids": ids, "ttl": ttl}
        save_catalog()
        return set(ids)
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as e:
        if soft:
            # keep existing (possibly empty or expired)
            with _LOCK:
                cached = _CATALOG.get(base)
                return set(cached.get("ids", set())) if cached else set()
        # Re-raise with more context for diagnostics
        raise RuntimeError(f"Failed to preflight models from {url}: {e}") from e


def catalog_for(api_base: str) -> Set[str]:
    base = _normalize_base(api_base)
    with _LOCK:
        cur = _CATALOG.get(base)
        return set(cur.get("ids", set())) if cur else set()


def check_model_available(api_base: str, model: str, *, soft: bool = False) -> bool:
    base = _normalize_base(api_base)
    with _LOCK:
        cur = _CATALOG.get(base)
        ids = set(cur.get("ids", set())) if cur else set()
    ok = model in ids
    if not ok and not soft:
        raise ValueError(
            f"Unknown model '{model}' for base '{base}'. Run preflight_models() or disable SCILLM_MODEL_PREFLIGHT."
        )
    return ok

# Load persisted catalog on import (best-effort)
try:
    load_catalog()
except Exception:
    # Already logged at debug level inside load_catalog
    pass


def _is_openai_host(base: str) -> bool:
    b = _normalize_base(base).lower()
    return (
        b.endswith("api.openai.com")
        or b.endswith("api.openai.com:443")
        or b.endswith("api.openai.com:80")
    )


def discover_auth_style(
    *,
    api_base: str,
    api_key: Optional[str],
    prefer: Optional[str] = None,
    timeout_s: float = 5.0,
    ttl_s: int = 7200,
    soft: bool = True,
) -> str:
    """Detect which auth header style a base accepts for OpenAI‑compatible APIs.

    Tries in order (unless `prefer` narrows the attempt):
      1) bearer  → Authorization: Bearer <key>
      2) x-api-key → x-api-key: <key>
      3) raw    → Authorization: <key>

    Returns: 'bearer' | 'x-api-key' | 'raw' | 'none'
    Caches result per base for `ttl_s` seconds.
    """
    base = _normalize_base(api_base)
    if not base:
        return "none"
    if _is_openai_host(base):
        return "bearer"

    # serve from non-expired cache
    with _LOCK:
        cur = _AUTH_STYLE.get(base)
        if cur and isinstance(cur.get("ts"), (int, float)) and (cur["ts"] + ttl_s) > _now():
            return str(cur.get("style") or "none")

    if not api_key:
        # No key to probe with; default conservative
        return "bearer"

    def _try(style: str) -> bool:
        headers = {"accept": "application/json"}
        if style == "bearer":
            headers["authorization"] = f"Bearer {api_key}"
        elif style == "x-api-key":
            headers["x-api-key"] = api_key
        elif style == "raw":
            headers["authorization"] = api_key
        else:
            return False
        url = base + "/v1/models"
        try:
            req = _urlreq.Request(url=url, headers=headers, method="GET")
            with _urlreq.urlopen(req, timeout=timeout_s) as resp:
                # any 2xx implies auth accepted
                code = getattr(resp, "status", getattr(resp, "code", 200))
                return 200 <= int(code) < 300
        except HTTPError as e:
            # 401/403 means wrong auth style; other 2xx/5xx we treat as not-accepted
            if int(getattr(e, "code", 0)) in (401, 403):
                return False
            # Other errors: treat as failure for this style
            return False
        except Exception:
            return False

    order = ["bearer", "x-api-key", "raw"]
    if prefer in order:
        order = [prefer] + [s for s in order if s != prefer]

    chosen = "none"
    for style in order:
        if _try(style):
            chosen = style
            break

    # cache result (even 'none' to avoid re-probing hot)
    with _LOCK:
        _AUTH_STYLE[base] = {"ts": _now(), "style": chosen, "ttl": int(ttl_s)}

    if chosen == "none" and not soft:
        raise RuntimeError(f"Could not determine auth style for {base}")
    return chosen
