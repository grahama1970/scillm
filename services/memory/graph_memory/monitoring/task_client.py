"""Task-monitor client for graph_memory.

Combines file-based state (for task-monitor TUI) with NDJSON streaming.

Usage:
    from graph_memory.monitoring.task_client import TaskClient
from loguru import logger

    client = TaskClient("embed-lessons", total=1000)
    for i, item in enumerate(items):
        result = process(item)
        client.item_complete(i, ok=result.ok, item=item.key)
    summary = client.finish(success=True)

Registry Integration:
    Tasks register at ~/.pi/task-monitor/registry.json for discovery.
    State files are written at .{task_name}_state.json for TUI display.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .events import EventEmitter

# Task monitor registry location
TASK_MONITOR_REGISTRY = Path.home() / ".pi" / "task-monitor" / "registry.json"


@dataclass
class TaskMetrics:
    """Accumulated metrics for a task."""

    success: int = 0
    failed: int = 0
    retries: int = 0
    rate_limits: int = 0
    quality_gates_passed: int = 0
    quality_gates_failed: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "success": self.success,
            "failed": self.failed,
            "retries": self.retries,
            "rate_limits": self.rate_limits,
            "quality_gates_passed": self.quality_gates_passed,
            "quality_gates_failed": self.quality_gates_failed,
        }


@dataclass
class FailureRecord:
    """Record of a task item failure."""

    index: int
    item_id: str
    error: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "index": self.index,
            "item_id": self.item_id[:50],
            "error": self.error[:100],
            "timestamp": self.timestamp,
        }


class TaskClient:
    """Unified task client for graph_memory batch operations.

    Combines:
    - File-based state for task-monitor TUI display
    - NDJSON event streaming for real-time monitoring
    - Registry integration for task discovery

    Args:
        task_name: Human-readable task name
        total: Total number of items to process
        project: Project name (default: "graph-memory")
        state_dir: Directory for state file (default: cwd)
        emit_ndjson: Whether to emit NDJSON events (default: True)
        register: Whether to register with task-monitor (default: True)
        description: Optional task description
    """

    def __init__(
        self,
        task_name: str,
        total: int,
        project: str = "graph-memory",
        state_dir: Optional[Path] = None,
        emit_ndjson: bool = True,
        register: bool = True,
        description: Optional[str] = None,
    ):
        self.task_name = task_name
        self.total = total
        self.project = project
        self.description = description or f"{project}: {task_name}"
        self.state_dir = Path(state_dir) if state_dir else Path.cwd()
        self.state_file = self.state_dir / f".{task_name}_state.json"

        # Metrics tracking
        self.metrics = TaskMetrics()
        self.completed = 0
        self.start_time = time.time()
        self.last_update = 0.0
        self.current_item = ""

        # Failure tracking (keep last N)
        self.failures: List[FailureRecord] = []
        self._max_failures = 50

        # NDJSON emitter
        self.emitter: Optional[EventEmitter] = None
        if emit_ndjson:
            self.emitter = EventEmitter(task_name=task_name, project=project)
            self.emitter.init(total=total, description=self.description)

        # Register with task-monitor
        if register:
            self._register()

        # Write initial state
        self._write_state()

    def _register(self) -> None:
        """Register with task-monitor registry."""
        try:
            TASK_MONITOR_REGISTRY.parent.mkdir(parents=True, exist_ok=True)

            registry = {}
            if TASK_MONITOR_REGISTRY.exists():
                try:
                    registry = json.loads(TASK_MONITOR_REGISTRY.read_text())
                except Exception as exc:
                    logger.error("Suppressed error in task_client: {}", exc)
                    registry = {}

            registry[f"{self.project}:{self.task_name}"] = {
                "state_file": str(self.state_file),
                "total": self.total,
                "description": self.description,
                "project": self.project,
                "registered_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

            # Atomic write
            tmp = TASK_MONITOR_REGISTRY.with_suffix(".tmp")
            tmp.write_text(json.dumps(registry, indent=2))
            os.replace(tmp, TASK_MONITOR_REGISTRY)
        except Exception as exc:
            logger.error("Suppressed error in task_client: {}", exc)
            pass  # Silent failure - monitoring is optional

    def _write_state(self, final: bool = False) -> None:
        """Write state file for task-monitor TUI."""
        now = time.time()

        # Throttle updates (except final)
        if not final and (now - self.last_update) < 0.5:
            return
        self.last_update = now

        elapsed = now - self.start_time
        pct = (self.completed / self.total * 100) if self.total > 0 else 0

        # Calculate ETA
        eta_s = None
        if self.completed > 0 and self.completed < self.total:
            rate = self.completed / elapsed
            remaining = self.total - self.completed
            eta_s = remaining / rate if rate > 0 else None

        state = {
            "completed": self.completed,
            "total": self.total,
            "description": self.description,
            "current_item": self.current_item,
            "progress_pct": round(pct, 1),
            "elapsed_seconds": round(elapsed, 1),
            "eta_seconds": round(eta_s, 1) if eta_s else None,
            "stats": self.metrics.to_dict(),
            "failures": [f.to_dict() for f in self.failures[-20:]],
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "completed" if final else "running",
        }

        try:
            # Atomic write
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2))
            os.replace(tmp, self.state_file)
        except Exception as exc:
            logger.error("Suppressed error in task_client: {}", exc)
            pass  # Silent failure - monitoring is optional

    def item_complete(
        self,
        index: int,
        ok: bool,
        item: str = "",
        error: Optional[str] = None,
        **data: Any,
    ) -> None:
        """Record item completion.

        Args:
            index: Item index (0-based)
            ok: Whether processing succeeded
            item: Item identifier for display
            error: Error message if not ok
            **data: Additional data for NDJSON event
        """
        self.completed += 1
        self.current_item = item[:50] if item else f"item-{index}"

        if ok:
            self.metrics.success += 1
        else:
            self.metrics.failed += 1
            if error:
                self.failures.append(
                    FailureRecord(index=index, item_id=item, error=error)
                )
                # Trim to max
                if len(self.failures) > self._max_failures:
                    self.failures = self.failures[-self._max_failures :]

        if self.emitter:
            self.emitter.item_complete(index=index, ok=ok, item=item, **data)

        self._write_state()

    def retry(
        self,
        index: int,
        attempt: int,
        max_attempts: int,
        error: str,
    ) -> None:
        """Record a retry attempt.

        Args:
            index: Item index being retried
            attempt: Current attempt number (1-based)
            max_attempts: Maximum attempts configured
            error: Error that triggered retry
        """
        self.metrics.retries += 1

        if self.emitter:
            self.emitter.retry(
                index=index,
                attempt=attempt,
                max_attempts=max_attempts,
                error=error,
            )

    def rate_limit(self, retry_after_s: float, provider: str = "") -> None:
        """Record a rate limit event.

        Args:
            retry_after_s: Seconds to wait
            provider: Provider that rate-limited
        """
        self.metrics.rate_limits += 1

        if self.emitter:
            self.emitter.rate_limit(retry_after_s=retry_after_s, provider=provider)

    def quality_gate(
        self,
        gate_name: str,
        passed: bool,
        metrics: Dict[str, float],
        thresholds: Dict[str, float],
    ) -> None:
        """Record a quality gate evaluation.

        Args:
            gate_name: Name of the gate
            passed: Whether gate passed
            metrics: Actual metric values
            thresholds: Threshold values
        """
        if passed:
            self.metrics.quality_gates_passed += 1
        else:
            self.metrics.quality_gates_failed += 1

        if self.emitter:
            self.emitter.quality_gate(
                gate_name=gate_name,
                passed=passed,
                metrics=metrics,
                thresholds=thresholds,
            )

    def error(
        self,
        index: Optional[int],
        error: str,
        recoverable: bool = False,
    ) -> None:
        """Record an error.

        Args:
            index: Item index (None for global errors)
            error: Error message
            recoverable: Whether processing can continue
        """
        if self.emitter:
            self.emitter.error(index=index, error=error, recoverable=recoverable)

    def progress(self, **data: Any) -> None:
        """Emit a progress update (for long-running single items).

        Args:
            **data: Additional data to include
        """
        if self.emitter:
            self.emitter.progress(completed=self.completed, total=self.total, **data)

    def finish(self, success: bool = True) -> Dict[str, Any]:
        """Mark task as complete and return summary.

        Args:
            success: Whether task completed successfully

        Returns:
            Summary statistics dictionary
        """
        summary = {
            "task_name": self.task_name,
            "project": self.project,
            "total": self.total,
            "completed": self.completed,
            **self.metrics.to_dict(),
            "elapsed_seconds": round(time.time() - self.start_time, 1),
            "success": success,
        }

        if self.emitter:
            self.emitter.done(success=success, summary=summary)

        self._write_state(final=True)

        return summary

    def get_summary(self) -> Dict[str, Any]:
        """Get current summary without finishing.

        Returns:
            Current statistics dictionary
        """
        return {
            "task_name": self.task_name,
            "project": self.project,
            "total": self.total,
            "completed": self.completed,
            **self.metrics.to_dict(),
            "elapsed_seconds": round(time.time() - self.start_time, 1),
            "progress_pct": round(
                (self.completed / self.total * 100) if self.total > 0 else 0, 1
            ),
        }


def create_task_client(
    task_name: str,
    total: int,
    **kwargs: Any,
) -> TaskClient:
    """Factory function for TaskClient.

    Args:
        task_name: Task name
        total: Total items
        **kwargs: Additional TaskClient arguments

    Returns:
        Configured TaskClient
    """
    return TaskClient(task_name=task_name, total=total, **kwargs)
