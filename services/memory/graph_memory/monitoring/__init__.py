"""Monitoring module for graph_memory.

Provides NDJSON event streaming and task-monitor integration.

Usage:
    from graph_memory.monitoring import EventEmitter, TaskClient, BatchProgress

    # NDJSON events
    emitter = EventEmitter("my-task")
    emitter.init(total=100)
    emitter.item_complete(0, ok=True)
    emitter.done(success=True)

    # Task client (combines NDJSON + file state)
    client = TaskClient("embed-lessons", total=1000)
    for i, item in enumerate(items):
        client.item_complete(i, ok=True, item=item.key)
    client.finish(success=True)

    # Batch progress wrapper (tqdm replacement)
    for item in BatchProgress(items, name="process"):
        process(item)
"""

from .events import EventEmitter, EventType
from .streaming import BatchProgress, emit_ndjson, ndjson_stream
from .task_client import (
    FailureRecord,
    TaskClient,
    TaskMetrics,
    create_task_client,
)

__all__ = [
    # Events
    "EventEmitter",
    "EventType",
    # Task client
    "TaskClient",
    "TaskMetrics",
    "FailureRecord",
    "create_task_client",
    # Streaming
    "BatchProgress",
    "ndjson_stream",
    "emit_ndjson",
]
