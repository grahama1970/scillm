from __future__ import annotations

# A single import point for the paved path used by project agents.
# This intentionally re-exports the stable helpers that:
# - Use openai_like + Bearer for Chutes
# - Return OpenAI-shaped responses with JSON mode
# - Keep retries deterministic by default (caller can opt into tenacious)

from scillm.extras.chutes_simple import (
    chutes_chat_json,
    chutes_router_json,
    chutes_healthcheck,
)

from scillm.paved.chat import (
    chat,
    chat_json,
    analyze_image,
    analyze_image_json,
)
from scillm.paved.preflight import (
    sanity_preflight,
    list_models_openai_like,
)

__all__ = [
    # Simple chat helpers
    "chat",
    "chat_json",
    "analyze_image",
    "analyze_image_json",
    # Chutes-specific
    "chutes_chat_json",
    "chutes_router_json",
    "chutes_healthcheck",
    # Preflight / discovery
    "sanity_preflight",
    "list_models_openai_like",
]
