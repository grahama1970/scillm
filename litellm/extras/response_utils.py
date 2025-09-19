"""
Small response helpers to reduce boilerplate and be tolerant of dict/typed shapes.

APIs:
- extract_content(resp) -> str
- assemble_stream_text(stream_iter) -> str
- augment_json_with_cost(json_text, resp) -> json_text (with metadata.token_usage/cache_hit if present)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any


def extract_content(resp: Any) -> str:
    """Return message content from a litellm-style response (dict or typed)."""
    try:
        choices = getattr(resp, "choices", None) or resp.get("choices", [])
        first = choices[0] if choices else {}
        msg = getattr(first, "message", None) or first.get("message", {})
        return (getattr(msg, "content", None) or msg.get("content") or "")
    except Exception:
        return ""


async def assemble_stream_text(stream_iter: Any) -> str:
    """Consume an async iterator of streaming chunks and join text content.

    Accepts litellm's streaming iterator or any async iterator yielding str/chunk dicts.
    """
    parts: list[str] = []
    async for chunk in stream_iter:
        if isinstance(chunk, str):
            parts.append(chunk)
        else:
            # try OpenAI-style: chunk.choices[0].delta.content or dict equivalent
            try:
                choices = getattr(chunk, "choices", None) or chunk.get("choices", [])
                first = choices[0] if choices else {}
                delta = getattr(first, "delta", None) or first.get("delta", {})
                text = getattr(delta, "content", None) or delta.get("content")
                if text:
                    parts.append(text)
            except Exception:
                pass
    return "".join(parts)


def augment_json_with_cost(text_json: str, resp: Any) -> str:
    """Augment an existing JSON (string) with usage + cache metadata if present on resp.

    The resulting JSON is returned as a string; on parse errors, returns original.
    """
    try:
        data = json.loads(text_json)
        if not isinstance(data, dict):
            return text_json
        meta = data.setdefault("metadata", {}) if isinstance(data, dict) else {}
        if hasattr(resp, "usage"):
            meta["token_usage"] = getattr(resp, "usage")
        hidden = getattr(resp, "_hidden_params", {})
        if isinstance(hidden, dict) and "cache_hit" in hidden:
            meta["cache_hit"] = hidden.get("cache_hit")
        data["metadata"] = meta
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        return text_json

