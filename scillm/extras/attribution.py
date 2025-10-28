from __future__ import annotations

from typing import Any, Optional


def extract_served_model(resp: Any) -> Optional[str]:
    """Best-effort extraction of the served model from various response shapes."""
    # Object-like with .model
    try:
        m = getattr(resp, "model", None)
        if isinstance(m, str) and m:
            return m
    except Exception:
        pass
    # Dict-like with ['model']
    if isinstance(resp, dict):
        try:
            m = resp.get("model")
            if isinstance(m, str) and m:
                return m
        except Exception:
            pass
    # Sometimes nested in additional_kwargs
    try:
        ak = getattr(resp, "additional_kwargs", None) or {}
        if isinstance(ak, dict):
            m = ak.get("model") or ak.get("served_model")
            if isinstance(m, str) and m:
                return m
    except Exception:
        pass
    return None


__all__ = ["extract_served_model"]

