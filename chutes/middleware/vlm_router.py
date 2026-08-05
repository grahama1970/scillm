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
MOONSHOT_MULTIMODAL_TEXT_MODELS: Set[str] = {
    "moonshot-text",
}

TEXT_MODEL_NAMES: Set[str] = {
    "local-text",
    "moonshot-text",
    "chutes-deepseek",
    "chutes-kimi",
    "chutes-qwen",
    "chutes-qwen-large",
    "deepseek-direct",
    "gemini-flash",
    "gemini-flash-high",
}


def _supports_native_multimodal(model: str) -> bool:
    """Models that accept image_url on their own provider route."""
    return model.strip().lower() in MOONSHOT_MULTIMODAL_TEXT_MODELS


def _is_text_model(model: str) -> bool:
    """Check if model is a text-only model."""
    model_lower = model.strip().lower()
    if model_lower in TEXT_MODEL_NAMES:
        return True
    if model_lower.startswith(("chutes-", "gemini-flash")):
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
            if _supports_native_multimodal(model):
                logger.info(
                    "vlm_router: preserving native multimodal model %r (skip vlm rewrite)",
                    model,
                )
                return request
            if request.get("require_exact_model") or request.get("allow_model_remap") is False:
                logger.info(
                    "vlm_router: preserving exact model '{}' despite image_url content",
                    model,
                )
                return request
            original = model
            request["model"] = "vlm"
            logger.info("vlm_router: auto-routed '{}' -> 'vlm' (image_url detected)", original)

        return request
