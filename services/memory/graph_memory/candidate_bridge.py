"""Staging bridge for WARN-grade QRA candidates awaiting human review.

Modeled on QRABridge. QRA documents flow through assess_qra():
  PASS → QRABridge.upsert_qra() (auto, no human)
  WARN → CandidateBridge.stage() → sparta_qra_candidates (human review)
  FAIL → CandidateBridge.log_rejected() → rejected_qras (GRPO training data)

Human reviewers use /qra-review TUI to accept/reject/amend candidates.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from loguru import logger

CANDIDATES_COLLECTION = "sparta_qra_candidates"
REJECTED_COLLECTION = "rejected_qras"


class CandidateBridge:
    """Read/write API for the QRA staging collection."""

    def __init__(self, db=None):
        self._db = db

    @property
    def db(self):
        if self._db is None:
            from .arango_client import get_db
            self._db = get_db()
        return self._db

    # ------------------------------------------------------------------
    # Writers
    # ------------------------------------------------------------------

    def stage(self, qra_doc: dict, assessment: dict) -> bool:
        """Stage a WARN-grade QRA candidate for human review.

        Args:
            qra_doc: The QRA document (same schema as sparta_qra).
            assessment: The assess_qra() result dict.

        Returns:
            True on success.
        """
        try:
            now = int(time.time())
            key = qra_doc.get("_key", f"cand__{now}")
            doc = {
                **qra_doc,
                "_key": key,
                "assessment_grade": assessment.get("grade", "WARN"),
                "assessment_notes": assessment.get("notes", []),
                "assessment_framework": assessment.get("framework", ""),
                "assessment_grounding": assessment.get("grounding_check", 0.0),
                "assessment_anchoring_ok": assessment.get("anchoring_ok", False),
                "assessment_space_terms_ok": assessment.get("space_terms_ok", False),
                "assessment_taxonomy_ok": assessment.get("taxonomy_ok", False),
                "status": "pending",
                "reviewed_by": None,
                "reviewed_at": None,
                "original_answer": None,
                "staged_at": now,
            }
            self.db.aql.execute(
                "UPSERT {_key: @key} INSERT @doc UPDATE @doc IN @@coll",
                bind_vars={"key": key, "doc": doc, "@coll": CANDIDATES_COLLECTION},
            )
            logger.info("Staged candidate {} (grade={})", key, assessment.get("grade"))
            return True
        except Exception as exc:
            logger.error("stage failed: {}", exc)
            return False

    def log_rejected(self, qra_doc: dict, assessment: dict) -> bool:
        """Log a FAIL-grade QRA to rejected_qras (for GRPO training data).

        Args:
            qra_doc: The QRA document.
            assessment: The assess_qra() result dict.

        Returns:
            True on success.
        """
        try:
            now = int(time.time())
            key = qra_doc.get("_key", f"rej__{now}")
            doc = {
                **qra_doc,
                "_key": key,
                "assessment_grade": "FAIL",
                "assessment_notes": assessment.get("notes", []),
                "assessment_framework": assessment.get("framework", ""),
                "rejected_at": now,
            }
            self.db.aql.execute(
                "UPSERT {_key: @key} INSERT @doc UPDATE @doc IN @@coll",
                bind_vars={"key": key, "doc": doc, "@coll": REJECTED_COLLECTION},
            )
            return True
        except Exception as exc:
            logger.error("log_rejected failed: {}", exc)
            return False

    def accept(self, key: str, reviewer: str) -> bool:
        """Accept a candidate: move from staging to sparta_qra.

        Returns True on success.
        """
        try:
            coll = self.db.collection(CANDIDATES_COLLECTION)
            doc = coll.get(key)
            if not doc:
                logger.warning("accept: candidate {} not found", key)
                return False

            now = int(time.time())

            # Strip staging metadata before promoting
            qra_doc = {k: v for k, v in doc.items()
                       if not k.startswith("assessment_")
                       and k not in ("status", "reviewed_by", "reviewed_at",
                                     "original_answer", "staged_at",
                                     "_id", "_rev")}
            qra_doc["_promoted_from"] = "candidate_review"
            qra_doc["_promoted_by"] = reviewer
            qra_doc["_promoted_at"] = now

            # Insert into sparta_qra
            self.db.aql.execute(
                "UPSERT {_key: @key} INSERT @doc UPDATE @doc IN @@coll",
                bind_vars={"key": key, "doc": qra_doc, "@coll": "sparta_qra"},
            )

            # Update candidate status
            coll.update({"_key": key, "status": "accepted",
                         "reviewed_by": reviewer, "reviewed_at": now})

            logger.info("Accepted candidate {} (reviewer={})", key, reviewer)
            return True
        except Exception as exc:
            logger.error("accept failed: {}", exc)
            return False

    def reject(self, key: str, reviewer: str, reason: str = "") -> bool:
        """Reject a candidate (stays in staging with status=rejected)."""
        try:
            now = int(time.time())
            coll = self.db.collection(CANDIDATES_COLLECTION)
            coll.update({
                "_key": key,
                "status": "rejected",
                "reviewed_by": reviewer,
                "reviewed_at": now,
                "rejection_reason": reason,
            })
            logger.info("Rejected candidate {} (reviewer={})", key, reviewer)
            return True
        except Exception as exc:
            logger.error("reject failed: {}", exc)
            return False

    def amend(self, key: str, reviewer: str, new_answer: str) -> dict:
        """Amend a candidate's answer and re-assess.

        Returns the new assessment dict (caller decides whether to accept).
        """
        try:
            coll = self.db.collection(CANDIDATES_COLLECTION)
            doc = coll.get(key)
            if not doc:
                return {"grade": "FAIL", "notes": [f"Candidate {key} not found"]}

            now = int(time.time())

            # Save original answer on first amendment
            original = doc.get("original_answer") or doc.get("answer", "")

            # Re-assess with new answer
            from .quality.assess import assess_qra
            amended_doc = {**doc, "answer": new_answer}
            assessment = assess_qra(amended_doc)

            # Update the candidate
            update = {
                "_key": key,
                "answer": new_answer,
                "original_answer": original,
                "assessment_grade": assessment.get("grade", "WARN"),
                "assessment_notes": assessment.get("notes", []),
                "status": "amended",
                "reviewed_by": reviewer,
                "reviewed_at": now,
            }
            coll.update(update)

            logger.info("Amended candidate {} → grade={}", key, assessment.get("grade"))
            return assessment
        except Exception as exc:
            logger.error("amend failed: {}", exc)
            return {"grade": "FAIL", "notes": [str(exc)]}

    def bulk_reject(self, filter_aql: str, reviewer: str) -> int:
        """Bulk reject candidates matching an AQL filter fragment.

        Only whitelisted fields are allowed in the filter to prevent injection.
        The filter_aql is a pre-sanitized AQL fragment like:
            'd.assessment_framework == "CWE" AND d.assessment_grounding < 0.55'
        """
        try:
            now = int(time.time())
            query = f"""
                FOR d IN @@coll
                    FILTER d.status == "pending" AND ({filter_aql})
                    UPDATE d WITH {{
                        status: "rejected",
                        reviewed_by: @reviewer,
                        reviewed_at: @now,
                        rejection_reason: "bulk_reject"
                    }} IN @@coll
                    RETURN 1
            """
            cursor = self.db.aql.execute(
                query,
                bind_vars={
                    "@coll": CANDIDATES_COLLECTION,
                    "reviewer": reviewer,
                    "now": now,
                },
            )
            count = sum(1 for _ in cursor)
            logger.info("Bulk rejected {} candidates (reviewer={})", count, reviewer)
            return count
        except Exception as exc:
            logger.error("bulk_reject failed: {}", exc)
            return 0

    # ------------------------------------------------------------------
    # Readers
    # ------------------------------------------------------------------

    def pending_count(self) -> int:
        """Count pending candidates."""
        try:
            cursor = self.db.aql.execute(
                'RETURN LENGTH(FOR d IN @@coll FILTER d.status == "pending" RETURN 1)',
                bind_vars={"@coll": CANDIDATES_COLLECTION},
            )
            return list(cursor)[0]
        except Exception as exc:
            logger.error("pending_count failed: {}", exc)
            return 0

    def get_pending(
        self, limit: int = 50, offset: int = 0, framework: Optional[str] = None,
    ) -> List[dict]:
        """Get pending candidates, optionally filtered by framework."""
        try:
            fw_filter = ""
            bind: Dict[str, Any] = {
                "@coll": CANDIDATES_COLLECTION,
                "lim": limit,
                "off": offset,
            }
            if framework:
                fw_filter = "AND d.assessment_framework == @fw"
                bind["fw"] = framework

            cursor = self.db.aql.execute(
                f"""
                FOR d IN @@coll
                    FILTER d.status == "pending" {fw_filter}
                    SORT d.staged_at ASC
                    LIMIT @off, @lim
                    RETURN d
                """,
                bind_vars=bind,
            )
            return [self._strip_meta(doc) for doc in cursor]
        except Exception as exc:
            logger.error("get_pending failed: {}", exc)
            return []

    def get_stats(self) -> dict:
        """Counts by status, framework, and grade."""
        try:
            cursor = self.db.aql.execute(
                """
                LET by_status = (
                    FOR d IN @@coll
                        COLLECT status = d.status WITH COUNT INTO cnt
                        RETURN {status, cnt}
                )
                LET by_framework = (
                    FOR d IN @@coll
                        FILTER d.status == "pending"
                        COLLECT fw = d.assessment_framework WITH COUNT INTO cnt
                        RETURN {fw, cnt}
                )
                RETURN {
                    by_status: by_status,
                    by_framework: by_framework,
                    total: LENGTH(@@coll)
                }
                """,
                bind_vars={"@coll": CANDIDATES_COLLECTION},
            )
            return list(cursor)[0]
        except Exception as exc:
            logger.error("get_stats failed: {}", exc)
            return {"by_status": [], "by_framework": [], "total": 0}

    @staticmethod
    def _strip_meta(doc: dict) -> dict:
        doc.pop("_id", None)
        doc.pop("_rev", None)
        return doc
