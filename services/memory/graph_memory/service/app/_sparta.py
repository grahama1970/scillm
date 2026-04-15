"""SPARTA CPU-bound endpoints: entity extraction and batch QRA validation.

All deprecated query endpoints (status, count, stats, resume, convergence,
sample, unvalidated, stamp-evidence, validation checks) were removed 2026-03-14.
Use generic endpoints (/recall, /list, /analytics/run, /taxonomy/query) instead.
"""
from __future__ import annotations

import asyncio
import json as _json
import time as _time

from fastapi import APIRouter
from pydantic import BaseModel
from loguru import logger

router = APIRouter()


# ---------------------------------------------------------------------------
# Entity extraction
# ---------------------------------------------------------------------------


class ExtractEntitiesRequest(BaseModel):
    """Extract entities from question text."""
    text: str
    max_len: int = 500


def _sync_extract_entities(text: str, max_len: int) -> dict:
    """CPU-bound entity extraction — runs in thread pool."""
    from ...entity_extraction import extract_entities

    result = extract_entities(text[:max_len])
    if result is None:
        return {"error": "extract_entities returned None"}
    d = result.to_dict()
    if hasattr(result, "agent_view"):
        d.update(result.agent_view() or {})
    return d


@router.post("/sparta/extract-entities")
async def sparta_extract_entities(req: ExtractEntitiesRequest) -> dict:
    """Run entity extraction on text. Returns grounding evidence."""
    try:
        return await asyncio.to_thread(_sync_extract_entities, req.text, req.max_len)
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Batch QRA evidence validation (server-side gates + stamp)
# ---------------------------------------------------------------------------


class QRAValidateBatchRequest(BaseModel):
    """Validate a batch of QRAs with deterministic gates and stamp results."""
    keys: list[str] = []
    limit: int = 500


def _sync_validate_batch(keys: list[str], limit: int) -> dict:
    """CPU-bound batch validation — runs in thread pool to avoid blocking the event loop."""
    from ...arango_client import get_db
    from ...entity_extraction import extract_entities

    db = get_db()

    # Fetch QRAs to validate
    if keys:
        aql = (
            "FOR d IN sparta_qra FILTER d._key IN @keys"
            " RETURN {_key: d._key, question: d.question, control_id: d.control_id}"
        )
        items = list(db.aql.execute(aql, bind_vars={"keys": keys}))
    else:
        aql = (
            "FOR d IN sparta_qra FILTER d.evidence_validated_at == null"
            " LIMIT @limit"
            " RETURN {_key: d._key, question: d.question, control_id: d.control_id}"
        )
        items = list(db.aql.execute(aql, bind_vars={"limit": limit}))

    if not items:
        return {"processed": 0, "results": [], "remaining": 0}

    results = []
    stamps = []
    now_unix = int(_time.time())

    for qra in items:
        key = qra["_key"]
        question = qra.get("question", "")
        if not question:
            results.append({"key": key, "error": "no_question"})
            continue

        # Entity extraction
        try:
            ent = extract_entities(question[:500])
        except Exception as exc:
            logger.error("entity extraction failed for {}: {}", key, exc)
            results.append({"key": key, "error": str(exc)})
            continue

        if ent is None:
            results.append({"key": key, "error": "extract_entities returned None"})
            continue

        d = ent.to_dict()

        # Build warnings from unresolved terms
        warnings = []
        for ut in d.get("unresolved_terms", []):
            if ut.get("type") == "id_like":
                warnings.append({"term": ut.get("term", ""), "category": "fabricated_id"})
        for ms in d.get("misspellings", []):
            warnings.append({"term": ms.get("word", ms.get("term", "")), "category": "misspelling"})

        # Deterministic gates
        gates_passed = []
        gates_total = 0

        # Gate 0: Topic (security domain)
        gates_total += 1
        q_lower = question.lower()
        try:
            topic_hits = list(db.aql.execute(
                "FOR t IN domain_terms_search"
                " SEARCH ANALYZER(t.term IN TOKENS(@q, 'text_en'), 'text_en')"
                " LIMIT 1 RETURN 1",
                bind_vars={"q": q_lower},
            ))
            on_topic = len(topic_hits) > 0
        except Exception as exc:
            logger.error("on_topic classification failed: {}", exc)
            on_topic = True
        if on_topic:
            gates_passed.append("topic")

        # Gate 1: Fabricated IDs
        gates_total += 1
        fabricated = [w for w in warnings if w.get("category") == "fabricated_id"]
        gate_failed = None
        if not fabricated:
            gates_passed.append("no_fabricated_ids")
        else:
            gate_failed = "fabricated_id"

        # Gate 1a: Misspellings
        gates_total += 1
        misspellings = [w for w in warnings if w.get("category") == "misspelling"]
        if not misspellings:
            gates_passed.append("no_misspellings")

        # Gate 2: Resolution strength
        gates_total += 1
        resolution_map = d.get("resolution_map", {})
        resolved_count = sum(
            1 for v in resolution_map.values()
            if isinstance(v, dict) and v.get("exists")
        )
        if resolved_count > 0:
            gates_passed.append("has_resolved_entities")

        # Gate 3: Control IDs present
        gates_total += 1
        control_ids = d.get("all_control_ids", d.get("control_ids", []))
        if control_ids:
            gates_passed.append("has_control_ids")

        # Scoring
        score = len(gates_passed) / max(gates_total, 1)
        if gate_failed:
            verdict = "flagged"
            grade = "F"
        elif score >= 0.8:
            verdict = "valid"
            grade = "A"
        elif score >= 0.6:
            verdict = "valid"
            grade = "B"
        elif score >= 0.4:
            verdict = "review"
            grade = "C"
        else:
            verdict = "flagged"
            grade = "D"

        gate_result = {
            "verdict": verdict,
            "score": round(score, 3),
            "grade": grade,
            "gates_passed": gates_passed,
            "gates_total": gates_total,
            "gate_failed": gate_failed,
            "resolved_count": resolved_count,
            "fabricated_count": len(fabricated),
            "misspelling_count": len(misspellings),
        }

        stamp = {
            "evidence_verdict": verdict,
            "evidence_score": round(score, 3),
            "evidence_grade": grade,
            "evidence_gates_passed": len(gates_passed),
            "evidence_gates_total": gates_total,
            "evidence_gate_trace": _json.dumps(gate_result, default=str)[:2000],
            "evidence_validated_at": now_unix,
        }
        stamps.append({"key": key, "stamp": stamp})
        results.append({"key": key, **gate_result})

    # Batch stamp all validated QRAs in one AQL round-trip
    if stamps:
        try:
            db.aql.execute(
                """
                FOR item IN @stamps
                    UPDATE {_key: item.key} WITH item.stamp IN sparta_qra
                    OPTIONS {ignoreErrors: true}
                """,
                bind_vars={"stamps": stamps},
            )
        except Exception as exc:
            logger.error("Batch stamp failed: {}", exc)
            return {"processed": len(items), "results": results, "stamp_error": str(exc)}

    # Count remaining
    try:
        remaining = list(db.aql.execute(
            "RETURN LENGTH(FOR d IN sparta_qra FILTER d.evidence_validated_at == null LIMIT 100000 RETURN 1)"
        ))[0]
    except Exception as exc:
        logger.error("remaining count failed: {}", exc)
        remaining = -1

    return {"processed": len(items), "stamped": len(stamps), "results": results, "remaining": remaining}


@router.post("/sparta/qra/validate-batch")
async def sparta_qra_validate_batch(req: QRAValidateBatchRequest) -> dict:
    """Fetch unvalidated QRAs, run entity extraction + gates, stamp results."""
    return await asyncio.to_thread(_sync_validate_batch, req.keys, req.limit)
