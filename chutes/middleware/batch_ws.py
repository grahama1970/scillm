"""WebSocket batch progress tracking — real-time notifications for batch operations.

Tracks batch progress via X-Scillm-Batch-Id header and broadcasts completion
events to subscribed WebSocket clients.

Architecture:
- BatchTracker: Global state tracking batch progress
- BatchProgressMiddleware: Middleware that increments counters on response
- WebSocket endpoint: Clients subscribe to batch/{batch_id} for real-time updates

No external dependencies — uses asyncio primitives for concurrency.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

from scillm.proxy.middleware import BaseMiddleware


@dataclass
class BatchState:
    """State for a single batch operation."""
    batch_id: str
    total: int  # 0 if unknown (client updates via header)
    completed: int = 0
    failed: int = 0
    created_at: float = field(default_factory=time.monotonic)
    last_update: float = field(default_factory=time.monotonic)
    # Track individual call outcomes
    results: list[dict] = field(default_factory=list)
    # WebSocket subscribers
    subscribers: set[WebSocket] = field(default_factory=set)


class BatchTracker:
    """Global batch progress tracker with WebSocket broadcast.

    Thread-safe via asyncio.Lock. Batches are auto-cleaned after 1 hour idle.
    """

    def __init__(self, cleanup_after_seconds: int = 3600):
        self._batches: dict[str, BatchState] = {}
        self._lock = asyncio.Lock()
        self._cleanup_after = cleanup_after_seconds

    async def get_or_create(self, batch_id: str, total: int = 0) -> BatchState:
        """Get existing batch or create new one."""
        async with self._lock:
            if batch_id not in self._batches:
                self._batches[batch_id] = BatchState(batch_id=batch_id, total=total)
                logger.debug("batch_ws: created batch {}", batch_id)
            elif total > 0 and self._batches[batch_id].total == 0:
                # Client provided total count — update
                self._batches[batch_id].total = total
            return self._batches[batch_id]

    async def record_completion(
        self,
        batch_id: str,
        call_key: str,
        status: str,
        duration_ms: int,
        cost_usd: float | None,
        error: str | None = None,
    ) -> None:
        """Record a call completion and broadcast to subscribers."""
        batch = await self.get_or_create(batch_id)

        async with self._lock:
            batch.last_update = time.monotonic()
            if status == "ok":
                batch.completed += 1
            else:
                batch.failed += 1

            result = {
                "call_key": call_key,
                "status": status,
                "duration_ms": duration_ms,
                "cost_usd": cost_usd,
                "error": error,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            batch.results.append(result)

            # Build progress event
            total_done = batch.completed + batch.failed
            event = {
                "event": "call_complete",
                "batch_id": batch_id,
                "call_key": call_key,
                "status": status,
                "duration_ms": duration_ms,
                "cost_usd": cost_usd,
                "error": error,
                "progress": {
                    "completed": batch.completed,
                    "failed": batch.failed,
                    "total": batch.total if batch.total > 0 else total_done,
                },
            }

            # Copy subscribers to avoid iteration issues
            subscribers = list(batch.subscribers)

        # Broadcast outside lock
        await self._broadcast(subscribers, event)

        # Check if batch is complete
        if batch.total > 0 and total_done >= batch.total:
            await self._finalize_batch(batch_id)

    async def _broadcast(self, subscribers: list[WebSocket], event: dict) -> None:
        """Broadcast event to all subscribers, removing disconnected ones."""
        disconnected = []
        for ws in subscribers:
            try:
                await ws.send_json(event)
            except Exception:
                disconnected.append(ws)

        # Clean up disconnected sockets
        if disconnected:
            async with self._lock:
                for batch in self._batches.values():
                    for ws in disconnected:
                        batch.subscribers.discard(ws)

    async def _finalize_batch(self, batch_id: str) -> None:
        """Send batch_complete event and close subscriptions."""
        async with self._lock:
            batch = self._batches.get(batch_id)
            if not batch:
                return

            elapsed_s = time.monotonic() - batch.created_at
            total_cost = sum(r.get("cost_usd") or 0 for r in batch.results)

            event = {
                "event": "batch_complete",
                "batch_id": batch_id,
                "summary": {
                    "completed": batch.completed,
                    "failed": batch.failed,
                    "total": batch.total,
                    "elapsed_seconds": round(elapsed_s, 2),
                    "total_cost_usd": round(total_cost, 6),
                },
            }

            subscribers = list(batch.subscribers)

        # Broadcast final event
        await self._broadcast(subscribers, event)

        # Close all WebSocket connections gracefully
        for ws in subscribers:
            try:
                await ws.close(code=1000, reason="batch_complete")
            except Exception:
                pass

    async def subscribe(self, batch_id: str, ws: WebSocket) -> BatchState:
        """Subscribe a WebSocket to batch updates."""
        batch = await self.get_or_create(batch_id)
        async with self._lock:
            batch.subscribers.add(ws)
            logger.info("batch_ws: client subscribed to batch {} ({} subscribers)",
                       batch_id, len(batch.subscribers))
        return batch

    async def unsubscribe(self, batch_id: str, ws: WebSocket) -> None:
        """Unsubscribe a WebSocket from batch updates."""
        async with self._lock:
            batch = self._batches.get(batch_id)
            if batch:
                batch.subscribers.discard(ws)
                logger.debug("batch_ws: client unsubscribed from batch {}", batch_id)

    async def get_status(self, batch_id: str) -> dict | None:
        """Get current batch status (for REST endpoint)."""
        async with self._lock:
            batch = self._batches.get(batch_id)
            if not batch:
                return None

            total_done = batch.completed + batch.failed
            elapsed_s = time.monotonic() - batch.created_at
            total_cost = sum(r.get("cost_usd") or 0 for r in batch.results)

            return {
                "batch_id": batch_id,
                "completed": batch.completed,
                "failed": batch.failed,
                "total": batch.total if batch.total > 0 else total_done,
                "elapsed_seconds": round(elapsed_s, 2),
                "total_cost_usd": round(total_cost, 6),
                "subscribers": len(batch.subscribers),
                "is_complete": batch.total > 0 and total_done >= batch.total,
            }

    async def cleanup_stale(self) -> int:
        """Remove batches that have been idle for cleanup_after seconds."""
        now = time.monotonic()
        stale = []

        async with self._lock:
            for batch_id, batch in self._batches.items():
                if now - batch.last_update > self._cleanup_after:
                    stale.append(batch_id)

            for batch_id in stale:
                del self._batches[batch_id]

        if stale:
            logger.debug("batch_ws: cleaned up {} stale batches", len(stale))

        return len(stale)


# Global tracker instance
_tracker = BatchTracker()


def get_batch_tracker() -> BatchTracker:
    """Get the global batch tracker instance."""
    return _tracker


class BatchProgressMiddleware(BaseMiddleware):
    """Middleware that tracks batch progress and broadcasts to WebSocket subscribers.

    Reads X-Scillm-Batch-Id and X-Scillm-Call-Key headers to correlate requests
    with batches. On completion (success or error), broadcasts progress event.
    """

    async def pre_call(self, request: dict) -> dict:
        """Extract batch headers and store for post_call."""
        headers = request.get("_headers", {})

        # Case-insensitive header lookup
        batch_id = None
        call_key = None
        batch_total = 0

        for k, v in headers.items():
            kl = k.lower()
            if kl == "x-scillm-batch-id":
                batch_id = v
            elif kl == "x-scillm-call-key":
                call_key = v
            elif kl == "x-scillm-batch-total":
                try:
                    batch_total = int(v)
                except (ValueError, TypeError):
                    pass

        if batch_id:
            request["_batch_id"] = batch_id
            request["_call_key"] = call_key or request.get("model", "unknown")
            request["_batch_start"] = time.monotonic()
            # Ensure batch exists with total if provided
            if batch_total > 0:
                await _tracker.get_or_create(batch_id, total=batch_total)

        return request

    async def post_call(self, request: dict, response: Any) -> Any:
        """Record successful completion and broadcast."""
        batch_id = request.get("_batch_id")
        if not batch_id:
            return response

        call_key = request.get("_call_key", "unknown")
        start = request.get("_batch_start")
        duration_ms = round((time.monotonic() - start) * 1000) if start else 0

        # Extract cost from response
        cost_usd = None
        if isinstance(response, dict):
            cost_headers = response.get("_cost_headers", {})
            raw_cost = cost_headers.get("x-cost-usd", "")
            if raw_cost and raw_cost != "unknown":
                try:
                    cost_usd = float(raw_cost)
                except (ValueError, TypeError):
                    pass

        await _tracker.record_completion(
            batch_id=batch_id,
            call_key=call_key,
            status="ok",
            duration_ms=duration_ms,
            cost_usd=cost_usd,
        )

        return response

    async def on_error(self, request: dict, error: Exception) -> None:
        """Record failed completion and broadcast."""
        batch_id = request.get("_batch_id")
        if not batch_id:
            return

        call_key = request.get("_call_key", "unknown")
        start = request.get("_batch_start")
        duration_ms = round((time.monotonic() - start) * 1000) if start else 0

        await _tracker.record_completion(
            batch_id=batch_id,
            call_key=call_key,
            status="error",
            duration_ms=duration_ms,
            cost_usd=None,
            error=f"{type(error).__name__}: {str(error)[:200]}",
        )


async def websocket_batch_handler(websocket: WebSocket, batch_id: str) -> None:
    """WebSocket handler for batch progress subscription.

    Called from the FastAPI WebSocket endpoint. Handles:
    - Connection accept
    - Initial state message
    - Subscription lifecycle
    - Graceful disconnect
    """
    await websocket.accept()

    try:
        # Subscribe and send initial state
        batch = await _tracker.subscribe(batch_id, websocket)

        async with _tracker._lock:
            total_done = batch.completed + batch.failed
            initial = {
                "event": "subscribed",
                "batch_id": batch_id,
                "progress": {
                    "completed": batch.completed,
                    "failed": batch.failed,
                    "total": batch.total if batch.total > 0 else total_done,
                },
            }

        await websocket.send_json(initial)

        # Keep connection alive, handle client messages
        while True:
            try:
                # Wait for client messages (ping/pong or updates)
                data = await asyncio.wait_for(websocket.receive_json(), timeout=30.0)

                # Handle client-sent batch total update
                if data.get("type") == "set_total":
                    total = data.get("total", 0)
                    if total > 0:
                        async with _tracker._lock:
                            batch.total = total
                        await websocket.send_json({
                            "event": "total_updated",
                            "batch_id": batch_id,
                            "total": total,
                        })

            except asyncio.TimeoutError:
                # Send ping to keep connection alive
                try:
                    await websocket.send_json({"event": "ping"})
                except Exception:
                    break

    except WebSocketDisconnect:
        logger.debug("batch_ws: client disconnected from batch {}", batch_id)
    except Exception as exc:
        logger.warning("batch_ws: error in WebSocket handler: {}", exc)
    finally:
        await _tracker.unsubscribe(batch_id, websocket)
