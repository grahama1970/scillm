"""Provider-specific API adapters for non-OpenAI-compatible backends."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx


def streaming_timeout(overall_timeout_s: float | int | None = None) -> httpx.Timeout:
    """Return an httpx timeout suitable for provider SSE streams.

    Connect/pool/write failures should surface quickly, but reads must not use
    a short response cap because long reasoning streams can be silent between
    provider events. The proxy-level SSE liveness wrapper owns heartbeat and
    overall-budget enforcement.
    """
    connect_timeout = 10.0
    if overall_timeout_s is not None:
        try:
            connect_timeout = min(connect_timeout, max(1.0, float(overall_timeout_s)))
        except (TypeError, ValueError):
            pass
    return httpx.Timeout(timeout=None, connect=connect_timeout, read=None, write=30.0, pool=10.0)


def sse_chunk(
    chunk_id: str,
    model: str,
    content_delta: str | None = None,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build an OpenAI-compatible streaming chunk dict.

    Returns a dict matching the ``chat.completion.chunk`` schema used by
    the OpenAI SDK streaming responses.

    For tool calls, pass ``tool_calls`` as a list of dicts with:
    - index, id, type, function.name, function.arguments (for initial chunk)
    - index, function.arguments (for argument deltas)
    """
    delta: dict[str, Any] = {}
    if content_delta is not None:
        delta["content"] = content_delta
        delta["role"] = "assistant"
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
        if "role" not in delta:
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
