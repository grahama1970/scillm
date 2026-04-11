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
import time
from typing import Any, Dict

from loguru import logger

from scillm.proxy.middleware import BaseMiddleware, MiddlewareReject

# ── Provider concurrency limits ──────────────────────────────────────────
# Defaults tuned to actual provider RPM limits (not theoretical maximums).
# Override any provider via env: SCILLM_CONCURRENCY_<PROVIDER>=N
# e.g. SCILLM_CONCURRENCY_GEMINI=5
_DEFAULT_LIMITS: Dict[str, int] = {
    "chutes": 1,  # Serialize Chutes calls - concurrent causes timeouts
    "ollama": 1,
    "moonshot": 3,
    "deepseek": 8,
    "gemini": 2,       # Google free tier: ~2 RPM. Was 10, caused 563 429s/942 questions.
    "openrouter": 6,
    "certainly": 1,
    "codeworld": 1,
    "claude": 2,       # Claude OAuth - RPM-based limits (50 RPM Tier 1), not concurrency
    "anthropic": 2,    # Same as claude - adaptive backoff adjusts if 429s occur
    "codex": 8,        # Codex Pro OAuth - OpenAI tier ~10K RPM, using 8 concurrent
    "openai": 8,       # Standard OpenAI
}
DEFAULT_LIMIT = 6


def _load_provider_limits() -> Dict[str, int]:
    """Load provider limits with env var overrides."""
    import os
    limits = dict(_DEFAULT_LIMITS)
    for provider in list(limits.keys()):
        env_key = f"SCILLM_CONCURRENCY_{provider.upper()}"
        env_val = os.environ.get(env_key)
        if env_val is not None:
            try:
                limits[provider] = int(env_val)
                logger.info("concurrency_guard: {} override from env {}={}", provider, env_key, env_val)
            except ValueError:
                logger.warning("concurrency_guard: invalid {} value '{}', using default {}", env_key, env_val, limits[provider])
    return limits


PROVIDER_LIMITS: Dict[str, int] = _load_provider_limits()

# ── Queue safety bounds ──────────────────────────────────────────────────
MAX_QUEUE_PER_PROVIDER = 0    # 0 = unlimited queue depth (no rejection)
QUEUE_TIMEOUT_S = 300.0       # Queued requests time out after 5min (ZIP+thinking models need more)

# ── Batch abuse detection ────────────────────────────────────────────────
# Detect when callers fire too many requests at once (common agent mistake).
# These thresholds trigger warnings/rejections with helpful guidance.
QUEUE_WARNING_THRESHOLD = 20   # Add warning header when queue exceeds this
QUEUE_REJECT_THRESHOLD = 100   # Reject with helpful 429 when queue exceeds this

# ── Adaptive backpressure ─────────────────────────────────────────────────
# When upstream returns 429, reduce effective concurrency. Recover slowly.
_BACKOFF_WINDOW_S = 60.0      # Count 429s within this window
_BACKOFF_THRESHOLD = 3        # 3 429s in window → halve concurrency
_RECOVERY_INTERVAL_S = 120.0  # Try restoring 1 slot every 2 minutes
_MIN_CONCURRENCY = 1          # Never go below 1

_effective_limits: Dict[str, int] = {}  # Current adaptive limit per provider
_rate_limit_hits: Dict[str, list] = {}  # Timestamps of recent 429s
_last_recovery: Dict[str, float] = {}   # Last time we restored a slot

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


def _record_429(provider: str) -> None:
    """Record a 429 hit and reduce concurrency if threshold exceeded."""
    now = time.monotonic()
    hits = _rate_limit_hits.setdefault(provider, [])
    hits.append(now)
    # Prune old hits outside the window
    cutoff = now - _BACKOFF_WINDOW_S
    _rate_limit_hits[provider] = [t for t in hits if t > cutoff]

    recent = len(_rate_limit_hits[provider])
    if recent >= _BACKOFF_THRESHOLD:
        configured = PROVIDER_LIMITS.get(provider, DEFAULT_LIMIT)
        current = _effective_limits.get(provider, configured)
        new_limit = max(_MIN_CONCURRENCY, current // 2)
        if new_limit < current:
            _effective_limits[provider] = new_limit
            _last_recovery[provider] = now
            logger.warning(
                "concurrency_guard: {} adaptive backoff — {} 429s in {:.0f}s, reducing {} → {}",
                provider, recent, _BACKOFF_WINDOW_S, current, new_limit,
            )
            # Recreate semaphore with lower limit (existing in-flight will drain)
            _semaphores[provider] = asyncio.Semaphore(new_limit)


def _maybe_recover(provider: str) -> None:
    """Slowly restore concurrency if no recent 429s."""
    now = time.monotonic()
    configured = PROVIDER_LIMITS.get(provider, DEFAULT_LIMIT)
    current = _effective_limits.get(provider, configured)
    if current >= configured:
        return  # Already at max

    last = _last_recovery.get(provider, 0.0)
    if now - last < _RECOVERY_INTERVAL_S:
        return  # Too soon

    # Check no 429s in the last window
    hits = _rate_limit_hits.get(provider, [])
    cutoff = now - _BACKOFF_WINDOW_S
    recent = len([t for t in hits if t > cutoff])
    if recent > 0:
        return  # Still getting 429s

    new_limit = min(configured, current + 1)
    _effective_limits[provider] = new_limit
    _last_recovery[provider] = now
    _semaphores[provider] = asyncio.Semaphore(new_limit)
    logger.info(
        "concurrency_guard: {} recovery — no 429s for {:.0f}s, restoring {} → {}",
        provider, _RECOVERY_INTERVAL_S, current, new_limit,
    )


def _release(provider: str) -> None:
    """Release a semaphore slot and attempt recovery."""
    sem = _semaphores.get(provider)
    if sem:
        sem.release()
        _in_flight[provider] = max(0, _in_flight.get(provider, 1) - 1)
    _maybe_recover(provider)


class ConcurrencyMiddleware(BaseMiddleware):
    """Limits concurrent requests per provider. Queues excess with bounds."""

    async def pre_call(self, request: dict) -> dict | None:
        model = request.get("model", "")
        provider = _resolve_provider(model)
        sem = _get_semaphore(provider)
        limit = PROVIDER_LIMITS.get(provider, DEFAULT_LIMIT)

        # Check queue depth before attempting acquire
        queued = _queue_depth.get(provider, 0)

        # ── Batch abuse detection ─────────────────────────────────────────
        # Reject with helpful message when queue is dangerously high
        if QUEUE_REJECT_THRESHOLD > 0 and queued >= QUEUE_REJECT_THRESHOLD:
            logger.warning(
                "concurrency_guard: {} queue overloaded ({} queued), rejecting with batch guidance",
                provider, queued,
            )
            raise MiddlewareReject(
                f"BATCH MISUSE: {queued} requests queued for {provider} (limit {limit} concurrent). "
                f"You are firing too many requests at once. Use chunked processing: "
                f"process {limit} requests at a time, wait for completion, then send the next batch. "
                f"See SKILL.md 'Batch Calls' section. "
                f"Example: for i in range(0, len(prompts), {limit}): await asyncio.gather(*chunk)",
                status_code=429,
            )

        # Warn when queue is getting high (request will still be queued)
        if QUEUE_WARNING_THRESHOLD > 0 and queued >= QUEUE_WARNING_THRESHOLD:
            logger.warning(
                "concurrency_guard: {} queue high ({} queued) — caller should use chunked batching",
                provider, queued,
            )
            # Tag request so post_call can add warning header
            request["_concurrency_queue_warning"] = (
                f"High queue depth ({queued}). Consider chunked batching to avoid timeouts. "
                f"See /v1/scillm/health for current queue status."
            )

        # Legacy: reject if hard queue limit exceeded
        if MAX_QUEUE_PER_PROVIDER > 0 and queued >= MAX_QUEUE_PER_PROVIDER:
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
            current_queued = _queue_depth.get(provider, 0)
            logger.warning(
                "concurrency_guard: {} queue timeout after {:.0f}s (still {} queued)",
                provider, QUEUE_TIMEOUT_S, current_queued,
            )
            raise MiddlewareReject(
                f"QUEUE TIMEOUT: Request waited {QUEUE_TIMEOUT_S}s for a {provider} slot "
                f"(limit {limit} concurrent, {current_queued} still queued). "
                f"This usually means you fired too many requests at once. "
                f"Use chunked batching: process {limit} requests, wait, then send more. "
                f"See SKILL.md 'Batch Calls' section.",
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
            # Detect 429 from upstream and trigger adaptive backoff
            err_str = str(error).lower()
            status = getattr(error, "status_code", 0) or getattr(error, "status", 0)
            if status == 429 or "rate" in err_str and "limit" in err_str or "429" in err_str:
                _record_429(provider)


# ── Status function (for /v1/scillm/health) ─────────────────────────────

def get_concurrency_status() -> Dict[str, Any]:
    """Return current concurrency state for all providers."""
    now = time.monotonic()
    status = {}
    for provider in PROVIDER_LIMITS:
        configured = PROVIDER_LIMITS[provider]
        effective = _effective_limits.get(provider, configured)
        current = _in_flight.get(provider, 0)
        queued = _queue_depth.get(provider, 0)
        hits = _rate_limit_hits.get(provider, [])
        recent_429s = len([t for t in hits if t > now - _BACKOFF_WINDOW_S])
        status[provider] = {
            "configured_limit": configured,
            "effective_limit": effective,
            "in_flight": current,
            "queued": queued,
            "available": effective - current,
            "max_queue": MAX_QUEUE_PER_PROVIDER,
            "recent_429s": recent_429s,
            "backoff_active": effective < configured,
        }
    return status
