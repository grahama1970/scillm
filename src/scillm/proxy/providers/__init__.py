"""Provider-specific API adapters for non-OpenAI-compatible backends."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any


def sse_chunk(
    chunk_id: str,
    model: str,
    content_delta: str | None = None,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an OpenAI-compatible streaming chunk dict.

    Returns a dict matching the ``chat.completion.chunk`` schema used by
    the OpenAI SDK streaming responses.
    """
    delta: dict[str, Any] = {}
    if content_delta is not None:
        delta["content"] = content_delta
        delta["role"] = "assistant"

    chunk: dict[str, Any] = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def sse_done() -> str:
    """Return the SSE [DONE] sentinel line."""
    return "data: [DONE]\n\n"


def sse_format(data: dict[str, Any]) -> str:
    """Format a dict as an SSE data line: ``data: {json}\\n\\n``."""
    return f"data: {json.dumps(data)}\n\n"


def make_chunk_id() -> str:
    """Generate a unique chunk ID for streaming responses."""
    return f"chatcmpl-{uuid.uuid4().hex[:12]}"
