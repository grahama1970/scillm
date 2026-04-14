"""Active calls tracker — exposes in-flight LLM requests for live monitoring.

Tracks requests from pre_call to post_call/on_error, enabling dashboards
to show currently running calls with elapsed time and activity graphs.
"""
from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from scillm.proxy.middleware import BaseMiddleware


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

    def to_dict(self) -> dict:
        elapsed_ms = round((time.monotonic() - self.started_at) * 1000)
        return {
            "call_id": self.call_id,
            "model": self.model,
            "caller": self.caller,
            "provider": self.provider,
            "stream": self.stream,
            "started_ts": self.started_ts,
            "elapsed_ms": elapsed_ms,
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
_lock = Lock()

# Rolling window of completed calls for activity graph (last 5 minutes)
_completed_calls: deque[CompletedCall] = deque(maxlen=1000)
_WINDOW_SECONDS = 300  # 5 minutes


def get_active_calls() -> list[dict]:
    """Return list of all active calls (for /v1/scillm/active-calls endpoint)."""
    with _lock:
        return [c.to_dict() for c in _active_calls.values()]


def get_active_count() -> int:
    """Return count of active calls."""
    with _lock:
        return len(_active_calls)


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
        bucket_end = bucket_start + bucket_seconds
        buckets.append({
            "ts": int(bucket_start * 1000),  # JS timestamp
            "total": 0,
            "success": 0,
            "error": 0,
            "latency_sum": 0,
        })

    with _lock:
        # Prune old entries
        while _completed_calls and _completed_calls[0].ts < cutoff:
            _completed_calls.popleft()

        # Bucket the calls
        for call in _completed_calls:
            bucket_idx = int((call.ts - (now - _WINDOW_SECONDS)) / bucket_seconds)
            if 0 <= bucket_idx < num_buckets:
                buckets[bucket_idx]["total"] += 1
                if call.success:
                    buckets[bucket_idx]["success"] += 1
                else:
                    buckets[bucket_idx]["error"] += 1
                buckets[bucket_idx]["latency_sum"] += call.duration_ms

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


def _infer_provider(model: str) -> str:
    """Infer provider from model name."""
    m = model.lower()
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
            provider=_infer_provider(request.get("model", "")),
            stream=bool(request.get("stream")),
        )

        with _lock:
            _active_calls[call_id] = call

        return request

    async def post_call(self, request: dict, response: Any) -> Any:
        call_id = request.get("_active_call_id")
        if call_id:
            with _lock:
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
            with _lock:
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
