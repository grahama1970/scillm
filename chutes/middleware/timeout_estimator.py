"""Timeout estimation middleware — queries /latency-stats for dynamic timeouts.

Estimates the appropriate timeout for each LLM call based on:
- Model and provider historical latency (p95 from llm_call_log)
- Estimated token count (prompt + expected completion)
- Throughput stats (tokens per second)

The estimated timeout is stored in `request["_dynamic_timeout_ms"]` and used by
the router to set per-call timeouts. Response headers show what was used.

Requires the memory service to be running at MEMORY_URL (default localhost:8601).
Falls back to config-based timeouts if memory service is unavailable.
"""
from __future__ import annotations

import os
import time
from typing import Any

import httpx
from loguru import logger

from scillm.proxy.middleware import BaseMiddleware

_MEMORY_URL = os.environ.get("MEMORY_URL", "http://127.0.0.1:8601")
_DEFAULT_TIMEOUT_MS = 120_000  # 2 minutes fallback
_MIN_TIMEOUT_MS = 10_000  # 10 seconds minimum
_MAX_TIMEOUT_MS = 600_000  # 10 minutes maximum

# Cache latency stats to avoid hammering memory service
# Key: (model, provider) -> (stats_dict, timestamp)
_stats_cache: dict[tuple[str, str | None], tuple[dict, float]] = {}
_CACHE_TTL_SEC = 300  # 5 minutes

# Lazy-init httpx client
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=_MEMORY_URL,
            timeout=httpx.Timeout(5.0, connect=2.0),
        )
    return _client


def _estimate_prompt_tokens(messages: list[dict]) -> int:
    """Rough token estimate from messages (chars / 4)."""
    total_chars = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            # Vision messages with multiple parts
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total_chars += len(part.get("text", ""))
    return max(1, total_chars // 4)


def _infer_provider(model: str) -> str | None:
    """Infer provider from model string for cache key."""
    m = model.lower()
    if "deepseek" in m:
        return "chutes" if "v3" in m or "r1" in m else "deepseek"
    if "gemini" in m or "flash" in m:
        return "google"
    if "claude" in m:
        return "anthropic"
    if "gpt" in m or "codex" in m:
        return "openai"
    if "qwen" in m:
        return "chutes"
    return None


async def _get_latency_stats(model: str, prompt_tokens: int, completion_tokens: int = 500) -> dict:
    """Fetch latency stats from memory service with caching."""
    provider = _infer_provider(model)
    cache_key = (model, provider)

    # Check cache
    now = time.monotonic()
    if cache_key in _stats_cache:
        cached, ts = _stats_cache[cache_key]
        if now - ts < _CACHE_TTL_SEC:
            # Update estimated timeout with current token counts
            if cached.get("throughput"):
                p95_tps = cached["throughput"].get("p95_tps", 0)
                if p95_tps > 0:
                    total_tokens = prompt_tokens + completion_tokens
                    cached["estimated_timeout_ms"] = int((total_tokens / p95_tps) * 1000 * 1.2)
            return cached

    # Query memory service
    try:
        client = _get_client()
        resp = await client.post(
            "/latency-stats",
            json={
                "model": model,
                "provider": provider,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "days": 7,
            },
        )
        if resp.status_code == 200:
            stats = resp.json()
            _stats_cache[cache_key] = (stats, now)
            return stats
        else:
            logger.debug("timeout_estimator: /latency-stats returned {}", resp.status_code)
    except Exception as exc:
        logger.debug("timeout_estimator: /latency-stats failed: {}", exc)

    return {}


class TimeoutEstimatorMiddleware(BaseMiddleware):
    """Estimate and set dynamic timeouts based on historical latency data."""

    async def pre_call(self, request: dict) -> dict:
        model = request.get("model", "")
        messages = request.get("messages", [])

        # Estimate tokens
        prompt_tokens = _estimate_prompt_tokens(messages)
        # Default expected completion — can be overridden by request
        completion_tokens = request.get("max_tokens", 500)
        if completion_tokens > 4000:
            # Reasoning models often have huge max_tokens but produce less
            completion_tokens = min(completion_tokens, 2000)

        # Get latency stats
        stats = await _get_latency_stats(model, prompt_tokens, completion_tokens)

        # Calculate timeout
        timeout_ms = _DEFAULT_TIMEOUT_MS
        timeout_source = "default"

        if stats.get("estimated_timeout_ms"):
            # Token-aware estimate available
            timeout_ms = stats["estimated_timeout_ms"]
            timeout_source = "estimated"
        elif stats.get("recommended_timeout_ms"):
            # p95 from historical data
            timeout_ms = stats["recommended_timeout_ms"]
            timeout_source = "p95"
        elif stats.get("percentiles", {}).get("p95"):
            timeout_ms = stats["percentiles"]["p95"]
            timeout_source = "p95"

        # Clamp to reasonable bounds
        timeout_ms = max(_MIN_TIMEOUT_MS, min(_MAX_TIMEOUT_MS, timeout_ms))

        # Store in request for router to use
        request["_dynamic_timeout_ms"] = timeout_ms
        request["_timeout_source"] = timeout_source
        throughput = stats.get("throughput") or {}
        request["_timeout_stats"] = {
            "prompt_tokens": prompt_tokens,
            "sample_count": stats.get("sample_count", 0),
            "p95_tps": throughput.get("p95_tps"),
        }

        logger.debug(
            "timeout_estimator: model={} tokens={} timeout={}ms ({})",
            model, prompt_tokens, timeout_ms, timeout_source,
        )

        return request

    async def post_call(self, request: dict, response: Any) -> Any:
        """Inject timeout headers into response."""
        if not isinstance(response, dict):
            return response

        # Stash headers for app.py to extract (same pattern as CostHeaderMiddleware)
        timeout_headers = response.setdefault("_timeout_headers", {})

        timeout_ms = request.get("_dynamic_timeout_ms", _DEFAULT_TIMEOUT_MS)
        timeout_source = request.get("_timeout_source", "default")

        timeout_headers["x-scillm-timeout-ms"] = str(timeout_ms)
        timeout_headers["x-scillm-timeout-source"] = timeout_source

        stats = request.get("_timeout_stats", {})
        if stats.get("sample_count"):
            timeout_headers["x-scillm-timeout-samples"] = str(stats["sample_count"])

        return response

    async def on_error(self, request: dict, error: Exception) -> None:
        """Log timeout-related errors for future estimation improvement."""
        import asyncio
        if isinstance(error, (asyncio.TimeoutError, httpx.TimeoutException)):
            model = request.get("model", "unknown")
            timeout_ms = request.get("_dynamic_timeout_ms", "?")
            logger.warning(
                "timeout_estimator: timeout for model={} (estimated={}ms) — consider increasing buffer",
                model, timeout_ms,
            )
