"""Hybrid DuckDB->ArangoDB metrics bridge for SPARTA pipeline.

DuckDB uses exclusive file locks (single writer). This bridge pushes live
metrics to ArangoDB so monitor daemons, CLI, and agents can read pipeline
state without hitting DuckDB lock contention.

Collection: sparta_metrics (3 doc_types: live_state, batch_stats, checkpoint)
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from loguru import logger

# TTL constants (seconds)
_BATCH_TTL = 90 * 86400   # 90 days
_CKPT_TTL = 180 * 86400   # 180 days

COLLECTION = "sparta_metrics"


class SpartaMetricsBridge:
    """Thin bridge for SPARTA metrics over ArangoDB. No file locks."""

    def __init__(self, db=None):
        self._db = db

    @property
    def db(self):
        if self._db is None:
            from .arango_client import get_db
            self._db = get_db()
        return self._db

    # ------------------------------------------------------------------
    # Writers (fire-and-forget, never raise)
    # ------------------------------------------------------------------

    def push_live_state(
        self,
        run_id: str,
        qra_count: int = 0,
        avg_grounding: float = 0.0,
        current_batch_id: int = 0,
        qra_rate_per_min: float = 0.0,
        pass_pct: float = 0.0,
        warn_pct: float = 0.0,
        fail_pct: float = 0.0,
        status: str = "running",
        extra: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """UPSERT singleton live_state for a run. Called by heartbeat."""
        try:
            now = int(time.time())
            key = f"live__{run_id}"
            doc = {
                "_key": key,
                "doc_type": "live_state",
                "run_id": run_id,
                "qra_count": qra_count,
                "avg_grounding": avg_grounding,
                "current_batch_id": current_batch_id,
                "qra_rate_per_min": qra_rate_per_min,
                "pass_pct": pass_pct,
                "warn_pct": warn_pct,
                "fail_pct": fail_pct,
                "pid": os.getpid(),
                "status": status,
                "updated_at": now,
            }
            if extra:
                doc.update(extra)
            self.db.aql.execute(
                "UPSERT {_key: @key} INSERT @doc UPDATE @doc IN @@coll",
                bind_vars={"key": key, "doc": doc, "@coll": COLLECTION},
            )
            return True
        except Exception as exc:
            logger.error("push_live_state failed: {}", exc)
            return False

    def push_batch_stats(
        self,
        run_id: str,
        batch_id: int,
        pass_pct: float = 0.0,
        warn_pct: float = 0.0,
        fail_pct: float = 0.0,
        framework_breakdown: Optional[Dict[str, float]] = None,
        batch_size: int = 50,
        duration_s: float = 0.0,
        extra: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Insert batch_stats document with 90-day TTL."""
        try:
            now = int(time.time())
            key = f"batch__{run_id}__{batch_id}"
            doc = {
                "_key": key,
                "doc_type": "batch_stats",
                "run_id": run_id,
                "batch_id": batch_id,
                "pass_pct": pass_pct,
                "warn_pct": warn_pct,
                "fail_pct": fail_pct,
                "framework_breakdown": framework_breakdown or {},
                "batch_size": batch_size,
                "duration_s": duration_s,
                "created_at": now,
                "expires_at": now + _BATCH_TTL,
            }
            if extra:
                doc.update(extra)
            self.db.aql.execute(
                "UPSERT {_key: @key} INSERT @doc UPDATE @doc IN @@coll",
                bind_vars={"key": key, "doc": doc, "@coll": COLLECTION},
            )
            return True
        except Exception as exc:
            logger.error("push_batch_stats failed: {}", exc)
            return False

    def push_checkpoint(
        self,
        run_id: str,
        qra_count: int,
        deficiencies: int = 0,
        brandon_verdicts: Optional[Dict[str, int]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Insert checkpoint document with 180-day TTL."""
        try:
            now = int(time.time())
            key = f"ckpt__{run_id}__{qra_count}"
            doc = {
                "_key": key,
                "doc_type": "checkpoint",
                "run_id": run_id,
                "qra_count": qra_count,
                "deficiencies": deficiencies,
                "brandon_verdicts": brandon_verdicts or {},
                "created_at": now,
                "expires_at": now + _CKPT_TTL,
            }
            if extra:
                doc.update(extra)
            self.db.aql.execute(
                "UPSERT {_key: @key} INSERT @doc UPDATE @doc IN @@coll",
                bind_vars={"key": key, "doc": doc, "@coll": COLLECTION},
            )
            return True
        except Exception as exc:
            logger.error("push_checkpoint failed: {}", exc)
            return False

    # ------------------------------------------------------------------
    # Readers
    # ------------------------------------------------------------------

    def get_live_state(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Get live_state for a specific run."""
        try:
            key = f"live__{run_id}"
            coll = self.db.collection(COLLECTION)
            if coll.has(key):
                doc = coll.get(key)
                doc.pop("_id", None)
                doc.pop("_rev", None)
                return doc
            return None
        except Exception as exc:
            logger.error("get_live_state failed: {}", exc)
            return None

    def get_latest_live_state(self) -> Optional[Dict[str, Any]]:
        """Get the most recently updated live_state across all runs."""
        try:
            cursor = self.db.aql.execute(
                """
                FOR doc IN @@coll
                    FILTER doc.doc_type == "live_state"
                    SORT doc.updated_at DESC
                    LIMIT 1
                    RETURN doc
                """,
                bind_vars={"@coll": COLLECTION},
            )
            results = list(cursor)
            if results:
                doc = results[0]
                doc.pop("_id", None)
                doc.pop("_rev", None)
                return doc
            return None
        except Exception as exc:
            logger.error("get_latest_live_state failed: {}", exc)
            return None

    def get_convergence_history(
        self, run_id: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Get checkpoint history for convergence tracking."""
        try:
            cursor = self.db.aql.execute(
                """
                FOR doc IN @@coll
                    FILTER doc.doc_type == "checkpoint" AND doc.run_id == @rid
                    SORT doc.created_at DESC
                    LIMIT @lim
                    RETURN doc
                """,
                bind_vars={"@coll": COLLECTION, "rid": run_id, "lim": limit},
            )
            results = []
            for doc in cursor:
                doc.pop("_id", None)
                doc.pop("_rev", None)
                results.append(doc)
            return results
        except Exception as exc:
            logger.error("get_convergence_history failed: {}", exc)
            return []

    def get_batch_history(
        self, run_id: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get recent batch stats for a run."""
        try:
            cursor = self.db.aql.execute(
                """
                FOR doc IN @@coll
                    FILTER doc.doc_type == "batch_stats" AND doc.run_id == @rid
                    SORT doc.created_at DESC
                    LIMIT @lim
                    RETURN doc
                """,
                bind_vars={"@coll": COLLECTION, "rid": run_id, "lim": limit},
            )
            results = []
            for doc in cursor:
                doc.pop("_id", None)
                doc.pop("_rev", None)
                results.append(doc)
            return results
        except Exception as exc:
            logger.error("get_batch_history failed: {}", exc)
            return []
