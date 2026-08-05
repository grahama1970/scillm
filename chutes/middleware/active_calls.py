"""Active calls tracker — exposes in-flight LLM requests for live monitoring.

Tracks requests from pre_call to post_call/on_error, enabling dashboards
to show currently running calls with elapsed time and activity graphs.
"""
from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import asyncio
from typing import Any

from loguru import logger

from scillm.proxy.middleware import BaseMiddleware


ACTIVE_CALL_DEFAULT_TTL_S = 900.0
ACTIVE_CALL_STALE_GRACE_S = 5.0
ACTIVE_CALL_JANITOR_INTERVAL_S = 5.0


@dataclass
class ActiveCall:
    """A currently in-flight LLM call."""
    call_id: str
    model: str
    caller: str
    started_at: float  # time.monotonic()
    started_ts: str    # ISO timestamp
    provider: str = ""
    stream: bool = False
    timeout_s: float | None = None
    dynamic_timeout_s: float | None = None
    timeout_source: str = ""
    stream_heartbeat_s: float | None = None
    stream_progress_events: bool = False
    pool: str = ""
    lane: str = ""

    def stale_after_s(self, default_ttl_s: float = ACTIVE_CALL_DEFAULT_TTL_S) -> float:
        if self.timeout_s is None:
            return default_ttl_s
        return max(0.0, float(self.timeout_s)) + ACTIVE_CALL_STALE_GRACE_S

    def to_dict(self, *, live_status: str = "live") -> dict:
        elapsed_ms = round((time.monotonic() - self.started_at) * 1000)
        duration_s = round(elapsed_ms / 1000.0, 3)
        return {
            "call_id": self.call_id,
            "model": self.model,
            "model_requested": self.model,
            # The definitive served model is known only after provider return.
            # While in flight, expose the selected/requested route so operators
            # can identify the stalled lane from /active-calls alone.
            "model_served": self.model,
            "caller": self.caller,
            "provider": self.provider,
            "stream": self.stream,
            "timeout_s": self.timeout_s,
            "deadline_timeout_s": self.timeout_s,
            "dynamic_timeout_s": self.dynamic_timeout_s,
            "timeout_source": self.timeout_source,
            "stream_heartbeat_s": self.stream_heartbeat_s,
            "stream_progress_events": self.stream_progress_events,
            "pool": self.pool,
            "lane": self.lane,
            "started_ts": self.started_ts,
            "created_at": self.started_ts,
            "elapsed_ms": elapsed_ms,
            "duration_s": duration_s,
            "live_status": live_status,
            "stale_after_s": self.stale_after_s(),
        }


@dataclass
class CompletedCall:
    """A recently completed call for activity graph."""
    ts: float  # time.time() when completed
    model: str
    duration_ms: int
    success: bool
    provider: str


# Global registry of active calls
_active_calls: dict[str, ActiveCall] = {}
_lock = asyncio.Lock()
_janitor_task: asyncio.Task | None = None

# Rolling window of completed calls for activity graph (last 5 minutes)
_completed_calls: deque[CompletedCall] = deque(maxlen=1000)
_stale_calls: deque[dict] = deque(maxlen=1000)
_WINDOW_SECONDS = 300  # 5 minutes


def get_active_calls() -> list[dict]:
    """Return list of all active calls (for /v1/scillm/active-calls endpoint)."""
    evict_stale_active_calls()
    # Snapshot copy - safe without lock for read-only access
    calls = list(_active_calls.values())
    return [c.to_dict() for c in calls]


def get_recent_stale_active_calls(window_s: float = _WINDOW_SECONDS) -> list[dict]:
    """Return recently evicted stale rows for diagnostics only."""
    now = time.monotonic()
    return [
        dict(call)
        for call in list(_stale_calls)
        if now - float(call.get("stale_evicted_at_monotonic", 0.0)) <= window_s
    ]


def get_active_count() -> int:
    """Return count of active calls."""
    evict_stale_active_calls()
    # Dict len is atomic, no lock needed for read
    return len(_active_calls)


def get_active_counts_by_provider() -> dict[str, int]:
    """Return live active-call counts grouped by provider."""
    evict_stale_active_calls()
    counts: dict[str, int] = {}
    for call in list(_active_calls.values()):
        provider = call.provider or "unknown"
        counts[provider] = counts.get(provider, 0) + 1
    return counts


def get_stale_counts_by_provider(window_s: float = _WINDOW_SECONDS) -> dict[str, int]:
    """Return recent stale-call counts grouped by provider for diagnostics."""
    counts: dict[str, int] = {}
    for call in get_recent_stale_active_calls(window_s=window_s):
        provider = str(call.get("provider") or "unknown")
        counts[provider] = counts.get(provider, 0) + 1
    return counts


def get_active_call_diagnostics_by_provider(window_s: float = _WINDOW_SECONDS) -> dict[str, dict[str, int]]:
    """Return live and stale registry counts grouped by provider."""
    live_counts = get_active_counts_by_provider()
    stale_counts = get_stale_counts_by_provider(window_s=window_s)
    providers = set(live_counts) | set(stale_counts)
    return {
        provider: {
            "live_in_flight": live_counts.get(provider, 0),
            "stale_active_calls": stale_counts.get(provider, 0),
        }
        for provider in providers
    }


def evict_stale_active_calls(default_ttl_s: float = ACTIVE_CALL_DEFAULT_TTL_S) -> list[dict]:
    """Evict active-call records that outlive their request timeout plus grace.

    Active calls are only an in-memory live registry. If a request is cancelled
    or an exception path misses middleware cleanup, stale rows must not survive
    indefinitely and distort dashboards.
    """
    now = time.monotonic()
    evicted: list[dict] = []
    for call_id, call in list(_active_calls.items()):
        ttl = call.stale_after_s(default_ttl_s)
        age_s = now - call.started_at
        if age_s <= ttl:
            continue
        removed = _active_calls.pop(call_id, None)
        if removed is None:
            continue
        record = removed.to_dict(live_status="stale")
        record["ttl_s"] = ttl
        record["stale_evicted_at_monotonic"] = now
        record["stale_evicted_ts"] = datetime.now(timezone.utc).isoformat()
        evicted.append(record)
        _stale_calls.append(record)
        logger.warning(
            "active_calls: evicted stale call provider={} model={} call_id={} age_s={:.1f} ttl_s={:.1f}",
            removed.provider,
            removed.model,
            removed.call_id,
            age_s,
            ttl,
        )
    return evicted


def clear_active_calls(
    *,
    older_than_s: float | None = None,
    caller: str | None = None,
    model_contains: str | None = None,
) -> list[dict]:
    """Purge matching active/stale-call records and return removed rows."""
    now = time.monotonic()
    removed: list[dict] = []
    caller_filter = caller.lower() if caller else None
    model_filter = model_contains.lower() if model_contains else None

    def matches(call: ActiveCall | dict) -> bool:
        if isinstance(call, ActiveCall):
            age_s = now - call.started_at
            caller_value = call.caller
            model_value = call.model
        else:
            age_s = float(call.get("elapsed_ms", 0.0)) / 1000.0
            caller_value = str(call.get("caller") or "")
            model_value = str(call.get("model") or "")
        if older_than_s is not None and age_s <= older_than_s:
            return False
        if caller_filter and caller_filter not in caller_value.lower():
            return False
        return not (model_filter and model_filter not in model_value.lower())

    for call_id, call in list(_active_calls.items()):
        if not matches(call):
            continue
        popped = _active_calls.pop(call_id, None)
        if popped:
            removed.append(popped.to_dict())
            logger.warning(
                "active_calls: purged call provider={} model={} call_id={} caller={}",
                popped.provider,
                popped.model,
                popped.call_id,
                popped.caller,
            )

    retained_stale: deque[dict] = deque(maxlen=_stale_calls.maxlen)
    for call in list(_stale_calls):
        if matches(call):
            removed.append(dict(call))
            logger.warning(
                "active_calls: purged stale call provider={} model={} call_id={} caller={}",
                call.get("provider"),
                call.get("model"),
                call.get("call_id"),
                call.get("caller"),
            )
        else:
            retained_stale.append(call)
    _stale_calls.clear()
    _stale_calls.extend(retained_stale)
    return removed


def get_activity_graph(bucket_seconds: int = 10) -> dict:
    """Return activity data bucketed for graphing.

    Returns buckets for the last 5 minutes with:
    - timestamp (bucket start)
    - total calls completed in bucket
    - success count
    - error count
    - avg latency
    """
    now = time.time()
    cutoff = now - _WINDOW_SECONDS

    # Create buckets
    num_buckets = _WINDOW_SECONDS // bucket_seconds
    buckets: list[dict] = []

    for i in range(num_buckets):
        bucket_start = now - (num_buckets - i) * bucket_seconds
        buckets.append({
            "ts": int(bucket_start * 1000),  # JS timestamp
            "total": 0,
            "success": 0,
            "error": 0,
            "latency_sum": 0,
        })

    # Snapshot copy for read-only access - no lock needed
    # Note: deque iteration is thread-safe for reads
    completed_snapshot = list(_completed_calls)

    # Bucket the calls
    for call in completed_snapshot:
        if call.ts < cutoff:
            continue  # Skip old entries
        bucket_idx = int((call.ts - (now - _WINDOW_SECONDS)) / bucket_seconds)
        if 0 <= bucket_idx < num_buckets:
            buckets[bucket_idx]["total"] += 1
            if call.success:
                buckets[bucket_idx]["success"] += 1
            else:
                buckets[bucket_idx]["error"] += 1
            buckets[bucket_idx]["latency_sum"] += call.duration_ms

    evict_stale_active_calls()
    active_count = len(_active_calls)

    # Compute avg latency
    for b in buckets:
        if b["total"] > 0:
            b["avg_latency_ms"] = round(b["latency_sum"] / b["total"])
        else:
            b["avg_latency_ms"] = 0
        del b["latency_sum"]

    return {
        "buckets": buckets,
        "bucket_seconds": bucket_seconds,
        "active_count": active_count,
        "window_seconds": _WINDOW_SECONDS,
    }


async def _janitor_loop(interval_s: float) -> None:
    try:
        while True:
            evicted = evict_stale_active_calls()
            if evicted:
                logger.warning("active_calls: janitor evicted {} stale call(s)", len(evicted))
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        logger.info("active_calls: janitor stopped")
        raise


def start_active_call_janitor(interval_s: float = ACTIVE_CALL_JANITOR_INTERVAL_S) -> None:
    """Start a background janitor that keeps live accounting clean."""
    global _janitor_task
    if _janitor_task is not None and not _janitor_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _janitor_task = loop.create_task(_janitor_loop(interval_s))
    logger.info("active_calls: janitor started (interval={}s)", interval_s)


def _infer_provider(model: str) -> str:
    """Infer provider from model name."""
    m = model.lower()
    if m.startswith("opencode-go/") or m in {"oc-kimi", "oc-glm", "oc-qwen", "oc-deepseek"}:
        return "opencode-go"
    if "/" in m and not m.startswith("http"):
        return "chutes"
    if "deepseek" in m:
        return "chutes" if "v3" in m or "r1" in m else "deepseek"
    if "gemini" in m or "flash" in m:
        return "google"
    if "qwen" in m:
        return "chutes"
    if "claude" in m:
        return "anthropic"
    if "gpt" in m or "codex" in m:
        return "openai"
    return "unknown"


class ActiveCallsMiddleware(BaseMiddleware):
    """Track in-flight requests for live monitoring."""

    async def pre_call(self, request: dict) -> dict:
        call_id = str(uuid.uuid4())[:8]
        request["_active_call_id"] = call_id

        # Extract caller from headers
        headers = request.get("_headers", {})
        caller = request.get("_caller_skill", "") or headers.get("x-caller-skill", "")

        call = ActiveCall(
            call_id=call_id,
            model=request.get("model", ""),
            caller=caller or "unknown",
            started_at=time.monotonic(),
            started_ts=datetime.now(timezone.utc).isoformat(),
            provider=str(request.get("_concurrency_provider") or _infer_provider(request.get("model", ""))),
            stream=bool(request.get("stream")),
            timeout_s=_request_timeout_s(request),
            dynamic_timeout_s=_dynamic_timeout_s(request),
            timeout_source=str(request.get("_timeout_source") or ""),
            stream_heartbeat_s=_stream_heartbeat_s(request),
            stream_progress_events=bool(request.get("stream_progress_events") or request.get("progress_events")),
            pool=str(request.get("_scillm_pool") or ""),
            lane=str(request.get("_scillm_pool_lane") or ""),
        )

        async with _lock:
            _active_calls[call_id] = call

        return request

    async def post_call(self, request: dict, response: Any) -> Any:
        call_id = request.get("_active_call_id")
        if call_id:
            async with _lock:
                active = _active_calls.pop(call_id, None)
                if active:
                    duration_ms = round((time.monotonic() - active.started_at) * 1000)
                    _completed_calls.append(CompletedCall(
                        ts=time.time(),
                        model=active.model,
                        duration_ms=duration_ms,
                        success=True,
                        provider=active.provider,
                    ))
        return response

    async def on_error(self, request: dict, error: Exception) -> None:
        call_id = request.get("_active_call_id")
        if call_id:
            async with _lock:
                active = _active_calls.pop(call_id, None)
                if active:
                    duration_ms = round((time.monotonic() - active.started_at) * 1000)
                    _completed_calls.append(CompletedCall(
                        ts=time.time(),
                        model=active.model,
                        duration_ms=duration_ms,
                        success=False,
                        provider=active.provider,
                    ))


def _request_timeout_s(request: dict) -> float | None:
    explicit_timeout = request.get("timeout")
    if explicit_timeout is not None:
        try:
            return float(explicit_timeout)
        except (TypeError, ValueError):
            return None

    timeout = None
    if "_dynamic_timeout_ms" in request:
        timeout = float(request["_dynamic_timeout_ms"]) / 1000.0
    if _infer_provider(str(request.get("model", ""))) == "opencode-go":
        timeout = max(float(timeout or 0), 600.0)
    try:
        return float(timeout) if timeout is not None else None
    except (TypeError, ValueError):
        return None


def _dynamic_timeout_s(request: dict) -> float | None:
    try:
        if "_dynamic_timeout_ms" in request:
            return float(request["_dynamic_timeout_ms"]) / 1000.0
    except (TypeError, ValueError):
        return None
    return None


def _stream_heartbeat_s(request: dict) -> float | None:
    for key in ("stream_heartbeat_s", "heartbeat_interval_s", "idle_timeout", "read_timeout"):
        try:
            value = request.get(key)
            if value is not None and float(value) > 0:
                return float(value)
        except (TypeError, ValueError):
            continue
    return None
