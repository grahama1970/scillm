"""Lock-free QRA storage over ArangoDB. Replaces DuckDB qra table.

DuckDB exclusive file locks block concurrent readers during Stage 12 runs.
This bridge stores QRAs in ArangoDB's ``sparta_qra`` collection so monitors,
CLI, validators, and backfill scripts can read/write simultaneously.

Collection: sparta_qra
Key format: qra__{run_id}__{qra_id}  (same __ separator as SpartaMetricsBridge)

Document schema (flat):
    _key, qra_id, run_id, batch_id, control_id, relationship_id,
    question, question_type, questioner_persona, reasoning, answer,
    citations (array), confidence, grounding_score, mind (array),
    model_used, reasoning_grade, reasoning_corrected,
    evidence_verdict, evidence_score, evidence_grade, evidence_gates_passed,
    evidence_gates_total, evidence_correction_rounds, evidence_gate_trace (array),
    evidence_validated_at,
    created_at, updated_at

Compat note: legacy docs may have ``tactical_tags`` or ``conceptual_tags`` fields.
Use ``get_mind_tags()`` from ``graph_memory.lessons.store`` to read either form.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Set

from loguru import logger

COLLECTION = "sparta_qra"

# Evidence case metadata fields (all optional, None if not yet validated)
_EVIDENCE_FIELDS = (
    "evidence_verdict",            # satisfied|not_satisfied|inconclusive
    "evidence_score",              # 0.0-1.0
    "evidence_grade",              # A+|A|B|C|F
    "evidence_gates_passed",       # int
    "evidence_gates_total",        # int
    "evidence_correction_rounds",  # 0-3
    "evidence_gate_trace",         # list[dict]
    "evidence_validated_at",       # epoch int
)


def _make_key(run_id: str, qra_id: int) -> str:
    """Deterministic _key for a QRA document."""
    return f"qra__{run_id}__{qra_id}"


def _ensure_list(val: Any) -> list:
    """Coerce stringified JSON or None to a list."""
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    return []


def _to_epoch(val: Any) -> Any:
    """Convert datetime to epoch int, pass through ints/floats/None."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    import datetime
    if isinstance(val, datetime.datetime):
        return int(val.timestamp())
    return val


def _strip_arango_meta(doc: dict) -> dict:
    """Remove ArangoDB internal fields from a document."""
    doc.pop("_id", None)
    doc.pop("_rev", None)
    return doc


class QRABridge:
    """Lock-free QRA storage over ArangoDB. Replaces DuckDB qra table."""

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

    def upsert_qra(self, run_id: str, qra: dict) -> bool:
        """UPSERT a single QRA document. Returns True on success."""
        try:
            now = int(time.time())
            qra_id = qra.get("qra_id")
            if qra_id is None:
                logger.debug("upsert_qra: missing qra_id")
                return False
            key = _make_key(run_id, qra_id)
            doc = {
                "_key": key,
                "qra_id": qra_id,
                "run_id": run_id,
                "batch_id": qra.get("batch_id"),
                "control_id": qra.get("control_id"),
                "relationship_id": qra.get("relationship_id"),
                "question": qra.get("question", ""),
                "question_type": qra.get("question_type", ""),
                "questioner_persona": qra.get("questioner_persona", ""),
                "reasoning": qra.get("reasoning", ""),
                "answer": qra.get("answer", ""),
                "citations": _ensure_list(qra.get("citations")),
                "confidence": qra.get("confidence", ""),
                "grounding_score": qra.get("grounding_score"),
                # Mind = 8 SPARTA tactical tags (Detect/Evade/Exploit/Harden/
                # Isolate/Model/Persist/Restore).  Read via get_mind_tags().
                # Compat: accept legacy tactical_tags or conceptual_tags as fallback.
                "mind": _ensure_list(
                    qra.get("mind")
                    or qra.get("tactical_tags")
                    or qra.get("conceptual_tags")
                ),
                "model_used": qra.get("model_used", ""),
                "reasoning_grade": qra.get("reasoning_grade"),
                "reasoning_corrected": qra.get("reasoning_corrected"),
                "domain": qra.get("domain"),
                "persona_scope": qra.get("persona_scope"),
                "created_at": _to_epoch(qra.get("created_at")) or now,
                "updated_at": now,
            }
            # Evidence case metadata (optional — None if not yet validated)
            for ef in _EVIDENCE_FIELDS:
                val = qra.get(ef)
                if val is not None:
                    doc[ef] = val
            self.db.aql.execute(
                "UPSERT {_key: @key} INSERT @doc UPDATE @doc IN @@coll",
                bind_vars={"key": key, "doc": doc, "@coll": COLLECTION},
            )
            return True
        except Exception as exc:
            logger.error("upsert_qra failed: {}", exc)
            return False

    def upsert_batch(self, run_id: str, qras: list[dict]) -> int:
        """UPSERT a batch of QRAs. Returns count of successful upserts."""
        if not qras:
            return 0
        try:
            now = int(time.time())
            docs = []
            for qra in qras:
                qra_id = qra.get("qra_id")
                if qra_id is None:
                    continue
                key = _make_key(run_id, qra_id)
                d = {
                    "_key": key,
                    "qra_id": qra_id,
                    "run_id": run_id,
                    "batch_id": qra.get("batch_id"),
                    "control_id": qra.get("control_id"),
                    "relationship_id": qra.get("relationship_id"),
                    "question": qra.get("question", ""),
                    "question_type": qra.get("question_type", ""),
                    "questioner_persona": qra.get("questioner_persona", ""),
                    "reasoning": qra.get("reasoning", ""),
                    "answer": qra.get("answer", ""),
                    "citations": _ensure_list(qra.get("citations")),
                    "confidence": qra.get("confidence", ""),
                    "grounding_score": qra.get("grounding_score"),
                    # Mind = 8 SPARTA tactical tags. Compat fallback reads legacy fields.
                    "mind": _ensure_list(
                        qra.get("mind")
                        or qra.get("tactical_tags")
                        or qra.get("conceptual_tags")
                    ),
                    "model_used": qra.get("model_used", ""),
                    "reasoning_grade": qra.get("reasoning_grade"),
                    "reasoning_corrected": qra.get("reasoning_corrected"),
                    "domain": qra.get("domain"),
                    "persona_scope": qra.get("persona_scope"),
                    "created_at": _to_epoch(qra.get("created_at")) or now,
                    "updated_at": now,
                }
                # Evidence case metadata (optional)
                for ef in _EVIDENCE_FIELDS:
                    val = qra.get(ef)
                    if val is not None:
                        d[ef] = val
                docs.append(d)
            if not docs:
                return 0
            cursor = self.db.aql.execute(
                """
                FOR d IN @docs
                    UPSERT {_key: d._key} INSERT d UPDATE d IN @@coll
                    RETURN 1
                """,
                bind_vars={"docs": docs, "@coll": COLLECTION},
            )
            return sum(1 for _ in cursor)
        except Exception as exc:
            logger.error("upsert_batch failed: {}", exc)
            return 0

    def update_grade(self, run_id: str, qra_id: int, reasoning_grade: str) -> bool:
        """Update reasoning_grade for a single QRA."""
        try:
            key = _make_key(run_id, qra_id)
            now = int(time.time())
            self.db.aql.execute(
                """
                UPDATE {_key: @key} WITH {
                    reasoning_grade: @grade, updated_at: @now
                } IN @@coll OPTIONS {ignoreErrors: true}
                """,
                bind_vars={
                    "key": key, "grade": reasoning_grade,
                    "now": now, "@coll": COLLECTION,
                },
            )
            return True
        except Exception as exc:
            logger.error("update_grade failed: {}", exc)
            return False

    def update_answer(
        self, run_id: str, qra_id: int,
        answer: str, grounding_score: float,
        reasoning_corrected: Optional[str] = None,
    ) -> bool:
        """Update answer, grounding_score, and reasoning_corrected."""
        try:
            key = _make_key(run_id, qra_id)
            now = int(time.time())
            patch = {
                "answer": answer,
                "grounding_score": grounding_score,
                "updated_at": now,
            }
            if reasoning_corrected is not None:
                patch["reasoning_corrected"] = reasoning_corrected
            self.db.aql.execute(
                "UPDATE {_key: @key} WITH @patch IN @@coll OPTIONS {ignoreErrors: true}",
                bind_vars={"key": key, "patch": patch, "@coll": COLLECTION},
            )
            return True
        except Exception as exc:
            logger.error("update_answer failed: {}", exc)
            return False

    def update_evidence(self, run_id: str, qra_id: int, evidence: dict) -> bool:
        """Stamp evidence case metadata onto an existing QRA."""
        try:
            key = _make_key(run_id, qra_id)
            now = int(time.time())
            patch = {"updated_at": now}
            for ef in _EVIDENCE_FIELDS:
                val = evidence.get(ef)
                if val is not None:
                    patch[ef] = val
            if "evidence_validated_at" not in patch:
                patch["evidence_validated_at"] = now
            self.db.aql.execute(
                "UPDATE {_key: @key} WITH @patch IN @@coll OPTIONS {ignoreErrors: true}",
                bind_vars={"key": key, "patch": patch, "@coll": COLLECTION},
            )
            return True
        except Exception as exc:
            logger.error("update_evidence failed: {}", exc)
            return False

    def delete_for_controls(self, run_id: str, control_ids: list[str]) -> int:
        """Delete all QRAs for the given control_ids in a run."""
        if not control_ids:
            return 0
        try:
            cursor = self.db.aql.execute(
                """
                FOR doc IN @@coll
                    FILTER doc.run_id == @rid AND doc.control_id IN @cids
                    REMOVE doc IN @@coll
                    RETURN 1
                """,
                bind_vars={
                    "rid": run_id, "cids": control_ids,
                    "@coll": COLLECTION,
                },
            )
            return sum(1 for _ in cursor)
        except Exception as exc:
            logger.error("delete_for_controls failed: {}", exc)
            return 0

    # ------------------------------------------------------------------
    # Readers
    # ------------------------------------------------------------------

    def count(self, run_id: str) -> int:
        """Count QRAs for a run."""
        try:
            cursor = self.db.aql.execute(
                "RETURN LENGTH(FOR d IN @@coll FILTER d.run_id == @rid RETURN 1)",
                bind_vars={"rid": run_id, "@coll": COLLECTION},
            )
            return list(cursor)[0]
        except Exception as exc:
            logger.error("count failed: {}", exc)
            return 0

    def avg_grounding(self, run_id: str) -> float:
        """Average grounding_score for a run."""
        try:
            cursor = self.db.aql.execute(
                """
                RETURN AVG(
                    FOR d IN @@coll
                        FILTER d.run_id == @rid AND d.grounding_score != null
                        RETURN d.grounding_score
                )
                """,
                bind_vars={"rid": run_id, "@coll": COLLECTION},
            )
            val = list(cursor)[0]
            return float(val) if val is not None else 0.0
        except Exception as exc:
            logger.error("avg_grounding failed: {}", exc)
            return 0.0

    def max_ids(self, run_id: str) -> dict:
        """Get max qra_id and max batch_id for a run (for Stage 12 resume)."""
        try:
            cursor = self.db.aql.execute(
                """
                RETURN {
                    max_qra_id: MAX(FOR d IN @@coll FILTER d.run_id == @rid RETURN d.qra_id),
                    max_batch_id: MAX(FOR d IN @@coll FILTER d.run_id == @rid RETURN d.batch_id)
                }
                """,
                bind_vars={"rid": run_id, "@coll": COLLECTION},
            )
            result = list(cursor)[0]
            return {
                "max_qra_id": result.get("max_qra_id") or 0,
                "max_batch_id": result.get("max_batch_id") or 0,
            }
        except Exception as exc:
            logger.error("max_ids failed: {}", exc)
            return {"max_qra_id": 0, "max_batch_id": 0}

    def distinct_relationship_ids(self, run_id: str) -> set:
        """Get distinct relationship_ids for a run."""
        try:
            cursor = self.db.aql.execute(
                """
                FOR d IN @@coll
                    FILTER d.run_id == @rid AND d.relationship_id != null
                    COLLECT rid = d.relationship_id
                    RETURN rid
                """,
                bind_vars={"rid": run_id, "@coll": COLLECTION},
            )
            return set(cursor)
        except Exception as exc:
            logger.error("distinct_relationship_ids failed: {}", exc)
            return set()

    def distinct_control_ids(self, run_id: str) -> set:
        """Get distinct control_ids for a run."""
        try:
            cursor = self.db.aql.execute(
                """
                FOR d IN @@coll
                    FILTER d.run_id == @rid AND d.control_id != null
                    COLLECT cid = d.control_id
                    RETURN cid
                """,
                bind_vars={"rid": run_id, "@coll": COLLECTION},
            )
            return set(cursor)
        except Exception as exc:
            logger.error("distinct_control_ids failed: {}", exc)
            return set()

    def get_by_control(
        self, run_id: str, control_id: str, limit: int = 50
    ) -> list[dict]:
        """Get QRAs for a specific control_id."""
        try:
            cursor = self.db.aql.execute(
                """
                FOR d IN @@coll
                    FILTER d.run_id == @rid AND d.control_id == @cid
                    LIMIT @lim
                    RETURN d
                """,
                bind_vars={
                    "rid": run_id, "cid": control_id,
                    "lim": limit, "@coll": COLLECTION,
                },
            )
            return [_strip_arango_meta(doc) for doc in cursor]
        except Exception as exc:
            logger.error("get_by_control failed: {}", exc)
            return []

    def sample_random(
        self, run_id: str, n: int = 50, where: Optional[str] = None,
    ) -> list[dict]:
        """Sample random QRAs from a run.

        ``where`` is an optional raw AQL filter fragment (e.g.
        ``"d.mind != null"``).  Use with care — only for
        trusted internal callers.
        """
        try:
            where_clause = f"AND {where}" if where else ""
            query = f"""
                FOR d IN @@coll
                    FILTER d.run_id == @rid {where_clause}
                    SORT RAND()
                    LIMIT @n
                    RETURN d
            """
            cursor = self.db.aql.execute(
                query,
                bind_vars={"rid": run_id, "n": n, "@coll": COLLECTION},
            )
            return [_strip_arango_meta(doc) for doc in cursor]
        except Exception as exc:
            logger.error("sample_random failed: {}", exc)
            return []

    def get_null_grades(self, run_id: str, limit: int = 10000) -> list[dict]:
        """Get QRAs with NULL reasoning_grade (for backfill)."""
        try:
            cursor = self.db.aql.execute(
                """
                FOR d IN @@coll
                    FILTER d.run_id == @rid AND d.reasoning_grade == null
                    LIMIT @lim
                    RETURN d
                """,
                bind_vars={
                    "rid": run_id, "lim": limit, "@coll": COLLECTION,
                },
            )
            return [_strip_arango_meta(doc) for doc in cursor]
        except Exception as exc:
            logger.error("get_null_grades failed: {}", exc)
            return []

    def count_by_grade(self, run_id: str) -> dict[str, int]:
        """Count QRAs grouped by reasoning_grade."""
        try:
            cursor = self.db.aql.execute(
                """
                FOR d IN @@coll
                    FILTER d.run_id == @rid
                    COLLECT grade = (d.reasoning_grade || "NULL")
                    WITH COUNT INTO cnt
                    RETURN {grade, cnt}
                """,
                bind_vars={"rid": run_id, "@coll": COLLECTION},
            )
            return {row["grade"]: row["cnt"] for row in cursor}
        except Exception as exc:
            logger.error("count_by_grade failed: {}", exc)
            return {}

    def get_stats(self, run_id: str) -> dict:
        """Combined stats: count, avg_grounding, grade distribution."""
        return {
            "run_id": run_id,
            "count": self.count(run_id),
            "avg_grounding": self.avg_grounding(run_id),
            "grades": self.count_by_grade(run_id),
        }

    def get_resume_info(self, run_id: str) -> dict:
        """Everything Stage 12 needs to resume: max IDs + distinct sets."""
        ids = self.max_ids(run_id)
        return {
            "run_id": run_id,
            "max_qra_id": ids["max_qra_id"],
            "max_batch_id": ids["max_batch_id"],
            "distinct_relationship_count": len(self.distinct_relationship_ids(run_id)),
            "distinct_control_count": len(self.distinct_control_ids(run_id)),
        }
