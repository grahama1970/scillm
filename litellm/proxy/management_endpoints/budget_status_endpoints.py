from __future__ import annotations

from fastapi import APIRouter
import os

try:
    from chutes.middleware.budget_guard import payg_snapshot  # type: ignore
except Exception:  # pragma: no cover
    payg_snapshot = None  # type: ignore

try:
    from chutes.middleware.pricing import get_pricing  # type: ignore
except Exception:  # pragma: no cover
    get_pricing = None  # type: ignore


router = APIRouter()


@router.get("/v1/budget", include_in_schema=False)
async def budget_status():
    out = {"limit": 0, "remaining": 0, "reset_at": "", "price_per_call_usd": 0.0}
    try:
        if payg_snapshot is not None:
            ps = payg_snapshot(ttl_s=30)
            if ps and ps.get("ok"):
                lim = ps.get("limit")
                rem = ps.get("remaining")
                out["limit"] = int(lim) if lim is not None else 0
                out["remaining"] = int(rem) if rem is not None else 0
                out["reset_at"] = str(ps.get("reset_at") or "")
    except Exception:
        pass
    try:
        p = float(os.getenv("CHUTES_PRICE_PER_CALL_USD", "0") or 0)
        out["price_per_call_usd"] = p
    except Exception:
        pass
    return out


@router.get("/v1/pricing", include_in_schema=False)
async def pricing_status():
    if get_pricing is None:
        return {}
    try:
        return get_pricing(ttl_s=300)
    except Exception:
        return {}

