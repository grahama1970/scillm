"""ArangoDB call log middleware — writes every LLM completion to memory daemon.

Creates documents in the `llm_call_log` collection via /upsert, enabling:
- /learn-timeout to estimate p95 latency per model/provider
- /orchestrate to diagnose failures and self-correct
- /analytics to track cost and token trends

No message content is stored — only metadata.
"""
from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from loguru import logger

from scillm.proxy.middleware import BaseMiddleware

_MEMORY_URL = os.environ.get("MEMORY_URL", "http://127.0.0.1:8601")
_COLLECTION = "llm_call_log"

# Lazy-init httpx client (reuses connection pool)
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=_MEMORY_URL,
            timeout=httpx.Timeout(5.0, connect=2.0),
        )
    return _client


def _make_key(ts_iso: str, model: str) -> str:
    """Deterministic _key from timestamp + model."""
    raw = f"{ts_iso}:{model}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class ArangoLogMiddleware(BaseMiddleware):
    """Log every LLM call to ArangoDB for /learn-timeout and /orchestrate."""

    async def pre_call(self, request: dict) -> dict:
        request["_arango_log_start"] = time.monotonic()
        return request

    async def post_call(self, request: dict, response: Any) -> Any:
        try:
            await self._write(request, response, error=None)
        except Exception as exc:
            logger.debug("arango_log: post_call write failed: {}", exc)
        return response

    async def on_error(self, request: dict, error: Exception) -> None:
        try:
            await self._write(request, {}, error=type(error).__name__)
        except Exception as exc:
            logger.debug("arango_log: on_error write failed: {}", exc)

    async def _write(
        self, request: dict, response: Any, *, error: str | None
    ) -> None:
        now = datetime.now(timezone.utc)
        start = request.get("_arango_log_start")
        duration_ms = (
            round((time.monotonic() - start) * 1000)
            if start is not None
            else None
        )

        usage = {}
        model_served = request.get("model", "")
        cost_usd = None

        if isinstance(response, dict) and not response.get("stream"):
            usage = response.get("usage", {})
            model_served = response.get("model", model_served)
            cost_headers = response.get("_cost_headers", {})
            raw_cost = cost_headers.get("x-cost-usd", "")
            if raw_cost and raw_cost != "unknown":
                try:
                    cost_usd = float(raw_cost)
                except ValueError:
                    pass

        doc = {
            "_key": _make_key(now.isoformat(), model_served),
            "ts": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"),
            "task_type": "llm_api_call",
            "model_requested": request.get("model", ""),
            "model_served": model_served,
            "provider": _infer_provider(model_served),
            "duration_ms": duration_ms,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "cost_usd": cost_usd,
            "stream": bool(request.get("stream")),
            "cache_hit": bool(request.get("_cache_hit")),
            "status": "error" if error else "ok",
            "error": error,
            "caller": request.get("_caller_skill", "") or request.get("_headers", {}).get("x-caller-skill", ""),
        }

        client = _get_client()
        resp = await client.post(
            "/upsert",
            json={"collection": _COLLECTION, "documents": [doc]},
        )
        if resp.status_code >= 400:
            logger.debug(
                "arango_log: /upsert returned {}: {}",
                resp.status_code,
                resp.text[:200],
            )


def _infer_provider(model: str) -> str:
    """Infer provider name from model string."""
    m = model.lower()
    if "deepseek" in m:
        return "chutes" if "v3" in m or "r1" in m else "deepseek"
    if "gemini" in m or "flash" in m:
        return "google"
    if "qwen" in m:
        return "chutes"
    if "glm" in m:
        return "zhipu"
    if "kimi" in m or "moonshot" in m:
        return "moonshot"
    if "claude" in m:
        return "anthropic"
    if "gpt" in m or "codex" in m:
        return "openai"
    return "unknown"
