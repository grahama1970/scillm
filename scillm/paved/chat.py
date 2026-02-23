"""Simple chat wrappers for scillm.

These provide one-liner access to LLM completions without boilerplate.

Usage:
    from scillm.paved import chat, chat_json, analyze_image

    # Simple text
    answer = await chat("What is 2+2?")

    # JSON response
    data = await chat_json("Return {name, age} for John who is 30")

    # Image analysis
    desc = await analyze_image("https://example.com/photo.jpg", "Describe this")
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from scillm.batch import parallel_acompletions_iter


# Defaults - can be overridden via env vars or function args
DEFAULT_MODEL = os.getenv("SCILLM_MODEL", "text")
DEFAULT_VISION_MODEL = os.getenv("SCILLM_VISION_MODEL", "vlm")
DEFAULT_API_BASE = os.getenv("SCILLM_API_BASE", "http://localhost:4010")
DEFAULT_API_KEY = os.getenv("SCILLM_PROXY_KEY", os.getenv("OPENROUTER_API_KEY", "sk-dev-proxy-123"))


async def chat(
    prompt: str,
    *,
    model: Optional[str] = None,
    system: Optional[str] = None,
    api_base: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 1024,
) -> str:
    """Simple text completion.

    Args:
        prompt: User message
        model: Model ID (default: SCILLM_MODEL or "text" via proxy)
        system: Optional system prompt
        api_base: API endpoint (default: SCILLM_API_BASE or localhost:4010)
        api_key: API key (default: SCILLM_PROXY_KEY)
        temperature: Sampling temperature
        max_tokens: Max response tokens

    Returns:
        Response text

    Example:
        >>> answer = await chat("What is the capital of France?")
        >>> print(answer)  # "Paris"
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    request = {
        "model": model or DEFAULT_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    async for r in parallel_acompletions_iter(
        [request],
        api_base=api_base or DEFAULT_API_BASE,
        api_key=api_key or DEFAULT_API_KEY,
        custom_llm_provider="openai",
        concurrency=1,
    ):
        if r.get("ok"):
            return r["content"]
        else:
            raise RuntimeError(f"Chat failed: {r.get('error')}")

    raise RuntimeError("No response received")


async def chat_json(
    prompt: str,
    *,
    model: Optional[str] = None,
    system: Optional[str] = None,
    api_base: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.2,
    max_tokens: int = 1024,
) -> Dict[str, Any]:
    """Get JSON response from LLM.

    Args:
        prompt: User message (should ask for JSON)
        model: Model ID
        system: Optional system prompt
        api_base: API endpoint
        api_key: API key
        temperature: Lower is more deterministic
        max_tokens: Max response tokens

    Returns:
        Parsed JSON dict

    Example:
        >>> data = await chat_json("Return {\"name\": \"...\", \"age\": ...} for Alice who is 25")
        >>> print(data)  # {"name": "Alice", "age": 25}
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    request = {
        "model": model or DEFAULT_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }

    async for r in parallel_acompletions_iter(
        [request],
        api_base=api_base or DEFAULT_API_BASE,
        api_key=api_key or DEFAULT_API_KEY,
        custom_llm_provider="openai",
        concurrency=1,
    ):
        if r.get("ok"):
            content = r["content"]
            if isinstance(content, dict):
                return content
            try:
                return json.loads(content)
            except (json.JSONDecodeError, TypeError):
                return {"raw": content}
        else:
            raise RuntimeError(f"Chat JSON failed: {r.get('error')}")

    raise RuntimeError("No response received")


async def analyze_image(
    image: str,
    prompt: str = "Describe this image",
    *,
    model: Optional[str] = None,
    api_base: Optional[str] = None,
    api_key: Optional[str] = None,
    max_tokens: int = 1024,
) -> str:
    """Analyze an image with a vision model.

    Args:
        image: URL, file path, or base64 data URI
        prompt: Question about the image
        model: Vision-capable model (default: claude-sonnet)
        api_base: API endpoint
        api_key: API key
        max_tokens: Max response tokens

    Returns:
        Description/analysis text

    Example:
        >>> desc = await analyze_image("https://example.com/chart.png", "What does this chart show?")
        >>> desc = await analyze_image("/path/to/photo.jpg", "Describe this")
    """
    # Handle file paths
    if not image.startswith(("http://", "https://", "data:")):
        path = Path(image)
        if path.exists():
            suffix = path.suffix.lower()
            mime = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".gif": "image/gif",
                ".webp": "image/webp",
            }.get(suffix, "image/png")
            b64 = base64.b64encode(path.read_bytes()).decode()
            image = f"data:{mime};base64,{b64}"

    request = {
        "model": model or DEFAULT_VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image}},
            ],
        }],
        "max_tokens": max_tokens,
    }

    async for r in parallel_acompletions_iter(
        [request],
        api_base=api_base or DEFAULT_API_BASE,
        api_key=api_key or DEFAULT_API_KEY,
        custom_llm_provider="openai",
        concurrency=1,
    ):
        if r.get("ok"):
            return r["content"]
        else:
            raise RuntimeError(f"Image analysis failed: {r.get('error')}")

    raise RuntimeError("No response received")


async def analyze_image_json(
    image: str,
    prompt: str,
    *,
    model: Optional[str] = None,
    api_base: Optional[str] = None,
    api_key: Optional[str] = None,
    max_tokens: int = 1024,
) -> Dict[str, Any]:
    """Analyze an image and return structured JSON.

    Args:
        image: URL, file path, or base64 data URI
        prompt: Should ask for JSON output
        model: Vision-capable model
        api_base: API endpoint
        api_key: API key
        max_tokens: Max response tokens

    Returns:
        Parsed JSON dict

    Example:
        >>> data = await analyze_image_json(
        ...     "https://example.com/receipt.jpg",
        ...     "Extract {\"total\": number, \"items\": [{\"name\": str, \"price\": number}]}"
        ... )
    """
    # Handle file paths (same as analyze_image)
    if not image.startswith(("http://", "https://", "data:")):
        path = Path(image)
        if path.exists():
            suffix = path.suffix.lower()
            mime = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".gif": "image/gif",
                ".webp": "image/webp",
            }.get(suffix, "image/png")
            b64 = base64.b64encode(path.read_bytes()).decode()
            image = f"data:{mime};base64,{b64}"

    request = {
        "model": model or DEFAULT_VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image}},
            ],
        }],
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }

    async for r in parallel_acompletions_iter(
        [request],
        api_base=api_base or DEFAULT_API_BASE,
        api_key=api_key or DEFAULT_API_KEY,
        custom_llm_provider="openai",
        concurrency=1,
    ):
        if r.get("ok"):
            content = r["content"]
            if isinstance(content, dict):
                return content
            try:
                return json.loads(content)
            except (json.JSONDecodeError, TypeError):
                return {"raw": content}
        else:
            raise RuntimeError(f"Image JSON analysis failed: {r.get('error')}")

    raise RuntimeError("No response received")


__all__ = ["chat", "chat_json", "analyze_image", "analyze_image_json"]
