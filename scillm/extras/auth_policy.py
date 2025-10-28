from __future__ import annotations

from typing import Dict, Tuple


def choose_auth(headers: Dict[str, str] | None, *, stream: bool = False) -> Tuple[Dict[str, str], str]:
    """
    Paved-path auth selection for OpenAI-compatible gateways.
    - Non-stream (JSON): prefer x-api-key when present.
    - Stream/tools: prefer Authorization: Bearer when present.
    - Never invent a token; only pick between what's provided.
    Returns: (new_headers, style) where style in {'x-api-key','bearer','none'}.
    """
    h = dict(headers or {})
    style = "none"
    has_key = "x-api-key" in h and isinstance(h.get("x-api-key"), str) and h.get("x-api-key").strip() != ""
    auth = h.get("Authorization")
    has_bearer = isinstance(auth, str) and auth.strip().startswith("Bearer ")

    if not stream:
        # JSON path prefers x-api-key if provided
        if has_key:
            style = "x-api-key"
            # Remove conflicting Authorization to avoid gateway confusion
            if has_bearer:
                h.pop("Authorization", None)
            return h, style
        if has_bearer:
            style = "bearer"
            return h, style
        return h, style
    else:
        # Streaming/tools commonly expect Bearer
        if has_bearer:
            style = "bearer"
            # Keep x-api-key alongside Bearer if present; many gateways ignore it in stream
            return h, style
        if has_key:
            style = "x-api-key"
            return h, style
        return h, style


__all__ = ["choose_auth"]

