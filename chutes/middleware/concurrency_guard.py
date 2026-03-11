"""Provider-aware concurrency guard with bounded queuing.

Limits concurrent in-flight requests per provider and QUEUES excess
requests until a slot opens. Prevents the 90-second Chutes penalty
when exceeding their 5-connection limit.

Safety bounds:
  - MAX_QUEUE_PER_PROVIDER: reject with 429 when queue exceeds this depth.
  - QUEUE_TIMEOUT_S: queued requests time out after this many seconds.

Migrated from CustomLogger to BaseMiddleware interface.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from loguru import logger

from scillm.proxy.middleware import BaseMiddleware, MiddlewareReject

# ── Provider concurrency limits ──────────────────────────────────────────
PROVIDER_LIMITS: Dict[str, int] = {
    "chutes": 4,
    "ollama": 1,
    "moonshot": 3,
    "deepseek": 8,
    "gemini": 10,
    "openrouter": 6,
    "certainly": 1,
    "codeworld": 1,
}
DEFAULT_LIMIT = 6

# ── Queue safety bounds ──────────────────────────────────────────────────
MAX_QUEUE_PER_PROVIDER = 50   # Reject new requests when queue exceeds this
QUEUE_TIMEOUT_S = 60.0        # Queued requests time out after 60s

# ── State ────────────────────────────────────────────────────────────────
_semaphores: Dict[str, asyncio.Semaphore] = {}
_in_flight: Dict[str, int] = {}
_queue_depth: Dict[str, int] = {}


def _resolve_provider(model: str) -> str:
    """Determine provider key from model name."""
    model_lower = model.lower()
    for provider in PROVIDER_LIMITS:
        if provider in model_lower:
            return provider
    # Common prefix patterns
    if model_lower.startswith("text"):
        return "chutes"
    if model_lower.startswith("local"):
        return "ollama"
    return "default"


def _get_semaphore(provider: str) -> asyncio.Semaphore:
    """Get or create semaphore for provider (lazy init)."""
    if provider not in _semaphores:
        limit = PROVIDER_LIMITS.get(provider, DEFAULT_LIMIT)
        _semaphores[provider] = asyncio.Semaphore(limit)
        _in_flight[provider] = 0
        _queue_depth[provider] = 0
        logger.info("concurrency_guard: init {} semaphore (limit={})", provider, limit)
    return _semaphores[provider]


def _release(provider: str) -> None:
    """Release a semaphore slot."""
    sem = _semaphores.get(provider)
    if sem:
        sem.release()
        _in_flight[provider] = max(0, _in_flight.get(provider, 1) - 1)


class ConcurrencyMiddleware(BaseMiddleware):
    """Limits concurrent requests per provider. Queues excess with bounds."""

    async def pre_call(self, request: dict) -> dict | None:
        model = request.get("model", "")
        provider = _resolve_provider(model)
        sem = _get_semaphore(provider)
        limit = PROVIDER_LIMITS.get(provider, DEFAULT_LIMIT)

        # Check queue depth before attempting acquire (reject if full)
        queued = _queue_depth.get(provider, 0)
        if queued >= MAX_QUEUE_PER_PROVIDER:
            logger.warning(
                "concurrency_guard: {} queue full ({}/{}), rejecting",
                provider, queued, MAX_QUEUE_PER_PROVIDER,
            )
            raise MiddlewareReject(
                f"Provider {provider} queue full ({queued} queued, limit {MAX_QUEUE_PER_PROVIDER})",
                status_code=429,
            )

        # Always use wait_for to acquire — avoids race between locked() check and acquire
        _queue_depth[provider] = queued + 1
        try:
            await asyncio.wait_for(sem.acquire(), timeout=QUEUE_TIMEOUT_S)
        except asyncio.TimeoutError:
            _queue_depth[provider] = max(0, _queue_depth.get(provider, 0) - 1)
            logger.warning(
                "concurrency_guard: {} queue timeout after {:.0f}s",
                provider, QUEUE_TIMEOUT_S,
            )
            raise MiddlewareReject(
                f"Provider {provider} queue timeout ({QUEUE_TIMEOUT_S}s)",
                status_code=429,
            )
        _queue_depth[provider] = max(0, _queue_depth.get(provider, 0) - 1)

        _in_flight[provider] = _in_flight.get(provider, 0) + 1
        request["_concurrency_provider"] = provider
        return request

    async def post_call(self, request: dict, response: Any) -> Any:
        provider = request.get("_concurrency_provider")
        if provider:
            _release(provider)
        return response

    async def on_error(self, request: dict, error: Exception) -> None:
        provider = request.get("_concurrency_provider")
        if provider:
            _release(provider)


# ── Status function (for /v1/scillm/health) ─────────────────────────────

def get_concurrency_status() -> Dict[str, Any]:
    """Return current concurrency state for all providers."""
    status = {}
    for provider in PROVIDER_LIMITS:
        limit = PROVIDER_LIMITS[provider]
        current = _in_flight.get(provider, 0)
        queued = _queue_depth.get(provider, 0)
        status[provider] = {
            "limit": limit,
            "in_flight": current,
            "queued": queued,
            "available": limit - current,
            "max_queue": MAX_QUEUE_PER_PROVIDER,
        }
    return status
