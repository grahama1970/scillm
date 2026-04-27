"""Provider-aware concurrency guard with bounded queuing.

Limits concurrent in-flight requests per provider and QUEUES excess
requests until a slot opens. Prevents the 90-second Chutes penalty
when exceeding their 5-connection limit.

Safety bounds:
  - MAX_QUEUE_PER_PROVIDER: reject with 429 when queue exceeds this depth.
  - QUEUE_TIMEOUT_S: queued requests time out after this many seconds.
  - SLOT_MAX_AGE_S: slots held longer than this are considered stale and released.

Persistence:
  - Backoff state (effective limits, 429 history) persists to ArangoDB
  - On restart, loads prior state so we don't hammer rate-limited providers

Migrated from CustomLogger to BaseMiddleware interface.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict

import httpx
from loguru import logger

from scillm.proxy.middleware import BaseMiddleware, MiddlewareReject

# ── ArangoDB persistence ────────────────────────────────────────────────
_MEMORY_URL = os.environ.get("MEMORY_URL", "http://127.0.0.1:8601")
_COLLECTION = "scillm_concurrency_state"
_arango_client: httpx.AsyncClient | None = None


def _get_arango_client() -> httpx.AsyncClient:
    global _arango_client
    if _arango_client is None:
        _arango_client = httpx.AsyncClient(
            base_url=_MEMORY_URL,
            timeout=httpx.Timeout(5.0, connect=2.0),
        )
    return _arango_client

# ── Provider concurrency limits ──────────────────────────────────────────
# Defaults tuned to actual provider RPM limits (not theoretical maximums).
# Override any provider via env: SCILLM_CONCURRENCY_<PROVIDER>=N
# e.g. SCILLM_CONCURRENCY_GEMINI=5
_DEFAULT_LIMITS: Dict[str, int] = {
    "chutes": 4,  # Chutes allows 5 concurrent; 429s now trigger pause + backoff
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
    "opencode-go": 4,  # OpenCode Go provider pool lane
}
DEFAULT_LIMIT = 6


def _load_provider_limits() -> Dict[str, int]:
    """Load provider limits with env var overrides."""
    import os
    limits = dict(_DEFAULT_LIMITS)
    for provider in list(limits.keys()):
        env_key = f"SCILLM_CONCURRENCY_{provider.upper().replace('-', '_')}"
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
QUEUE_TIMEOUT_S = 600.0       # 10 min timeout — let 100+ requests queue and complete without failing

# ── Slot age tracking (zombie slot protection) ───────────────────────────
# If upstream hangs past timeout without raising an exception, slots stay
# occupied forever. Track acquire time and release stale slots.
# Set to 90s to match QUEUE_TIMEOUT_S (60s) + buffer. Was 300s which caused
# 4+ minute zombie delays during batch operations.
SLOT_MAX_AGE_S = 90.0         # Release slots held longer than 90 seconds
_STALE_CHECK_INTERVAL_S = 30.0  # How often to check for stale slots

# ── Batch abuse detection ────────────────────────────────────────────────
# With 10 min timeout, queue can hold ~100 requests without timing out.
# Disable rejection — let requests queue and scillm handles the rate limiting.
QUEUE_WARNING_THRESHOLD = 50   # Warn at 50 queued (for debugging)
QUEUE_REJECT_THRESHOLD = 0     # 0 = disabled — never reject, always queue

# ── Adaptive backpressure ─────────────────────────────────────────────────
# When upstream returns 429, reduce effective concurrency. Recover slowly.
_BACKOFF_WINDOW_S = 60.0      # Count 429s within this window
_BACKOFF_THRESHOLD = 3        # 3 429s in window → halve concurrency (legacy, now immediate)
_RECOVERY_INTERVAL_S = 120.0  # Try restoring 1 slot every 2 minutes
_MIN_CONCURRENCY = 1          # Never go below 1
_DEFAULT_PAUSE_S = 90.0       # Default pause duration on 429 (Chutes penalty is 90s)

_effective_limits: Dict[str, int] = {}  # Current adaptive limit per provider
_rate_limit_hits: Dict[str, list] = {}  # Timestamps of recent 429s
_last_recovery: Dict[str, float] = {}   # Last time we restored a slot
_state_loaded: bool = False             # Whether we've loaded from ArangoDB
_paused_until: Dict[str, float] = {}    # Provider → monotonic timestamp when pause ends

# ── State ────────────────────────────────────────────────────────────────
_semaphores: Dict[str, asyncio.Semaphore] = {}
_in_flight: Dict[str, int] = {}
_queue_depth: Dict[str, int] = {}

# ── Slot age tracking ────────────────────────────────────────────────────
# Track when each slot was acquired: provider → {request_id → acquire_time}
_slot_acquired_at: Dict[str, Dict[str, float]] = {}
_last_stale_check: float = 0.0  # Monotonic time of last stale slot check
_request_counter: int = 0  # Simple counter for unique request IDs


def _new_semaphore_with_reserved_slots(limit: int, reserved_slots: int) -> asyncio.Semaphore:
    """Create a fresh semaphore whose initial availability accounts for in-flight calls."""
    if limit < 0:
        raise ValueError("limit must be non-negative")
    reserved = max(0, min(reserved_slots, limit))
    return asyncio.Semaphore(limit - reserved)


# ── Persistence functions ────────────────────────────────────────────────

async def _persist_state() -> None:
    """Persist backoff state to ArangoDB for restart survival."""
    docs = []
    for provider in PROVIDER_LIMITS:
        configured = PROVIDER_LIMITS[provider]
        effective = _effective_limits.get(provider, configured)
        hits = _rate_limit_hits.get(provider, [])
        last_rec = _last_recovery.get(provider, 0.0)
        # Only persist if state differs from default
        if effective < configured or hits:
            docs.append({
                "_key": f"concurrency_{provider}",
                "provider": provider,
                "effective_limit": effective,
                "configured_limit": configured,
                "rate_limit_hits": hits[-20:],  # Keep last 20 timestamps
                "last_recovery": last_rec,
                "updated_at": time.time(),
            })
    if not docs:
        return
    try:
        client = _get_arango_client()
        resp = await client.post(
            "/upsert",
            json={"collection": _COLLECTION, "documents": docs},
        )
        if resp.status_code >= 400:
            logger.debug("concurrency_guard: persist failed: {}", resp.text[:200])
    except Exception as exc:
        logger.debug("concurrency_guard: persist error: {}", exc)


async def load_state_from_arango() -> None:
    """Load backoff state from ArangoDB on startup."""
    global _state_loaded
    if _state_loaded:
        return
    _state_loaded = True
    try:
        client = _get_arango_client()
        # Query all concurrency state docs
        resp = await client.post(
            "/query",
            json={
                "query": f"FOR doc IN {_COLLECTION} RETURN doc",
            },
        )
        if resp.status_code != 200:
            logger.debug("concurrency_guard: load state query failed: {}", resp.status_code)
            return
        data = resp.json()
        docs = data.get("result", []) or data.get("documents", []) or []
        if not docs:
            logger.info("concurrency_guard: no persisted state found, starting fresh")
            return
        now = time.monotonic()
        restored = 0
        for doc in docs:
            provider = doc.get("provider")
            if not provider or provider not in PROVIDER_LIMITS:
                continue
            effective = doc.get("effective_limit", PROVIDER_LIMITS[provider])
            configured = PROVIDER_LIMITS[provider]
            # Only restore if state is still relevant (updated within last hour)
            updated_at = doc.get("updated_at", 0)
            if time.time() - updated_at > 3600:
                logger.debug("concurrency_guard: {} state stale ({}s old), ignoring",
                           provider, time.time() - updated_at)
                continue
            if effective < configured:
                _effective_limits[provider] = effective
                _semaphores[provider] = asyncio.Semaphore(effective)
                _in_flight[provider] = 0
                _queue_depth[provider] = 0
                restored += 1
                logger.warning(
                    "concurrency_guard: {} restored backoff state — limit {} (configured {})",
                    provider, effective, configured,
                )
            # Restore recent 429 hits (convert stored times to relative)
            hits = doc.get("rate_limit_hits", [])
            if hits:
                # Timestamps were stored as monotonic; approximate relative to now
                _rate_limit_hits[provider] = [now - 30.0 for _ in hits[:5]]  # Assume recent
        if restored:
            logger.info("concurrency_guard: restored backoff state for {} providers", restored)
    except Exception as exc:
        logger.debug("concurrency_guard: load state error: {}", exc)


def _resolve_provider(model: str) -> str:
    """Determine provider key from model name."""
    model_lower = model.lower()

    # OpenCode Go model ids also contain "/", so detect before generic
    # provider/model slash routing to Chutes.
    if model_lower.startswith("opencode-go/"):
        return "opencode-go"

    # ── Chutes detection FIRST (Org/Model format) ─────────────────────────
    # Models like "deepseek-ai/DeepSeek-V3.1-TEE" or "Qwen/Qwen3-30B" are Chutes
    # Must check BEFORE substring matching, otherwise "deepseek" in model_lower
    # would incorrectly resolve to the "deepseek" provider (direct API, not Chutes)
    if "/" in model_lower and not model_lower.startswith("http"):
        return "chutes"

    # ── Common prefix patterns ────────────────────────────────────────────
    if model_lower.startswith("text"):
        return "chutes"
    if model_lower.startswith("local"):
        return "ollama"
    if model_lower.startswith("vlm"):
        return "gemini"  # VLM cascade starts with Gemini

    # ── Substring matching for provider names ─────────────────────────────
    for provider in PROVIDER_LIMITS:
        if provider in model_lower:
            return provider

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


def _record_429(provider: str, retry_after: int | None = None) -> None:
    """Record a 429 hit: immediately pause provider and halve concurrency.

    Args:
        provider: Provider key (e.g., "chutes", "gemini")
        retry_after: Seconds to wait (from Retry-After header), or None for default
    """
    now = time.monotonic()

    # Track hit for recovery logic
    hits = _rate_limit_hits.setdefault(provider, [])
    hits.append(now)
    cutoff = now - _BACKOFF_WINDOW_S
    _rate_limit_hits[provider] = [t for t in hits if t > cutoff]

    # ── Immediate pause ───────────────────────────────────────────────────
    # Block all new requests to this provider until pause expires
    pause_duration = retry_after if retry_after else _DEFAULT_PAUSE_S
    pause_until = now + pause_duration

    # Only extend pause if new pause is longer
    existing_pause = _paused_until.get(provider, 0)
    if pause_until > existing_pause:
        _paused_until[provider] = pause_until
        logger.warning(
            "concurrency_guard: {} PAUSED for {:.0f}s (until {:+.0f}s from now)",
            provider, pause_duration, pause_duration,
        )

    # ── Immediate concurrency reduction ───────────────────────────────────
    # Don't wait for threshold — halve on first 429
    configured = PROVIDER_LIMITS.get(provider, DEFAULT_LIMIT)
    current = _effective_limits.get(provider, configured)
    new_limit = max(_MIN_CONCURRENCY, current // 2)

    if new_limit < current:
        _effective_limits[provider] = new_limit
        _last_recovery[provider] = now

        # CRITICAL: Pre-acquire slots for requests still in flight
        # Without this, new semaphore has all slots available, allowing
        # _in_flight to exceed the limit (race condition causing "9/8 slots" errors)
        current_in_flight = _in_flight.get(provider, 0)
        slots_to_reserve = min(current_in_flight, new_limit)

        logger.warning(
            "concurrency_guard: {} concurrency reduced {} → {} (reserving {} slots for {} in-flight)",
            provider, current, new_limit, slots_to_reserve, current_in_flight,
        )

        _semaphores[provider] = _new_semaphore_with_reserved_slots(new_limit, slots_to_reserve)

        # Persist state change to ArangoDB
        asyncio.create_task(_persist_state())


async def _wait_for_pause(provider: str) -> float:
    """Wait if provider is paused. Returns seconds waited (0 if not paused)."""
    now = time.monotonic()
    pause_until = _paused_until.get(provider, 0)

    if pause_until <= now:
        # Not paused (or pause expired)
        if provider in _paused_until:
            del _paused_until[provider]
        return 0.0

    remaining = pause_until - now
    logger.info(
        "concurrency_guard: {} is PAUSED — waiting {:.1f}s before proceeding",
        provider, remaining,
    )
    await asyncio.sleep(remaining)

    # Clear pause after waiting
    if provider in _paused_until:
        del _paused_until[provider]
    logger.info("concurrency_guard: {} pause expired, resuming", provider)
    return remaining


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

    # Reserve slots for in-flight requests (same fix as _record_429)
    current_in_flight = _in_flight.get(provider, 0)
    slots_to_reserve = min(current_in_flight, new_limit)
    _semaphores[provider] = _new_semaphore_with_reserved_slots(new_limit, slots_to_reserve)

    logger.info(
        "concurrency_guard: {} recovery — no 429s for {:.0f}s, restoring {} → {} (reserved {} for in-flight)",
        provider, _RECOVERY_INTERVAL_S, current, new_limit, slots_to_reserve,
    )
    # Persist recovery state
    asyncio.create_task(_persist_state())


def _generate_request_id() -> str:
    """Generate a unique request ID for slot tracking."""
    global _request_counter
    _request_counter += 1
    return f"req_{_request_counter}_{time.monotonic():.3f}"


def _release_stale_slots() -> int:
    """Release slots held longer than SLOT_MAX_AGE_S. Returns count released."""
    global _last_stale_check
    now = time.monotonic()

    # Rate limit checks to avoid overhead
    if now - _last_stale_check < _STALE_CHECK_INTERVAL_S:
        return 0
    _last_stale_check = now

    released = 0
    cutoff = now - SLOT_MAX_AGE_S

    for provider, slots in list(_slot_acquired_at.items()):
        stale_ids = [req_id for req_id, acquired in slots.items() if acquired < cutoff]
        for req_id in stale_ids:
            acquired = slots[req_id]
            del slots[req_id]
            # Release the semaphore slot
            sem = _semaphores.get(provider)
            if sem:
                sem.release()
                _in_flight[provider] = max(0, _in_flight.get(provider, 1) - 1)
            released += 1
            logger.warning(
                "concurrency_guard: {} released STALE slot {} (held {:.0f}s > {:.0f}s limit)",
                provider, req_id, now - acquired, SLOT_MAX_AGE_S,
            )

    if released:
        logger.warning(
            "concurrency_guard: released {} stale slots total (zombie request cleanup)",
            released,
        )
    return released


def _track_slot_acquire(provider: str, request_id: str) -> None:
    """Track when a slot was acquired for stale detection."""
    if provider not in _slot_acquired_at:
        _slot_acquired_at[provider] = {}
    _slot_acquired_at[provider][request_id] = time.monotonic()


def _track_slot_release(provider: str, request_id: str) -> bool:
    """Remove slot from age tracking on release. Returns true if tracked."""
    if provider in _slot_acquired_at and request_id in _slot_acquired_at[provider]:
        del _slot_acquired_at[provider][request_id]
        return True
    return False


def _release(provider: str, request_id: str | None = None) -> None:
    """Release a semaphore slot and attempt recovery."""
    if request_id and not _track_slot_release(provider, request_id):
        logger.debug(
            "concurrency_guard: {} release skipped for already-released slot {}",
            provider,
            request_id,
        )
        return
    sem = _semaphores.get(provider)
    if sem:
        sem.release()
        _in_flight[provider] = max(0, _in_flight.get(provider, 1) - 1)
    _maybe_recover(provider)


_stale_cleanup_task: asyncio.Task | None = None
_cleanup_restart_count: int = 0
_MAX_CLEANUP_RESTARTS = 10  # Max restarts before giving up


async def _background_stale_cleanup() -> None:
    """Background task to release stale slots every 30s, independent of request flow.

    If this task dies, start_background_cleanup will restart it on the next request.
    """
    global _cleanup_restart_count
    while True:
        try:
            await asyncio.sleep(30.0)
            released = _release_stale_slots_force()
            if released > 0:
                logger.warning("concurrency_guard: background cleanup released {} stale slots", released)
            # Reset restart count on successful iteration
            _cleanup_restart_count = 0
        except asyncio.CancelledError:
            logger.info("concurrency_guard: background cleanup task cancelled")
            raise  # Let the task end cleanly
        except Exception as exc:
            logger.error("concurrency_guard: background cleanup error (will retry): {}", exc)
            # Sleep briefly to avoid spin-looping on persistent errors
            await asyncio.sleep(5.0)


def _release_stale_slots_force() -> int:
    """Release stale slots unconditionally (no rate limit check)."""
    now = time.monotonic()
    cutoff = now - SLOT_MAX_AGE_S
    released = 0

    for provider, slots in list(_slot_acquired_at.items()):
        stale_ids = [req_id for req_id, acquired in slots.items() if acquired < cutoff]
        for req_id in stale_ids:
            acquired = slots[req_id]
            del slots[req_id]
            sem = _semaphores.get(provider)
            if sem:
                sem.release()
                _in_flight[provider] = max(0, _in_flight.get(provider, 1) - 1)
            released += 1
            logger.warning(
                "concurrency_guard: {} released STALE slot {} (held {:.0f}s)",
                provider, req_id, now - acquired,
            )
    return released


def start_background_cleanup() -> None:
    """Start the background stale cleanup task. Restarts if it died."""
    global _stale_cleanup_task, _cleanup_restart_count

    if _stale_cleanup_task is None:
        _stale_cleanup_task = asyncio.create_task(_background_stale_cleanup())
        logger.info("concurrency_guard: started background stale cleanup task")
    elif _stale_cleanup_task.done():
        # Task died - check if we should restart
        _cleanup_restart_count += 1
        if _cleanup_restart_count <= _MAX_CLEANUP_RESTARTS:
            # Log the exception that killed it if any
            exc = _stale_cleanup_task.exception() if not _stale_cleanup_task.cancelled() else None
            if exc:
                logger.error("concurrency_guard: cleanup task died (restart {}/{}): {}",
                            _cleanup_restart_count, _MAX_CLEANUP_RESTARTS, exc)
            _stale_cleanup_task = asyncio.create_task(_background_stale_cleanup())
            logger.warning("concurrency_guard: restarted background cleanup task (restart {}/{})",
                          _cleanup_restart_count, _MAX_CLEANUP_RESTARTS)
        else:
            logger.error("concurrency_guard: cleanup task failed {} times, giving up. "
                        "Zombie slots will not be auto-cleaned!", _MAX_CLEANUP_RESTARTS)


class ConcurrencyMiddleware(BaseMiddleware):
    """Limits concurrent requests per provider. Queues excess with bounds."""

    async def pre_call(self, request: dict) -> dict | None:
        # Ensure background cleanup is running
        start_background_cleanup()
        model = request.get("model", "")
        provider = _resolve_provider(model)

        # ── Cleanup stale slots (zombie request protection) ───────────────
        # Releases slots held longer than SLOT_MAX_AGE_S (hung requests)
        _release_stale_slots()

        # ── Check for pause (429 backoff in progress) ─────────────────────
        # If provider is paused due to recent 429, wait before proceeding
        pause_waited = await _wait_for_pause(provider)
        if pause_waited > 0:
            request["_concurrency_pause_wait_s"] = pause_waited

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
        queue_start = time.monotonic()
        try:
            await asyncio.wait_for(sem.acquire(), timeout=QUEUE_TIMEOUT_S)
        except asyncio.TimeoutError:
            _queue_depth[provider] = max(0, _queue_depth.get(provider, 0) - 1)
            current_queued = _queue_depth.get(provider, 0)
            effective = _effective_limits.get(provider, limit)
            in_flight = _in_flight.get(provider, 0)
            logger.warning(
                "concurrency_guard: {} queue timeout after {:.0f}s ({} still queued, {} in-flight, limit {})",
                provider, QUEUE_TIMEOUT_S, current_queued, in_flight, effective,
            )
            # REJECT with 503 (Service Unavailable) — semantically correct for capacity
            # exhaustion. 429 means "you're sending too fast" which isn't true here;
            # the proxy is simply overloaded processing other requests.

            # Build informative message based on queue state
            if in_flight >= effective and current_queued > 0:
                # All slots full + others queued = another batch job is running
                msg = (
                    f"SERVICE_BUSY: {provider} capacity exhausted — {in_flight}/{effective} slots in use, "
                    f"{current_queued} requests still queued. Request waited {QUEUE_TIMEOUT_S:.0f}s. "
                    f"For large batches (50+), use chunked processing (CHUNK_SIZE=4) to avoid queue buildup. "
                    f"See SKILL.md 'Large batches (50+)' section."
                )
            elif in_flight >= effective:
                # All slots full, no queue = you're competing with concurrent requests
                msg = (
                    f"SERVICE_BUSY: {provider} at capacity — {in_flight}/{effective} slots in use. "
                    f"Request waited {QUEUE_TIMEOUT_S:.0f}s. Retry in 30-60s or use chunked processing."
                )
            else:
                # Shouldn't happen, but fallback message
                msg = (
                    f"SERVICE_BUSY: {provider} queue timeout ({in_flight}/{effective} slots). "
                    f"Request waited {QUEUE_TIMEOUT_S:.0f}s. Retry with backoff."
                )

            raise MiddlewareReject(msg, status_code=503)
        except BaseException:
            _queue_depth[provider] = max(0, _queue_depth.get(provider, 0) - 1)
            raise

        queue_wait_ms = int((time.monotonic() - queue_start) * 1000)
        _queue_depth[provider] = max(0, _queue_depth.get(provider, 0) - 1)

        # Track slot acquisition for stale detection
        request_id = _generate_request_id()
        _track_slot_acquire(provider, request_id)

        _in_flight[provider] = _in_flight.get(provider, 0) + 1
        request["_concurrency_provider"] = provider
        request["_concurrency_request_id"] = request_id
        request["_concurrency_queue_wait_ms"] = queue_wait_ms

        # Compute backoff status for response headers
        configured = PROVIDER_LIMITS.get(provider, DEFAULT_LIMIT)
        effective = _effective_limits.get(provider, configured)
        request["_concurrency_backoff_active"] = effective < configured
        request["_concurrency_queue_depth"] = _queue_depth.get(provider, 0)

        return request

    async def post_call(self, request: dict, response: Any) -> Any:
        # Don't release if we bypassed the semaphore (queue timeout)
        if request.get("_concurrency_bypassed"):
            return response
        provider = request.get("_concurrency_provider")
        request_id = request.get("_concurrency_request_id")
        if provider:
            _release(provider, request_id)

            # ── Detect 429 in response and trigger pause ──────────────────────
            # 429s come as HTTP responses, not exceptions, so check here
            status = None
            retry_after = None
            if isinstance(response, dict):
                # Check for error status in response dict
                status = response.get("status_code") or response.get("status")
                if not status and response.get("error"):
                    err = response.get("error", {})
                    if isinstance(err, dict):
                        status = err.get("status_code") or err.get("code")
                    elif "429" in str(err):
                        status = 429
            elif hasattr(response, "status_code"):
                status = response.status_code
            elif hasattr(response, "status"):
                status = response.status

            if status == 429:
                # Extract Retry-After if available
                if hasattr(response, "headers"):
                    ra = response.headers.get("Retry-After") or response.headers.get("retry-after")
                    if ra:
                        try:
                            retry_after = int(ra)
                        except ValueError:
                            pass
                _record_429(provider, retry_after)
                logger.warning(
                    "concurrency_guard: {} got 429 in response — triggering pause",
                    provider,
                )

        # Add concurrency info headers to response for agent visibility
        queue_wait_ms = request.get("_concurrency_queue_wait_ms", 0)
        queue_depth = request.get("_concurrency_queue_depth", 0)
        backoff_active = request.get("_concurrency_backoff_active", False)
        warning = request.get("_concurrency_queue_warning")

        # Inject headers into response (works for both dict and object responses)
        headers = {
            "x-scillm-queue-wait-ms": str(queue_wait_ms),
            "x-scillm-queue-depth": str(queue_depth),
            "x-scillm-backoff-active": "true" if backoff_active else "false",
        }
        if warning:
            headers["x-scillm-queue-warning"] = warning

        if isinstance(response, dict):
            existing = response.get("_hidden_params", {}).get("additional_headers", {})
            existing.update(headers)
            if "_hidden_params" not in response:
                response["_hidden_params"] = {}
            response["_hidden_params"]["additional_headers"] = existing
        elif hasattr(response, "_hidden_params"):
            existing = getattr(response._hidden_params, "additional_headers", {}) or {}
            existing.update(headers)
            response._hidden_params["additional_headers"] = existing

        return response

    async def on_error(self, request: dict, error: Exception) -> None:
        # Don't release if we bypassed the semaphore (queue timeout)
        if request.get("_concurrency_bypassed"):
            return
        provider = request.get("_concurrency_provider")
        request_id = request.get("_concurrency_request_id")
        if provider:
            _release(provider, request_id)
            # Detect 429 from upstream and trigger adaptive backoff
            err_str = str(error).lower()
            status = getattr(error, "status_code", 0) or getattr(error, "status", 0)
            if status == 429 or "rate" in err_str and "limit" in err_str or "429" in err_str:
                # Try to extract Retry-After header from error
                retry_after = None
                headers = getattr(error, "headers", None) or getattr(error, "response_headers", None)
                if headers:
                    ra = headers.get("Retry-After") or headers.get("retry-after")
                    if ra:
                        try:
                            retry_after = int(ra)
                        except ValueError:
                            pass
                _record_429(provider, retry_after)


# ── Status function (for /v1/scillm/health) ─────────────────────────────

def get_concurrency_status() -> Dict[str, Any]:
    """Return current concurrency state for all providers."""
    now = time.monotonic()
    status = {}
    registry_counts: dict[str, int] = {}
    stale_counts: dict[str, int] = {}
    try:
        from chutes.middleware.active_calls import (
            get_active_counts_by_provider,
            get_stale_counts_by_provider,
        )
        registry_counts = get_active_counts_by_provider()
        stale_counts = get_stale_counts_by_provider()
    except Exception as exc:
        logger.debug("concurrency_guard: active registry unavailable for drift check: {}", exc)

    for provider in PROVIDER_LIMITS:
        configured = PROVIDER_LIMITS[provider]
        effective = _effective_limits.get(provider, configured)
        semaphore_in_flight = _in_flight.get(provider, 0)
        registry_in_flight = registry_counts.get(provider, 0)
        stale_active_calls = stale_counts.get(provider, 0)
        drift = registry_in_flight - semaphore_in_flight
        queued = _queue_depth.get(provider, 0)
        hits = _rate_limit_hits.get(provider, [])
        recent_429s = len([t for t in hits if t > now - _BACKOFF_WINDOW_S])

        # Pause info
        pause_until = _paused_until.get(provider, 0)
        paused = pause_until > now
        pause_remaining = max(0, pause_until - now) if paused else 0

        # Slot age info (zombie detection)
        slots = _slot_acquired_at.get(provider, {})
        oldest_slot_age_s = 0.0
        stale_warning_count = 0  # Slots > 80% of SLOT_MAX_AGE_S
        if slots:
            ages = [now - acquired for acquired in slots.values()]
            oldest_slot_age_s = max(ages)
            stale_warning_threshold = SLOT_MAX_AGE_S * 0.8
            stale_warning_count = len([a for a in ages if a > stale_warning_threshold])

        status[provider] = {
            "configured_limit": configured,
            "effective_limit": effective,
            "in_flight": semaphore_in_flight,
            "actual_in_flight": semaphore_in_flight,
            "live_in_flight": semaphore_in_flight,
            "semaphore_in_flight": semaphore_in_flight,
            "registry_in_flight": registry_in_flight,
            "stale_active_calls": stale_active_calls,
            "registry_drift": drift,
            "drift": drift,
            "queued": queued,
            "available": effective - semaphore_in_flight,
            "max_queue": MAX_QUEUE_PER_PROVIDER,
            "recent_429s": recent_429s,
            "backoff_active": effective < configured,
            "paused": paused,
            "pause_remaining_s": round(pause_remaining, 1),
            "oldest_slot_age_s": round(oldest_slot_age_s, 1),
            "stale_warning_count": stale_warning_count,
        }
    return status


def reset_concurrency(provider: str | None = None) -> Dict[str, Any]:
    """Reset concurrency state — clears queue, resets semaphores.

    Use when batch failures have corrupted state or queue is stuck.
    If provider specified, only reset that provider. Otherwise reset all.

    Returns summary of what was cleared.
    """
    global _semaphores, _in_flight, _queue_depth, _slot_acquired_at
    global _effective_limits, _rate_limit_hits, _paused_until

    cleared = {
        "providers_reset": [],
        "slots_cleared": 0,
        "queue_cleared": 0,
        "pauses_cleared": 0,
    }

    providers_to_reset = [provider] if provider else list(PROVIDER_LIMITS.keys())

    for p in providers_to_reset:
        if p not in PROVIDER_LIMITS:
            continue

        # Clear in-flight count
        in_flight = _in_flight.get(p, 0)
        if in_flight > 0:
            cleared["slots_cleared"] += in_flight
            _in_flight[p] = 0

        # Clear queue depth
        queued = _queue_depth.get(p, 0)
        if queued > 0:
            cleared["queue_cleared"] += queued
            _queue_depth[p] = 0

        # Reset semaphore to configured limit
        configured = PROVIDER_LIMITS.get(p, DEFAULT_LIMIT)
        _semaphores[p] = asyncio.Semaphore(configured)
        _effective_limits[p] = configured

        # Clear slot tracking
        if p in _slot_acquired_at:
            del _slot_acquired_at[p]

        # Clear rate limit history and pause
        if p in _rate_limit_hits:
            del _rate_limit_hits[p]
        if p in _paused_until:
            cleared["pauses_cleared"] += 1
            del _paused_until[p]

        cleared["providers_reset"].append(p)
        logger.info(
            "concurrency_guard: RESET {} — cleared {} in-flight, {} queued",
            p, in_flight, queued,
        )

    # Persist the reset state
    asyncio.create_task(_persist_state())

    return cleared


def get_concurrency_for_model(model: str) -> Dict[str, Any]:
    """Return concurrency info for a specific model (for batch sizing).

    Skills call this to determine optimal chunk_size for batch processing.
    Returns effective_limit (accounting for adaptive backoff) as chunk_size.
    If provider is paused, chunk_size = 0 and pause_remaining_s indicates wait time.
    """
    now = time.monotonic()
    provider = _resolve_provider(model)
    configured = PROVIDER_LIMITS.get(provider, DEFAULT_LIMIT)
    effective = _effective_limits.get(provider, configured)
    current = _in_flight.get(provider, 0)
    queued = _queue_depth.get(provider, 0)

    # Pause info
    pause_until = _paused_until.get(provider, 0)
    paused = pause_until > now
    pause_remaining = max(0, pause_until - now) if paused else 0

    # If paused, report chunk_size = 0 so skills know to wait
    chunk_size = 0 if paused else effective

    return {
        "model": model,
        "provider": provider,
        "chunk_size": chunk_size,  # 0 if paused, else effective limit
        "configured_limit": configured,
        "effective_limit": effective,
        "in_flight": current,
        "queued": queued,
        "available": max(0, effective - current),
        "backoff_active": effective < configured,
        "paused": paused,
        "pause_remaining_s": round(pause_remaining, 1),
    }
