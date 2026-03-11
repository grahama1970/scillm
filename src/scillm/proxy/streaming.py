"""SSE streaming response handler for OpenAI-compatible async streams.

Converts openai SDK async streams into FastAPI/Starlette StreamingResponse
objects with proper SSE formatting and graceful disconnect handling.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from loguru import logger
from starlette.responses import StreamingResponse

SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


async def _sse_generator(stream: Any) -> AsyncIterator[str]:
    """Iterate over an OpenAI async stream, yielding SSE-formatted lines.

    On provider disconnect or any mid-stream exception the error is logged
    and the stream is closed cleanly with a ``[DONE]`` sentinel.
    """
    try:
        async for chunk in stream:
            payload = chunk.model_dump() if hasattr(chunk, "model_dump") else chunk
            yield f"data: {json.dumps(payload)}\n\n"
    except Exception as exc:
        logger.error("Stream interrupted: {}", exc)
        # Signal the error to clients instead of silently closing
        error_payload = {
            "error": {
                "message": f"stream_interrupted: {exc}",
                "type": "stream_error",
            }
        }
        yield f"data: {json.dumps(error_payload)}\n\n"
    finally:
        yield "data: [DONE]\n\n"


async def stream_response(stream: Any) -> StreamingResponse:
    """Convert an OpenAI ``AsyncStream`` into a Starlette ``StreamingResponse``.

    Parameters
    ----------
    stream:
        An async iterable of chat completion chunks (e.g. from
        ``openai.AsyncOpenAI.chat.completions.create(stream=True)``).

    Returns
    -------
    StreamingResponse
        A streaming HTTP response with ``text/event-stream`` content type
        and appropriate SSE headers.
    """
    return StreamingResponse(
        _sse_generator(stream),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


async def collect_response(stream: Any) -> dict[str, Any]:
    """Collect all chunks from an OpenAI async stream into a single response dict.

    Assembles ``delta`` content fields across chunks and returns the final
    aggregated response suitable for non-streaming callers.

    Parameters
    ----------
    stream:
        An async iterable of chat completion chunks.

    Returns
    -------
    dict
        The assembled response with concatenated content and final metadata.
    """
    result: dict[str, Any] = {}
    collected_content: list[str] = []
    tool_calls_by_index: dict[int, dict[str, Any]] = {}

    try:
        async for chunk in stream:
            data = chunk.model_dump() if hasattr(chunk, "model_dump") else chunk

            # Preserve top-level metadata from every chunk (last wins).
            for key in ("id", "object", "created", "model", "system_fingerprint"):
                if key in data and data[key] is not None:
                    result[key] = data[key]

            if "usage" in data and data["usage"] is not None:
                result["usage"] = data["usage"]

            choices = data.get("choices") or []
            if not choices:
                continue

            choice = choices[0]
            delta = choice.get("delta") or {}

            # Accumulate text content.
            if delta.get("content"):
                collected_content.append(delta["content"])

            # Accumulate tool calls.
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                if idx not in tool_calls_by_index:
                    tool_calls_by_index[idx] = {
                        "id": tc.get("id", ""),
                        "type": tc.get("type", "function"),
                        "function": {"name": "", "arguments": ""},
                    }
                entry = tool_calls_by_index[idx]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    entry["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    entry["function"]["arguments"] += fn["arguments"]

            # Capture finish_reason from the final chunk.
            if choice.get("finish_reason"):
                result["_finish_reason"] = choice["finish_reason"]

    except Exception as exc:
        logger.error("Error collecting stream: {}", exc)

    # Build the assembled response.
    message: dict[str, Any] = {"role": "assistant"}
    if collected_content:
        message["content"] = "".join(collected_content)
    if tool_calls_by_index:
        message["tool_calls"] = [
            tool_calls_by_index[i] for i in sorted(tool_calls_by_index)
        ]

    result["choices"] = [
        {
            "index": 0,
            "message": message,
            "finish_reason": result.pop("_finish_reason", "stop"),
        }
    ]

    return result
