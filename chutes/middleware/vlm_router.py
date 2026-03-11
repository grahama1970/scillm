"""Proxy-level VLM auto-routing.

Inspects incoming chat completion requests for image_url content parts.
When detected and the requested model is a text model, rewrites the model
to "vlm" so the router sends it to VLM providers instead of text-only ones.

Migrated from CustomLogger to BaseMiddleware interface.
"""
from __future__ import annotations

from typing import Any, Set

from loguru import logger

from scillm.proxy.middleware import BaseMiddleware

# Known text model prefixes — anything starting with these or exactly matching
# is considered a text-only model that needs VLM rerouting for image requests.
TEXT_MODEL_NAMES: Set[str] = {
    "text", "local-text", "moonshot-text", "text-deepseek", "text-gemini",
}


def _is_text_model(model: str) -> bool:
    """Check if model is a text-only model."""
    model_lower = model.strip().lower()
    if model_lower in TEXT_MODEL_NAMES:
        return True
    if model_lower.startswith("text"):
        return True
    return False


def _has_image_content(messages: list) -> bool:
    """Check if any message contains image_url parts."""
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
        if isinstance(content, dict) and "image_url" in content:
            return True
    return False


class VlmRouter(BaseMiddleware):
    """Rewrites model to 'vlm' when request contains image content."""

    async def pre_call(self, request: dict) -> dict | None:
        model = (request.get("model") or "").strip()
        if not model or model.lower().startswith("vlm"):
            return request

        messages = request.get("messages")
        if not messages or not isinstance(messages, list):
            return request

        if not _is_text_model(model):
            return request

        if _has_image_content(messages):
            original = model
            request["model"] = "vlm"
            logger.info("vlm_router: auto-routed '{}' -> 'vlm' (image_url detected)", original)

        return request
