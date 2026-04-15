"""Streaming utilities for batch operations.

Provides tqdm-like wrappers with NDJSON event emission.

Usage:
    from graph_memory.monitoring.streaming import BatchProgress

    # Simple iteration with progress
    for item in BatchProgress(items, name="process-docs"):
        process(item)

    # With error handling
    progress = BatchProgress(items, name="extract")
    for item in progress:
        try:
            process(item)
        except Exception as e:
            progress.fail(str(e))
"""
from __future__ import annotations

import json
import sys
from typing import Any, Callable, Iterable, Iterator, Optional, TypeVar

from .events import EventEmitter
from .task_client import TaskClient

T = TypeVar("T")


class BatchProgress:
    """NDJSON-streaming progress wrapper (tqdm alternative).

    Wraps an iterable and emits NDJSON events for each item.
    Also writes state files for task-monitor TUI.

    Args:
        iterable: Items to iterate
        name: Task name for monitoring
        total: Total count (auto-detected if iterable has len)
        project: Project name (default: "graph-memory")
        emit_interval: Emit progress every N items (default: 1)
        register: Register with task-monitor (default: True)

    Example:
        for doc in BatchProgress(documents, name="embed-docs"):
            embed(doc)
    """

    def __init__(
        self,
        iterable: Iterable[T],
        name: str = "batch",
        total: Optional[int] = None,
        project: str = "graph-memory",
        emit_interval: int = 1,
        register: bool = True,
    ):
        self.iterable = iterable
        self.name = name
        self.total = total or (len(iterable) if hasattr(iterable, "__len__") else 0)
        self.project = project
        self.emit_interval = emit_interval
        self.register = register

        self._client: Optional[TaskClient] = None
        self._current_index = 0
        self._last_error: Optional[str] = None

    def __iter__(self) -> Iterator[T]:
        """Iterate with progress tracking."""
        self._client = TaskClient(
            task_name=self.name,
            total=self.total,
            project=self.project,
            emit_ndjson=True,
            register=self.register,
        )

        for i, item in enumerate(self.iterable):
            self._current_index = i
            self._last_error = None

            yield item

            # Record completion (ok unless fail() was called)
            ok = self._last_error is None
            if (i + 1) % self.emit_interval == 0 or not ok:
                self._client.item_complete(
                    index=i,
                    ok=ok,
                    item=str(item)[:50] if item else f"item-{i}",
                    error=self._last_error,
                )

        # Final completion
        success = self._client.metrics.failed == 0
        self._client.finish(success=success)

    def fail(self, error: str) -> None:
        """Mark current item as failed.

        Call this in the iteration loop when an error occurs.

        Args:
            error: Error message
        """
        self._last_error = error

    def retry(self, attempt: int, max_attempts: int, error: str) -> None:
        """Record a retry attempt for current item.

        Args:
            attempt: Current attempt number (1-based)
            max_attempts: Maximum attempts configured
            error: Error that triggered retry
        """
        if self._client:
            self._client.retry(
                index=self._current_index,
                attempt=attempt,
                max_attempts=max_attempts,
                error=error,
            )

    @property
    def client(self) -> Optional[TaskClient]:
        """Get the underlying TaskClient (available after iteration starts)."""
        return self._client


def ndjson_stream(
    iterable: Iterable[T],
    processor: Callable[[T], dict],
    name: str = "stream",
    project: str = "graph-memory",
) -> Iterator[dict]:
    """Yield NDJSON records while emitting progress events.

    Useful for streaming transformations with monitoring.

    Args:
        iterable: Items to process
        processor: Function to transform each item to a dict
        name: Task name for monitoring
        project: Project name

    Yields:
        Processed records (or error records on failure)

    Example:
        for record in ndjson_stream(docs, extract_text, name="extract"):
            print(json.dumps(record))
    """
    emitter = EventEmitter(task_name=name, project=project)
    items_list = list(iterable)
    emitter.init(total=len(items_list))

    for i, item in enumerate(items_list):
        try:
            result = processor(item)
            emitter.item_complete(index=i, ok=True)
            yield result
        except Exception as e:
            emitter.error(index=i, error=str(e), recoverable=True)
            yield {"error": str(e), "index": i, "item": str(item)[:50]}

    success = True  # Could track errors
    emitter.done(success=success)


def emit_ndjson(record: dict, stream=sys.stdout) -> None:
    """Emit a single NDJSON record.

    Convenience function for manual NDJSON output.

    Args:
        record: Dictionary to emit
        stream: Output stream (default: stdout)
    """
    line = json.dumps(record, ensure_ascii=False)
    stream.write(line + "\n")
    stream.flush()
