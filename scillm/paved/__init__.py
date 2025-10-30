from __future__ import annotations

# A single import point for the paved path used by project agents.
# This intentionally re-exports the stable helpers that:
# - Use openai_like + Bearer for Chutes
# - Return OpenAI-shaped responses with JSON mode
# - Keep retries deterministic by default (caller can opt into tenacious)

from scillm.extras.chutes_simple import (
    chutes_chat_json,
    chutes_router_json,
)

__all__ = [
    "chutes_chat_json",
    "chutes_router_json",
]

