from __future__ import annotations

import datetime as _dt
import os
from typing import Any, Dict, Optional, Tuple

import httpx

# Simple in-process cache to avoid hammering usage endpoints
_USAGE_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


class _ChutesBudgetTracker:
    """Process-local tracker of attempts within the current reset window.

    Tracks per (api_base, api_key_hash) window: base_used (from preflight),
    local_attempts (incremented per outbound call), daily_limit, and reset_at.
    """

    def __init__(self) -> None:
        self._state: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _key(api_base: str, api_key: str) -> str:
        import hashlib

        sig = f"{api_base}::{api_key}".encode()
        return hashlib.sha256(sig).hexdigest()[:16]

    def _ensure(self, api_base: str, api_key: str) -> Dict[str, Any]:
        k = self._key(api_base, api_key)
        if k not in self._state:
            self._state[k] = {
                "base_used": 0,
                "local_attempts": 0,
                "limit": _get_daily_limit(),
                "reset_at": _next_reset_at_iso_utc(),
            }
        return self._state[k]

    def init_from_preflight(self, *, api_base: str, api_key: str, used: int, limit: int) -> None:
        st = self._ensure(api_base, api_key)
        st["base_used"] = max(0, int(used))
        st["limit"] = max(1, int(limit))
        st["reset_at"] = _next_reset_at_iso_utc()
        # Keep local_attempts; do not zero it to avoid double-counting mid-run

    def update_reset_at(self, *, api_base: str, api_key: str, reset_at_iso: str) -> None:
        st = self._ensure(api_base, api_key)
        st["reset_at"] = reset_at_iso

    def register_attempt(self, *, api_base: str, api_key: str) -> Dict[str, Any]:
        st = self._ensure(api_base, api_key)
        # If reset window passed, roll to new window
        try:
            s = st.get("reset_at")
            if isinstance(s, str):
                t = s[:-1] + "+00:00" if s.endswith("Z") else s
                tgt = _dt.datetime.fromisoformat(t)
                if tgt.tzinfo is None:
                    tgt = tgt.replace(tzinfo=_dt.timezone.utc)
                if _dt.datetime.now(tz=_dt.timezone.utc) >= tgt:
                    st["base_used"] = 0
                    st["local_attempts"] = 0
                    st["reset_at"] = _next_reset_at_iso_utc()
        except Exception:
            pass
        st["local_attempts"] = int(st.get("local_attempts", 0)) + 1
        return st.copy()

    def snapshot(self, *, api_base: str, api_key: str) -> Dict[str, Any]:
        st = self._ensure(api_base, api_key)
        used = int(st.get("base_used", 0)) + int(st.get("local_attempts", 0))
        limit = int(st.get("limit", _get_daily_limit()))
        remaining = max(0, limit - used)
        return {
            "used": used,
            "limit": limit,
            "remaining": remaining,
            "reset_at": st.get("reset_at"),
            "local_attempts": int(st.get("local_attempts", 0)),
            "base_used": int(st.get("base_used", 0)),
        }


_BUDGET = _ChutesBudgetTracker()


def _env_flag(name: str, default: str = "0") -> bool:
    v = os.getenv(name, default).strip().lower()
    return v in {"1", "true", "yes", "y"}


def _get_daily_limit() -> int:
    """Return daily call limit.

    Default is 999999 (effectively unlimited) because Chutes PAYG continues
    serving past the subscription quota as long as the account has balance.
    Set CHUTES_DAILY_LIMIT explicitly to enforce a hard cap.
    """
    try:
        return int(os.getenv("CHUTES_DAILY_LIMIT", "999999"))
    except Exception:
        return 999999


def _next_reset_at_iso_utc() -> str:
    now = _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc)
    # Allow optional override of reset hour/minute in UTC
    try:
        hour = int(os.getenv("CHUTES_RESET_UTC_HOUR", "0"))
        minute = int(os.getenv("CHUTES_RESET_UTC_MINUTE", "0"))
    except Exception:
        hour, minute = 0, 0
    reset_time_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now >= reset_time_today:
        reset_time_today += _dt.timedelta(days=1)
    return reset_time_today.isoformat().replace("+00:00", "Z")


def _retry_after_seconds_from_headers(headers: Optional[Dict[str, str]]) -> Optional[int]:
    if not headers:
        return None
    ra = headers.get("Retry-After") or headers.get("retry-after")
    if ra:
        try:
            return max(0, int(float(ra)))
        except Exception:
            return None
    return None


def preflight_usage(ttl_s: int = 90) -> Optional[Dict[str, Any]]:
    """
    Optional preflight probe for current-day usage. Returns a structured dict or None on failure/disabled.

    Requires CHUTES_PREFLIGHT=1, CHUTES_API_BASE, CHUTES_API_KEY.
    """
    if not _env_flag("CHUTES_PREFLIGHT", "0"):
        return None
    api_base = (os.getenv("CHUTES_API_BASE", "").strip("/"))
    api_key = os.getenv("CHUTES_API_KEY", "").strip()
    if not api_base or not api_key:
        return None

    cache_key = f"{api_base}:/users/me/usage"
    now = _dt.datetime.utcnow().timestamp()
    cached = _USAGE_CACHE.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]

    today = _dt.datetime.utcnow().date().isoformat()
    url = f"{api_base}/users/me/usage"
    hdrs = {"Authorization": f"Bearer {api_key}"}
    params = {"start_date": today, "end_date": today, "per_chute": "true"}
    try:
        with httpx.Client(timeout=10.0) as cx:
            r = cx.get(url, headers=hdrs, params=params)
            if r.status_code != 200:
                return None
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            # Heuristic extraction: prefer explicit fields, fallback to common aliases
            used = (
                data.get("total")
                or data.get("count")
                or (data.get("summary") or {}).get("calls")
                or 0
            )
            try:
                used = int(used)
            except Exception:
                used = 0
            # Extract cost if available (PAYG visibility)
            cost = None
            try:
                # common fields: cost_usd, usd, total_usd, spend_usd
                for k in ("cost_usd", "usd", "total_usd", "spend_usd"):
                    v = (data.get("summary") or {}).get(k)
                    if v is None:
                        v = data.get(k)
                    if v is not None:
                        cost = float(v)
                        break
            except Exception:
                cost = None
            limit = _get_daily_limit()
            remaining = max(0, limit - used)
            out = {"ok": True, "used": used, "limit": limit, "remaining": remaining}
            if cost is not None:
                out["cost_usd_today"] = float(cost)
            # Seed tracker with preflight numbers
            try:
                _BUDGET.init_from_preflight(api_base=api_base, api_key=api_key, used=used, limit=limit)
            except Exception:
                pass
            _USAGE_CACHE[cache_key] = (now + max(10, ttl_s), out)
            return out
    except Exception:
        return None


def normalize_ratelimit_to_budget(
    *,
    exc: Exception,
    default_cooldown_s: int = 120,
) -> Dict[str, Any]:
    """
    Given a RateLimit/429 exception, classify as budget_exhausted when preflight shows 0 remaining
    (or message hints quota/plan). Returns a mapping suitable for ProxyException construction.
    """
    # Inspect headers for Retry-After
    headers: Dict[str, str] = {}
    try:
        resp_headers = getattr(exc, "response", None)
        if resp_headers is not None:
            resp_headers = getattr(resp_headers, "headers", None)
        if isinstance(resp_headers, dict):
            headers.update({str(k): str(v) for k, v in resp_headers.items()})
    except Exception:
        pass

    cooldown = _retry_after_seconds_from_headers(headers) or default_cooldown_s
    headers.setdefault("Retry-After", str(cooldown))

    msg = str(getattr(exc, "message", str(exc)) or "Rate limit")
    lower = msg.lower()

    # Use cached preflight data only — never make a blocking HTTP call in the
    # error path (this runs on the async event loop).
    cache_key = f"{(os.getenv('CHUTES_API_BASE', '').strip('/'))}:/users/me/usage"
    cached = _USAGE_CACHE.get(cache_key)
    pf = cached[1] if cached and cached[0] > _dt.datetime.utcnow().timestamp() else None

    exhausted = False
    limit = _get_daily_limit()
    remaining = None
    if pf and pf.get("ok"):
        limit = int(pf.get("limit", limit))
        remaining = int(pf.get("remaining", 0))
        exhausted = remaining <= 0
    else:
        # Heuristic fallback based on message hints (no HTTP call)
        exhausted = any(kw in lower for kw in ("quota", "plan", "budget", "daily"))

    reset_at = _next_reset_at_iso_utc()
    # Convey reset hint via header for clients while keeping OpenAI-style body
    headers.setdefault("x-budget-reset-at", reset_at)
    if remaining is not None:
        headers.setdefault("x-ratelimit-remaining-requests", str(max(0, remaining)))
    if limit is not None:
        headers.setdefault("x-ratelimit-limit", str(limit))

    if exhausted:
        return {
            "message": msg,
            "type": "budget_exhausted",
            "code": 429,
            "headers": headers,
        }
    else:
        return {
            "message": msg,
            "type": getattr(exc, "type", "throttling_error"),
            "code": 429,
            "headers": headers,
        }


__all__ = [
    "preflight_usage",
    "normalize_ratelimit_to_budget",
    "budget_register_attempt",
    "budget_snapshot",
    "payg_snapshot",
    "budget_metadata",
    "BudgetMiddleware",
    "get_budget_snapshot",
]


# ── BaseMiddleware integration ────────────────────────────────────────────

from scillm.proxy.middleware import BaseMiddleware as _BaseMiddleware


class BudgetMiddleware(_BaseMiddleware):
    """Track spend per request and expose budget info via response headers.

    Pre-call: registers an attempt with the budget tracker.
    Post-call: no-op (headers are set by the budget endpoint, not per-response).
    On-error: observes 429s and classifies as budget_exhausted vs throttling.
    """

    async def pre_call(self, request: dict) -> dict | None:
        budget_register_attempt()
        return request

    async def on_error(self, request: dict, error: Exception) -> None:
        status = getattr(error, "status_code", None) or getattr(error, "status", None)
        if status == 429:
            from loguru import logger
            info = normalize_ratelimit_to_budget(exc=error)
            logger.warning("budget_guard: 429 classified as {}", info.get("type"))


def get_budget_snapshot() -> Dict[str, Any]:
    """Return budget snapshot for the /v1/budget endpoint."""
    meta = budget_metadata()
    snap = payg_snapshot() or budget_snapshot() or {}
    meta["status"] = "ok" if snap else "no_data"
    return meta


def budget_register_attempt() -> Optional[Dict[str, Any]]:
    """Increment local attempt counter for current key/base and return snapshot."""
    api_base = (os.getenv("CHUTES_API_BASE", "").strip("/"))
    api_key = os.getenv("CHUTES_API_KEY", "").strip()
    if not api_base or not api_key:
        return None
    try:
        st = _BUDGET.register_attempt(api_base=api_base, api_key=api_key)
        snap = _BUDGET.snapshot(api_base=api_base, api_key=api_key)
        return snap
    except Exception:
        return None


def budget_snapshot() -> Optional[Dict[str, Any]]:
    api_base = (os.getenv("CHUTES_API_BASE", "").strip("/"))
    api_key = os.getenv("CHUTES_API_KEY", "").strip()
    if not api_base or not api_key:
        return None
    try:
        return _BUDGET.snapshot(api_base=api_base, api_key=api_key)
    except Exception:
        return None


def payg_snapshot(ttl_s: int = 60) -> Optional[Dict[str, Any]]:
    """Return PAYG status with spend today if available.

    Fields: ok, payg_active(bool), used, limit, remaining, cost_usd_today(float|None), reset_at(iso)
    """
    pf = preflight_usage(ttl_s=ttl_s)
    if not pf or not pf.get("ok"):
        return None
    used = int(pf.get("used", 0))
    limit = int(pf.get("limit", _get_daily_limit()))
    remaining = max(0, limit - used)
    payg_active = used >= limit
    reset_at = _next_reset_at_iso_utc()
    return {
        "ok": True,
        "payg_active": payg_active,
        "used": used,
        "limit": limit,
        "remaining": remaining,
        "cost_usd_today": (pf.get("cost_usd_today") if isinstance(pf.get("cost_usd_today"), (int, float)) else None),
        "reset_at": reset_at,
    }


def budget_metadata() -> Dict[str, Any]:
    """Return a standardized budget metadata block for response bodies.

    Fields:
      - plan: str (from CHUTES_PLAN or "unknown")
      - daily_limit: int
      - used_today: int
      - remaining: int
      - reset_at: ISO8601 string
      - credits_balance: float|null (from CHUTES_CREDITS_BALANCE if provided)
    """
    # Prefer PAYG snapshot (includes used + optional spend), fallback to local snapshot
    snap = payg_snapshot(ttl_s=30) or budget_snapshot() or {}
    limit = int(snap.get("limit") or _get_daily_limit())
    remaining = int(snap.get("remaining") or max(0, limit - int(snap.get("used", 0))))
    used_today = max(0, limit - remaining)
    reset_at = snap.get("reset_at") or _next_reset_at_iso_utc()
    plan = os.getenv("CHUTES_PLAN", "unknown").strip() or "unknown"
    credits_balance: Optional[float] = None
    try:
        cb = os.getenv("CHUTES_CREDITS_BALANCE", "").strip()
        if cb:
            credits_balance = float(cb)
    except Exception:
        credits_balance = None
    return {
        "plan": plan,
        "daily_limit": int(limit),
        "used_today": int(used_today),
        "remaining": int(remaining),
        "reset_at": str(reset_at),
        "credits_balance": credits_balance,
    }
