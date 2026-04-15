"""Learning from operation outcomes.

Tracks success/failure patterns and retry complexity to improve future operations.

Confidence adjustments match existing feedback.py pattern:
    - Success: +0.02
    - Failure: -0.02
    - Per retry: -0.01 (complexity penalty)

Usage:
    from graph_memory.quality.learning import LearningTracker, record_outcome
from loguru import logger

    # Batch tracking
    tracker = LearningTracker(scope="retrieval")
    for item in items:
        result = process(item)
        if result.ok:
            tracker.record_success(item.id, retry_count=result.retries)
        else:
            tracker.record_failure(item.id, error_context=str(result.error))
    tracker.flush()

    # Single outcome
    record_outcome("lessons/abc", success=True, retry_count=2, scope="extraction")
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

# Confidence adjustments (matching existing feedback.py pattern)
SUCCESS_BOOST = 0.02
FAILURE_PENALTY = -0.02
RETRY_COMPLEXITY_WEIGHT = 0.01  # Per retry count

# Environment variable overrides
ENV_SUCCESS_BOOST = "GM_LEARNING_SUCCESS_BOOST"
ENV_FAILURE_PENALTY = "GM_LEARNING_FAILURE_PENALTY"
ENV_RETRY_WEIGHT = "GM_LEARNING_RETRY_WEIGHT"
ENV_BATCH_SIZE = "GM_LEARNING_BATCH_SIZE"


def _get_confidence_adjustments() -> tuple[float, float, float]:
    """Get confidence adjustment values from env vars or defaults."""
    success = float(os.getenv(ENV_SUCCESS_BOOST, str(SUCCESS_BOOST)))
    failure = float(os.getenv(ENV_FAILURE_PENALTY, str(FAILURE_PENALTY)))
    retry = float(os.getenv(ENV_RETRY_WEIGHT, str(RETRY_COMPLEXITY_WEIGHT)))
    return success, failure, retry


@dataclass
class OutcomeRecord:
    """Record of a single operation outcome."""

    item_id: str
    success: bool
    retry_count: int = 0
    error_context: Optional[str] = None
    duration_ms: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "item_id": self.item_id,
            "success": self.success,
            "retry_count": self.retry_count,
            "error_context": self.error_context[:200] if self.error_context else None,
            "duration_ms": self.duration_ms,
            "timestamp": self.timestamp,
        }


class LearningTracker:
    """Track outcomes and update memory confidence based on patterns.

    Learns from:
    - Success patterns: boost confidence for related items
    - Error patterns: reduce confidence, record error context
    - Retry complexity: items needing many retries indicate difficulty

    Args:
        scope: Scope for learning (e.g., "retrieval", "extraction")
        batch_size: Flush to database after this many outcomes
        auto_flush: Whether to auto-flush on batch size (default: True)
    """

    def __init__(
        self,
        scope: str = "",
        batch_size: int = 50,
        auto_flush: bool = True,
    ):
        self.scope = scope
        self.batch_size = int(os.getenv(ENV_BATCH_SIZE, str(batch_size)))
        self.auto_flush = auto_flush

        self._outcomes: List[OutcomeRecord] = []
        self._start_time = time.time()

        # Load adjustment values
        self._success_boost, self._failure_penalty, self._retry_weight = (
            _get_confidence_adjustments()
        )

    def record_success(
        self,
        item_id: str,
        retry_count: int = 0,
        duration_ms: int = 0,
        **metadata: Any,
    ) -> None:
        """Record a successful operation.

        Args:
            item_id: Identifier for the item (e.g., "lessons/abc")
            retry_count: Number of retries needed
            duration_ms: Operation duration
            **metadata: Additional metadata to store
        """
        self._outcomes.append(
            OutcomeRecord(
                item_id=item_id,
                success=True,
                retry_count=retry_count,
                duration_ms=duration_ms,
                metadata=metadata,
            )
        )

        if self.auto_flush and len(self._outcomes) >= self.batch_size:
            self.flush()

    def record_failure(
        self,
        item_id: str,
        error_context: str,
        retry_count: int = 0,
        duration_ms: int = 0,
        **metadata: Any,
    ) -> None:
        """Record a failed operation.

        Args:
            item_id: Identifier for the item
            error_context: Error message or context
            retry_count: Number of retries attempted
            duration_ms: Operation duration
            **metadata: Additional metadata to store
        """
        self._outcomes.append(
            OutcomeRecord(
                item_id=item_id,
                success=False,
                retry_count=retry_count,
                error_context=error_context[:500],  # Truncate long errors
                duration_ms=duration_ms,
                metadata=metadata,
            )
        )

        if self.auto_flush and len(self._outcomes) >= self.batch_size:
            self.flush()

    def flush(self) -> Dict[str, Any]:
        """Persist outcomes to database and update confidence.

        Returns:
            Dict with update statistics
        """
        if not self._outcomes:
            return {"updated": 0, "skipped": 0}

        updated = 0
        skipped = 0

        # Try to get database connection
        try:
            from ..arango_client import get_db

            db = get_db()
        except Exception as exc:
            logger.error("Suppressed error in learning: {}", exc)
            # Database not available - just clear outcomes
            count = len(self._outcomes)
            self._outcomes.clear()
            return {"updated": 0, "skipped": count, "reason": "db_unavailable"}

        for outcome in self._outcomes:
            # Calculate adjustment based on success/failure and retry complexity
            if outcome.success:
                base_adj = self._success_boost
            else:
                base_adj = self._failure_penalty

            # Complexity penalty: more retries = harder task
            complexity_adj = -abs(self._retry_weight * outcome.retry_count)
            total_adj = base_adj + complexity_adj

            # Update lesson confidence if item is a lesson
            if outcome.item_id.startswith("lessons/"):
                try:
                    aql = """
                    LET doc = DOCUMENT(@id)
                    FILTER doc != null
                    LET base = TO_NUMBER(doc.confidence != null ? doc.confidence : 0.5)
                    LET adj = @adj
                    LET clamped = MAX([0.0, MIN([1.0, base + adj])])
                    UPDATE doc WITH {
                        confidence: clamped,
                        retry_complexity: @retries,
                        last_outcome_at: @ts,
                        last_outcome_success: @success
                    } IN lessons_v2
                    RETURN NEW
                    """
                    result = list(
                        db.aql.execute(
                            aql,
                            bind_vars={
                                "id": outcome.item_id,
                                "adj": total_adj,
                                "retries": outcome.retry_count,
                                "ts": int(outcome.timestamp),
                                "success": outcome.success,
                            },
                        )
                    )
                    if result:
                        updated += 1
                    else:
                        skipped += 1
                except Exception as exc:
                    logger.error("Suppressed error in learning: {}", exc)
                    skipped += 1

            # Log event for audit trail
            try:
                from ..events import log_event

                log_event(
                    db,
                    "learning_outcome",
                    f"{'success' if outcome.success else 'failure'}: {outcome.item_id}",
                    {
                        "item_id": outcome.item_id,
                        "success": outcome.success,
                        "retry_count": outcome.retry_count,
                        "error_context": outcome.error_context,
                        "adjustment": total_adj,
                        "scope": self.scope,
                    },
                )
            except Exception as exc:
                logger.error("Suppressed error in learning: {}", exc)
                pass  # Event logging is optional

        self._outcomes.clear()
        return {"updated": updated, "skipped": skipped}

    def get_stats(self) -> Dict[str, Any]:
        """Get accumulated stats without flushing.

        Returns:
            Dict with current statistics
        """
        successes = sum(1 for o in self._outcomes if o.success)
        failures = len(self._outcomes) - successes
        total_retries = sum(o.retry_count for o in self._outcomes)

        return {
            "pending": len(self._outcomes),
            "successes": successes,
            "failures": failures,
            "total_retries": total_retries,
            "avg_retries": total_retries / len(self._outcomes) if self._outcomes else 0,
            "scope": self.scope,
            "elapsed_ms": int((time.time() - self._start_time) * 1000),
        }

    def __enter__(self) -> "LearningTracker":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit - flush remaining outcomes."""
        self.flush()


def record_outcome(
    item_id: str,
    success: bool,
    retry_count: int = 0,
    error_context: Optional[str] = None,
    scope: str = "",
    duration_ms: int = 0,
) -> Dict[str, Any]:
    """Convenience function to record a single outcome immediately.

    Args:
        item_id: Item identifier
        success: Whether operation succeeded
        retry_count: Number of retries
        error_context: Error message if failed
        scope: Learning scope
        duration_ms: Operation duration

    Returns:
        Flush result
    """
    tracker = LearningTracker(scope=scope, batch_size=1, auto_flush=False)

    if success:
        tracker.record_success(item_id=item_id, retry_count=retry_count, duration_ms=duration_ms)
    else:
        tracker.record_failure(
            item_id=item_id,
            error_context=error_context or "",
            retry_count=retry_count,
            duration_ms=duration_ms,
        )

    return tracker.flush()


def get_complexity_for_item(item_id: str) -> Optional[int]:
    """Get historical retry complexity for an item.

    Args:
        item_id: Item identifier

    Returns:
        Retry complexity or None if not found
    """
    try:
        from ..arango_client import get_db

        db = get_db()
        result = list(
            db.aql.execute(
                "LET d = DOCUMENT(@id) RETURN d.retry_complexity",
                bind_vars={"id": item_id},
            )
        )
        return result[0] if result and result[0] is not None else None
    except Exception as exc:
        logger.error("Suppressed error in learning: {}", exc)
        return None


def get_average_complexity(scope: str = "", limit: int = 100) -> float:
    """Get average retry complexity for recent operations.

    Args:
        scope: Optional scope filter
        limit: Number of recent items to consider

    Returns:
        Average retry complexity (0 if no data)
    """
    try:
        from ..arango_client import get_db

        db = get_db()

        if scope:
            aql = """
            FOR l IN lessons_v2
                FILTER l.scope == @scope AND l.retry_complexity != null
                SORT l.last_outcome_at DESC
                LIMIT @limit
                RETURN l.retry_complexity
            """
            bind_vars = {"scope": scope, "limit": limit}
        else:
            aql = """
            FOR l IN lessons_v2
                FILTER l.retry_complexity != null
                SORT l.last_outcome_at DESC
                LIMIT @limit
                RETURN l.retry_complexity
            """
            bind_vars = {"limit": limit}

        results = list(db.aql.execute(aql, bind_vars=bind_vars))
        return sum(results) / len(results) if results else 0.0
    except Exception as exc:
        logger.error("Suppressed error in learning: {}", exc)
        return 0.0
