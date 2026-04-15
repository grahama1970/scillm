"""NDJSON event emitter for graph_memory.

Provides task-monitor compatible event streaming to stderr or file.

Event Types:
    init          - Task started
    progress      - Periodic progress update
    item_complete - Single item finished
    retry         - LLM retry with correction context
    error         - Non-fatal error
    rate_limit    - Rate limit hit
    quality_gate  - Quality threshold checked
    done          - Task completed

Usage:
    from graph_memory.monitoring.events import EventEmitter

    emitter = EventEmitter(task_name="embed-lessons")
    emitter.init(total=1000, description="Embedding all lessons")
    for i, item in enumerate(items):
        result = process(item)
        emitter.item_complete(index=i, ok=result.ok, data={"key": item.key})
    emitter.done(success=True)
"""
from __future__ import annotations

import json
import sys
import time
from enum import Enum
from typing import Any, Dict, Optional, TextIO


class EventType(str, Enum):
    """Task-monitor compatible event types."""

    INIT = "init"
    PROGRESS = "progress"
    ITEM_COMPLETE = "item_complete"
    RETRY = "retry"
    ERROR = "error"
    RATE_LIMIT = "rate_limit"
    QUALITY_GATE = "quality_gate"
    DONE = "done"


class EventEmitter:
    """Emit NDJSON events to stderr or file.

    Each event is a single JSON line with:
    - event: Event type (init, progress, item_complete, etc.)
    - task_id: Unique identifier for this task run
    - task_name: Human-readable task name
    - timestamp: Unix timestamp (float)
    - elapsed_ms: Milliseconds since task start
    - Additional fields based on event type

    Args:
        task_name: Human-readable name for the task
        stream: Output stream (default: stderr)
        task_id: Unique identifier (default: auto-generated)
        project: Project name for grouping (default: None)
    """

    def __init__(
        self,
        task_name: str,
        stream: TextIO = sys.stderr,
        task_id: Optional[str] = None,
        project: Optional[str] = None,
    ):
        self.task_name = task_name
        self.task_id = task_id or f"{task_name}-{int(time.time())}"
        self.project = project
        self.stream = stream
        self._start_time = time.time()
        self._count = 0
        self._total = 0

    def _emit(self, event_type: EventType, **data: Any) -> None:
        """Emit a single NDJSON line."""
        record = {
            "event": event_type.value,
            "task_id": self.task_id,
            "task_name": self.task_name,
            "timestamp": time.time(),
            "elapsed_ms": int((time.time() - self._start_time) * 1000),
            **data,
        }
        if self.project:
            record["project"] = self.project
        line = json.dumps(record, ensure_ascii=False)
        self.stream.write(line + "\n")
        self.stream.flush()

    def init(self, total: int, description: str = "", **meta: Any) -> None:
        """Emit task initialization event.

        Args:
            total: Total number of items to process
            description: Human-readable task description
            **meta: Additional metadata
        """
        self._total = total
        self._emit(EventType.INIT, total=total, description=description, meta=meta)

    def progress(self, completed: int, total: Optional[int] = None, **data: Any) -> None:
        """Emit periodic progress update.

        Args:
            completed: Number of items completed
            total: Total items (uses init total if not provided)
            **data: Additional data
        """
        total = total or self._total
        pct = (completed / total * 100) if total > 0 else 0
        self._emit(
            EventType.PROGRESS,
            completed=completed,
            total=total,
            pct=round(pct, 1),
            **data,
        )

    def item_complete(self, index: int, ok: bool, **data: Any) -> None:
        """Emit item completion event.

        Args:
            index: Item index (0-based)
            ok: Whether processing succeeded
            **data: Additional data (item_id, duration_ms, etc.)
        """
        self._count += 1
        self._emit(
            EventType.ITEM_COMPLETE,
            index=index,
            ok=ok,
            count=self._count,
            **data,
        )

    def retry(
        self,
        index: int,
        attempt: int,
        max_attempts: int,
        error: str,
        **data: Any,
    ) -> None:
        """Emit retry event (for LLM self-correction).

        Args:
            index: Item index being retried
            attempt: Current attempt number (1-based)
            max_attempts: Maximum attempts configured
            error: Error message that triggered retry
            **data: Additional data
        """
        self._emit(
            EventType.RETRY,
            index=index,
            attempt=attempt,
            max_attempts=max_attempts,
            error=error[:200],  # Truncate long errors
            **data,
        )

    def error(
        self,
        index: Optional[int],
        error: str,
        recoverable: bool = False,
        error_type: Optional[str] = None,
        **data: Any,
    ) -> None:
        """Emit error event.

        Args:
            index: Item index (None for global errors)
            error: Error message
            recoverable: Whether processing can continue
            error_type: Classification (timeout, validation, etc.)
            **data: Additional data
        """
        self._emit(
            EventType.ERROR,
            index=index,
            error=error[:500],  # Truncate long errors
            recoverable=recoverable,
            error_type=error_type,
            **data,
        )

    def rate_limit(
        self,
        retry_after_s: float,
        provider: str = "",
        **data: Any,
    ) -> None:
        """Emit rate limit event.

        Args:
            retry_after_s: Seconds to wait before retry
            provider: Provider that rate-limited (e.g., "openai")
            **data: Additional data
        """
        self._emit(
            EventType.RATE_LIMIT,
            retry_after_s=retry_after_s,
            provider=provider,
            **data,
        )

    def quality_gate(
        self,
        gate_name: str,
        passed: bool,
        metrics: Dict[str, float],
        thresholds: Dict[str, float],
        **data: Any,
    ) -> None:
        """Emit quality gate evaluation event.

        Args:
            gate_name: Name of the quality gate
            passed: Whether gate passed
            metrics: Actual metric values
            thresholds: Threshold values used
            **data: Additional data
        """
        self._emit(
            EventType.QUALITY_GATE,
            gate_name=gate_name,
            passed=passed,
            metrics=metrics,
            thresholds=thresholds,
            **data,
        )

    def done(self, success: bool, summary: Optional[Dict[str, Any]] = None) -> None:
        """Emit task completion event.

        Args:
            success: Whether task completed successfully
            summary: Summary statistics
        """
        self._emit(
            EventType.DONE,
            success=success,
            summary=summary or {},
            total_count=self._count,
        )

    @property
    def count(self) -> int:
        """Number of items completed."""
        return self._count

    @property
    def elapsed_ms(self) -> int:
        """Milliseconds since task start."""
        return int((time.time() - self._start_time) * 1000)
