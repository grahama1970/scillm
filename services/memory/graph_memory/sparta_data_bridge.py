"""Lock-free SPARTA pipeline data storage over ArangoDB.

Replaces DuckDB tables (controls, urls, control_urls, url_content,
url_knowledge, knowledge_anchors, relationships, url_extraction_log,
qra_generation_log, brandon_reviews) with ArangoDB collections.

Follows the same pattern as QRABridge: lazy get_db(), deterministic _key,
fire-and-forget writes.

Collection naming: sparta_<table_name>  (same DB as sparta_qra)
Key format: <prefix>__<id>  (double underscore separator)

Write operations live here (SpartaDataBridgeBase).
Read/query operations live in sparta_data_queries.py (SpartaDataQueries).
For backward compatibility, ``SpartaDataBridge`` is an alias for
``SpartaDataQueries`` which inherits from ``SpartaDataBridgeBase``.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from loguru import logger

COLLECTIONS = {
    "controls": "sparta_controls",
    "urls": "sparta_urls",
    "control_urls": "sparta_control_urls",
    "url_content": "sparta_url_content",
    "url_knowledge": "sparta_url_knowledge",
    "knowledge_anchors": "sparta_knowledge_anchors",
    "relationships": "sparta_relationships",
    "url_extraction_log": "sparta_url_extraction_log",
    "qra_gen_log": "sparta_qra_gen_log",
    "brandon_reviews": "sparta_brandon_reviews",
}


def _safe_str(val: Any) -> str:
    """Coerce to string, replacing None with empty string."""
    return str(val) if val is not None else ""


def _to_serializable(val: Any) -> Any:
    """Convert Python types that ArangoDB can't serialize (datetime, etc.)."""
    if val is None:
        return None
    import datetime
    if isinstance(val, datetime.datetime):
        return val.isoformat()
    if isinstance(val, datetime.date):
        return val.isoformat()
    if isinstance(val, (int, float, str, bool, list, dict)):
        return val
    return str(val)


def _safe_key(raw: str) -> str:
    """Sanitise a value for use as an ArangoDB _key.

    ArangoDB _key allows: letters, digits, - _ : . @ ( ) + , = ; ! * ' %
    We replace any disallowed characters with ``_``.
    """
    import re
    return re.sub(r"[^a-zA-Z0-9\-_:.@()+,=;!*'%]", "_", raw)


class SpartaDataBridgeBase:
    """Lock-free SPARTA pipeline data storage over ArangoDB (write ops)."""

    def __init__(self, db=None):
        self._db = db

    @property
    def db(self):
        if self._db is None:
            from .arango_client import get_db
            self._db = get_db()
        return self._db

    # ------------------------------------------------------------------
    # Generic helpers
    # ------------------------------------------------------------------

    def _coll(self, logical: str):
        """Return the ArangoDB collection object for a logical table name."""
        name = COLLECTIONS.get(logical, logical)
        return self.db.collection(name)

    def _upsert_one(self, coll_name: str, key: str, doc: dict) -> bool:
        """UPSERT a single document. Returns True on success."""
        try:
            doc["_key"] = key
            self.db.aql.execute(
                "UPSERT {_key: @key} INSERT @doc UPDATE @doc IN @@coll",
                bind_vars={"key": key, "doc": doc, "@coll": coll_name},
            )
            return True
        except Exception as exc:
            logger.error("upsert_one({}) failed: {}", coll_name, exc)
            return False

    def _upsert_batch(self, coll_name: str, docs: list[dict]) -> int:
        """UPSERT a batch of documents. Returns count of successful upserts."""
        if not docs:
            return 0
        try:
            cursor = self.db.aql.execute(
                """FOR d IN @docs
                   UPSERT {_key: d._key} INSERT d UPDATE d IN @@coll
                   RETURN 1""",
                bind_vars={"docs": docs, "@coll": coll_name},
            )
            return sum(1 for _ in cursor)
        except Exception as exc:
            logger.error("upsert_batch({}) failed: {}", coll_name, exc)
            return 0

    @staticmethod
    def _strip_meta(doc: dict) -> dict:
        """Remove ArangoDB internal fields."""
        doc.pop("_id", None)
        doc.pop("_rev", None)
        return doc

    # ------------------------------------------------------------------
    # Writers — controls
    # ------------------------------------------------------------------

    def upsert_controls(self, rows: list[dict]) -> int:
        """Upsert controls into sparta_controls.

        Description preservation: if the existing document has an enriched
        description (contains [BRAVE-SEARCH] or [INFERRED]) and the incoming
        description is shorter or empty, the existing description is kept.
        This prevents re-ingestion from overwriting backfilled descriptions.
        """
        coll = COLLECTIONS["controls"]
        docs = []
        for r in rows:
            cid = _safe_str(r.get("control_id"))
            if not cid:
                continue
            docs.append({
                "_key": _safe_key(f"ctrl__{cid}"),
                "control_id": cid,
                "name": r.get("name"),
                "description": r.get("description"),
                "source_framework": r.get("source_framework"),
                "source_version": r.get("source_version"),
                "source_table": r.get("source_table"),
                "control_type": r.get("control_type"),
                "parent_id": r.get("parent_id"),
                "description_preserve": r.get("description_preserve", False),
                "updated_at": int(time.time()),
            })
        return self._upsert_controls_batch(coll, docs)

    def _upsert_controls_batch(self, coll_name: str, docs: list[dict]) -> int:
        """UPSERT controls with description preservation.

        On UPDATE, keeps OLD.description when:
        1. It contains [BRAVE-SEARCH] or [INFERRED] markers and is longer, OR
        2. The document has description_preserve flag and OLD has a non-empty description

        This prevents re-ingestion from overwriting enriched or inferred descriptions
        (e.g., ISO 27001 controls whose descriptions came from Brave Search).
        """
        if not docs:
            return 0
        try:
            cursor = self.db.aql.execute(
                """FOR d IN @docs
                   UPSERT {_key: d._key} INSERT d
                   UPDATE MERGE(d, {
                       description: (
                           (LENGTH(OLD.description) > LENGTH(d.description)
                            AND (CONTAINS(OLD.description, "[BRAVE-SEARCH]")
                                 OR CONTAINS(OLD.description, "[INFERRED]")))
                           OR (d.description_preserve == true
                               AND LENGTH(OLD.description) > 0)
                       ) ? OLD.description : d.description
                   }) IN @@coll
                   RETURN 1""",
                bind_vars={"docs": docs, "@coll": coll_name},
            )
            return sum(1 for _ in cursor)
        except Exception as exc:
            logger.error("upsert_controls_batch({}) failed: {}", coll_name, exc)
            return 0

    # ------------------------------------------------------------------
    # Writers — urls & mappings
    # ------------------------------------------------------------------

    def upsert_urls(self, rows: list[dict]) -> int:
        """Upsert URLs into sparta_urls."""
        coll = COLLECTIONS["urls"]
        docs = []
        for r in rows:
            uid = r.get("url_id")
            if uid is None:
                continue
            docs.append({
                "_key": _safe_key(f"url__{uid}"),
                "url_id": uid,
                "url": r.get("url"),
                "domain": r.get("domain"),
                "updated_at": int(time.time()),
            })
        return self._upsert_batch(coll, docs)

    def upsert_control_urls(self, rows: list[dict]) -> int:
        """Upsert control↔URL mappings into sparta_control_urls."""
        coll = COLLECTIONS["control_urls"]
        docs = []
        for r in rows:
            cid = _safe_str(r.get("control_id"))
            uid = r.get("url_id")
            if not cid or uid is None:
                continue
            docs.append({
                "_key": _safe_key(f"{cid}__{uid}"),
                "control_id": cid,
                "url_id": uid,
                "updated_at": int(time.time()),
            })
        return self._upsert_batch(coll, docs)

    # ------------------------------------------------------------------
    # Writers — content & knowledge
    # ------------------------------------------------------------------

    def upsert_url_content(self, rows: list[dict]) -> int:
        """Upsert fetched URL content into sparta_url_content."""
        coll = COLLECTIONS["url_content"]
        docs = []
        for r in rows:
            uid = r.get("url_id")
            if uid is None:
                continue
            docs.append({
                "_key": _safe_key(f"uc__{uid}"),
                "url_id": uid,
                "file_path": r.get("file_path"),
                "status_code": r.get("status_code"),
                "error_message": r.get("error_message"),
                "fetched_at": _to_serializable(r.get("fetched_at")),
                "updated_at": int(time.time()),
            })
        return self._upsert_batch(coll, docs)

    def upsert_url_knowledge(self, rows: list[dict]) -> int:
        """Upsert extracted knowledge chunks into sparta_url_knowledge."""
        coll = COLLECTIONS["url_knowledge"]
        docs = []
        for r in rows:
            uid = r.get("url_id")
            idx = r.get("excerpt_index", 0)
            if uid is None:
                continue
            docs.append({
                "_key": _safe_key(f"uk__{uid}__{idx}"),
                "url_id": uid,
                "excerpt_index": idx,
                "text": r.get("text"),
                "topic": r.get("topic"),
                "excerpt_type": r.get("excerpt_type"),
                "updated_at": int(time.time()),
            })
        return self._upsert_batch(coll, docs)

    def upsert_knowledge_anchors(self, rows: list[dict]) -> int:
        """Upsert knowledge anchors into sparta_knowledge_anchors."""
        coll = COLLECTIONS["knowledge_anchors"]
        docs = []
        for r in rows:
            aid = r.get("anchor_id")
            if aid is None:
                continue
            docs.append({
                "_key": _safe_key(f"ka__{aid}"),
                "anchor_id": aid,
                "control_id": r.get("control_id"),
                "category": r.get("category"),
                "parent_context": r.get("parent_context"),
                "child_context": r.get("child_context"),
                "updated_at": int(time.time()),
            })
        return self._upsert_batch(coll, docs)

    def update_anchor_parent_context(self, anchor_id: int, parent_context: str) -> bool:
        """Update parent_context on a single knowledge anchor."""
        coll = COLLECTIONS["knowledge_anchors"]
        key = _safe_key(f"ka__{anchor_id}")
        try:
            self.db.aql.execute(
                """FOR d IN @@coll
                    FILTER d._key == @key
                    UPDATE d WITH {parent_context: @pc, updated_at: @ts} IN @@coll""",
                bind_vars={"@coll": coll, "key": key, "pc": parent_context, "ts": int(time.time())},
            )
            return True
        except Exception as exc:
            logger.error("update_anchor_parent_context({}) failed: {}", anchor_id, exc)
            return False

    def batch_update_anchor_parent_context(self, updates: list[tuple[str, int]]) -> int:
        """Batch-update parent_context on multiple anchors.

        Args:
            updates: list of (parent_context, anchor_id) tuples.

        Returns:
            Number of successful updates.
        """
        coll = COLLECTIONS["knowledge_anchors"]
        docs = []
        for parent_context, anchor_id in updates:
            docs.append({
                "_key": _safe_key(f"ka__{anchor_id}"),
                "parent_context": parent_context,
                "updated_at": int(time.time()),
            })
        if not docs:
            return 0
        try:
            cursor = self.db.aql.execute(
                """FOR d IN @docs
                    UPDATE {_key: d._key} WITH {parent_context: d.parent_context, updated_at: d.updated_at} IN @@coll
                    RETURN 1""",
                bind_vars={"docs": docs, "@coll": coll},
            )
            return sum(1 for _ in cursor)
        except Exception as exc:
            logger.error("batch_update_anchor_parent_context failed: {}", exc)
            return 0

    # ------------------------------------------------------------------
    # Writers — relationships
    # ------------------------------------------------------------------

    def upsert_relationships(self, rows: list[dict]) -> int:
        """Upsert control relationships into sparta_relationships."""
        coll = COLLECTIONS["relationships"]
        docs = []
        for r in rows:
            src = _safe_str(r.get("source_control_id"))
            tgt = _safe_str(r.get("target_control_id"))
            if not src or not tgt:
                continue
            docs.append({
                "_key": _safe_key(f"{src}__{tgt}"),
                "source_control_id": src,
                "target_control_id": tgt,
                "method": r.get("method"),
                "combined_score": r.get("combined_score"),
                "updated_at": int(time.time()),
            })
        return self._upsert_batch(coll, docs)

    # ------------------------------------------------------------------
    # Writers — logs
    # ------------------------------------------------------------------

    def upsert_url_extraction_log(self, rows: list[dict]) -> int:
        """Upsert URL extraction log entries."""
        coll = COLLECTIONS["url_extraction_log"]
        docs = []
        for r in rows:
            uid = r.get("url_id")
            if uid is None:
                continue
            docs.append({
                "_key": _safe_key(f"el__{uid}"),
                "url_id": uid,
                "source_quality": r.get("source_quality"),
                "error": r.get("error"),
                "excerpt_count": r.get("excerpt_count"),
                "extracted_at": _to_serializable(r.get("extracted_at")),
                "updated_at": int(time.time()),
            })
        return self._upsert_batch(coll, docs)

    def upsert_qra_gen_log(self, rows: list[dict]) -> int:
        """Upsert QRA generation log entries."""
        coll = COLLECTIONS["qra_gen_log"]
        docs = []
        for r in rows:
            log_id = r.get("log_id")
            if log_id is None:
                continue
            docs.append({
                "_key": _safe_key(f"ql__{log_id}"),
                "log_id": log_id,
                "control_id": r.get("control_id"),
                "ok": r.get("ok"),
                "request_payload": r.get("request_payload"),
                "response_payload": r.get("response_payload"),
                "generated_at": _to_serializable(r.get("generated_at")),
                "updated_at": int(time.time()),
            })
        return self._upsert_batch(coll, docs)

    def upsert_brandon_reviews(self, rows: list[dict]) -> int:
        """Upsert Brandon reviews."""
        coll = COLLECTIONS["brandon_reviews"]
        docs = []
        for r in rows:
            qra_id = _safe_str(r.get("qra_id"))
            if not qra_id:
                continue
            docs.append({
                "_key": _safe_key(f"br__{qra_id}"),
                "qra_id": qra_id,
                "grade": r.get("grade"),
                "notes": r.get("notes"),
                "category": r.get("category"),
                "reviewer": r.get("reviewer"),
                "reviewed_at": _to_serializable(r.get("reviewed_at")),
                "updated_at": int(time.time()),
            })
        return self._upsert_batch(coll, docs)


# Backward-compatible alias: SpartaDataBridge includes both write and
# read operations via inheritance.
from .sparta_data_queries import SpartaDataQueries  # noqa: E402

SpartaDataBridge = SpartaDataQueries
