"""Requirement-to-control matching endpoint.

POST /match/requirement — match single requirement to SPARTA controls
POST /match/batch — batch match from proof_jobs queue
GET /match/review-queue — pending review items
GET /match/coverage — matching statistics

## Traceability vs Verification

This module provides TRACEABILITY — linking requirements to controls based on
semantic similarity, crosswalk chains, and QRA evidence. Matching answers:
"Which controls are related to this requirement?"

Matching alone is NOT SUFFICIENT for certification (ISO 26262, DO-178C).
A requirement might mention "authentication" and a control might mention
"access control" — high semantic similarity, but are they FUNCTIONALLY
EQUIVALENT? Matching cannot answer this.

VERIFICATION requires formal proofs stored in evidence_case.formal_proof.
The proof demonstrates that the control ENTAILS the requirement — not just
that they use similar words, but that implementing the control satisfies
the requirement's functional obligations.

| Level | What it proves | Sufficient for |
|-------|----------------|----------------|
| Matching | "Related concepts" | Triage, gap detection |
| Confidence | "Probably covers" | Audit prep |
| Formal proof | "Functionally entails" | Certification |

Architecture:
1. Call /create-evidence-case for the requirement text
2. Extract candidate SPARTA controls from evidence case glossary + crosswalk_chains
3. Use glossary descriptions for obligation frame extraction
4. Use crosswalk_chains for technique matching confidence
5. Use prior_qra_evidence to boost confidence
6. Route to edges/pending_review/conflicts based on confidence
7. Formal proof (evidence_case.formal_proof) added via enrich_evidence_case()
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter
from loguru import logger
from pydantic import BaseModel, Field

router = APIRouter(prefix="/match", tags=["matching"])

# scillm endpoint for obligation frame extraction
SCILLM_URL = "http://localhost:4001/v1/chat/completions"
SCILLM_KEY = "sk-dev-proxy-123"

# Obligation frameworks only (modal structure: shall/must/should)
OBLIGATION_FRAMEWORKS = {"NIST", "SPARTA", "ISO"}


# ── Request/Response Models ─────────────────────────────────────────────────


class MatchRequirementRequest(BaseModel):
    chunk_key: str = Field(..., description="datalake_chunks key")
    confidence_threshold: float = Field(0.7, ge=0.0, le=1.0)
    dry_run: bool = Field(False, description="Skip persistence")


class MatchBatchRequest(BaseModel):
    limit: int = Field(10, ge=1, le=100)
    confidence_threshold: float = Field(0.7, ge=0.0, le=1.0)
    dry_run: bool = Field(False)


# ── Obligation Frame Extraction ─────────────────────────────────────────────


OBLIGATION_PROMPT = """Extract the obligation structure from this text. Return JSON:

{
  "actor": "who must act (e.g. 'the system', 'the organization')",
  "action": "the verb (e.g. 'implement', 'verify', 'protect')",
  "object": "what the action applies to",
  "modality": "shall|must|should|may|prohibited",
  "conditions": ["when this applies"],
  "parameters": {}
}

TEXT:
{text}

Return ONLY valid JSON."""


def extract_obligation_frame(text: str, use_scillm: bool = True) -> dict:
    """Extract obligation frame from text. scillm first, heuristic fallback."""
    if use_scillm:
        try:
            resp = httpx.post(
                SCILLM_URL,
                headers={"Authorization": f"Bearer {SCILLM_KEY}"},
                json={
                    "model": "text",
                    "messages": [{"role": "user", "content": OBLIGATION_PROMPT.format(text=text[:2000])}],
                    "response_format": {"type": "json_object"},
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            frame = json.loads(resp.json()["choices"][0]["message"]["content"])
            frame["extraction_method"] = "scillm"
            return frame
        except Exception as e:
            logger.debug("scillm extraction failed: {}", e)

    # Heuristic fallback
    text_lower = text.lower()

    # Modality detection
    modality = "shall"
    for pattern, modal in [
        ("shall not", "prohibited"), ("must not", "prohibited"),
        ("shall", "shall"), ("must", "must"),
        ("should", "should"), ("may", "may"),
    ]:
        if pattern in text_lower:
            modality = modal
            break

    return {
        "actor": "the system",
        "action": "implement",
        "object": text[:300],
        "modality": modality,
        "conditions": [],
        "parameters": {},
        "extraction_method": "heuristic",
    }


# ── Confidence Scoring ──────────────────────────────────────────────────────


def compute_confidence(
    has_crosswalk: bool,
    crosswalk_method: str | None,
    has_qra_evidence: bool,
    extraction_method: str,
    req_frame: dict,
    ctrl_frame: dict,
) -> float:
    """Compute confidence from evidence case signals.

    Signals:
    - Crosswalk exists: +0.3 base (direct: +0.4, nist_nvd: +0.35, mitre_chain: +0.25)
    - QRA evidence: +0.15
    - scillm extraction: +0.1
    - Frame completeness: up to +0.15
    """
    # Base from crosswalk
    if has_crosswalk:
        if crosswalk_method == "direct":
            base = 0.65
        elif crosswalk_method == "nist_nvd":
            base = 0.55
        else:
            base = 0.45
    else:
        base = 0.3

    # QRA evidence boost
    qra_adj = 0.15 if has_qra_evidence else 0.0

    # Extraction method
    extraction_adj = 0.1 if extraction_method == "scillm" else 0.0

    # Frame completeness
    fields = ["actor", "action", "object", "modality"]
    req_complete = sum(1 for f in fields if req_frame.get(f)) / len(fields)
    ctrl_complete = sum(1 for f in fields if ctrl_frame.get(f)) / len(fields)
    completeness_adj = ((req_complete + ctrl_complete) / 2) * 0.15

    return min(1.0, base + qra_adj + extraction_adj + completeness_adj)


# ── Persistence ─────────────────────────────────────────────────────────────


def persist_edge(
    db,
    chunk_key: str,
    control_id: str,
    relationship_type: str,
    confidence: float,
    confidence_threshold: float,
    crosswalk_method: str | None,
    extraction_method: str,
) -> tuple[str, str]:
    """Persist to edges/pending_review/conflicts based on confidence."""
    edge_key = hashlib.md5(f"{chunk_key}:{control_id}".encode()).hexdigest()[:16]
    now_iso = datetime.now(timezone.utc).isoformat()

    doc = {
        "_key": edge_key,
        "chunk_key": chunk_key,
        "control_id": control_id,
        "relationship_type": relationship_type,
        "confidence": round(confidence, 3),
        "crosswalk_method": crosswalk_method,
        "extraction_method": extraction_method,
        "matched_at": now_iso,
    }

    if relationship_type == "conflicts":
        collection = "match_conflicts"
        db.collection(collection).insert(doc, overwrite=True)
        logger.warning("CONFLICT: {} vs {}", chunk_key, control_id)
    elif confidence < confidence_threshold or relationship_type in ("unknown", "partial_coverage"):
        collection = "pending_review"
        doc["review_reason"] = (
            f"low_confidence ({confidence:.2f})" if confidence < confidence_threshold
            else f"relationship_{relationship_type}"
        )
        db.collection(collection).insert(doc, overwrite=True)
    else:
        collection = "requirement_control_edges"
        doc["_from"] = f"datalake_chunks/{chunk_key.split('/')[-1]}"
        doc["_to"] = f"sparta_controls/{control_id}"
        db.collection(collection).insert(doc, overwrite=True)

    return edge_key, collection


# ── Core Matching Logic (Evidence-Case Aligned) ─────────────────────────────


def _sync_match_requirement(
    chunk_key: str,
    confidence_threshold: float,
    dry_run: bool,
) -> dict:
    """Match requirement using /create-evidence-case data."""
    from ...arango_client import get_db
    from ._entities import build_evidence_case_endpoint, BuildEvidenceCaseRequest

    db = get_db()
    result = {
        "chunk_key": chunk_key,
        "requirement_text": "",
        "requirement_valid": False,
        "evidence_case": None,
        "candidate_controls": [],
        "comparable_controls": [],
        "excluded_controls": [],
        "relationships": [],
        "matched_at": datetime.now(timezone.utc).isoformat(),
        "error": None,
    }

    try:
        # Step 1: Load requirement chunk
        key = chunk_key.split("/")[-1] if "/" in chunk_key else chunk_key
        cursor = db.aql.execute(
            "FOR doc IN datalake_chunks FILTER doc._key == @key LIMIT 1 RETURN doc",
            bind_vars={"key": key},
        )
        chunks = list(cursor)
        if not chunks:
            result["error"] = f"Chunk not found: {chunk_key}"
            return result

        chunk = chunks[0]
        req_text = chunk.get("text", chunk.get("content", ""))[:1000]
        result["requirement_text"] = req_text[:500]

        if not req_text:
            result["error"] = "Empty requirement text"
            return result

        # Step 2: Call /create-evidence-case for the requirement
        # This gives us: glossary, crosswalk_chains, prior_qra_evidence
        evidence_req = BuildEvidenceCaseRequest(question=req_text)
        evidence_case = build_evidence_case_endpoint(evidence_req)

        if evidence_case.get("error"):
            result["error"] = f"Evidence case failed: {evidence_case['error']}"
            return result

        result["evidence_case"] = {
            "glossary_count": len(evidence_case.get("glossary", [])),
            "crosswalk_count": len(evidence_case.get("crosswalk_chains", [])),
            "qra_count": len(evidence_case.get("prior_qra_evidence", [])),
        }
        result["requirement_valid"] = True

        # Step 3: Extract candidate SPARTA controls from evidence case
        # Source: glossary (resolved entities) + crosswalk_chains (linked controls)
        glossary = evidence_case.get("glossary", [])
        crosswalk_chains = evidence_case.get("crosswalk_chains", [])
        qra_evidence = evidence_case.get("prior_qra_evidence", [])

        # Build control ID → description map from glossary
        control_descriptions: dict[str, str] = {}
        for g in glossary:
            gid = g.get("id", "")
            if gid:
                control_descriptions[gid] = g.get("description", "")

        # Collect candidate controls: only NIST/SPARTA/ISO (obligation frameworks)
        candidates: list[dict] = []
        for g in glossary:
            gid = g.get("id", "")
            gfw = g.get("framework", "")
            if gfw in OBLIGATION_FRAMEWORKS or gid.startswith("SV-") or gid.startswith("AC-") or gid.startswith("SC-"):
                candidates.append({
                    "control_id": gid,
                    "framework": gfw,
                    "description": g.get("description", ""),
                    "crosswalk_method": None,
                })

        # Add controls from crosswalk chain terminals (SPARTA targets)
        for chain in crosswalk_chains:
            if chain.get("to_framework") not in OBLIGATION_FRAMEWORKS:
                continue
            # Get terminal control from hops
            hops = chain.get("hops", [])
            if hops:
                terminal = hops[-1]
                tid = terminal.get("id", "")
                if tid and tid not in {c["control_id"] for c in candidates}:
                    candidates.append({
                        "control_id": tid,
                        "framework": chain.get("to_framework", ""),
                        "description": terminal.get("description", ""),
                        "crosswalk_method": chain.get("method", chain.get("relationship", "crosswalk")),
                    })

        result["candidate_controls"] = [c["control_id"] for c in candidates]

        if not candidates:
            result["error"] = "No applicable controls found in evidence case"
            return result

        # Step 4: Extract requirement obligation frame
        req_frame = extract_obligation_frame(req_text, use_scillm=True)

        # Step 5: Match each candidate control
        for candidate in candidates:
            control_id = candidate["control_id"]
            ctrl_desc = candidate["description"]

            # If no description from glossary, fetch from sparta_controls
            if not ctrl_desc:
                ctrl_cursor = db.aql.execute(
                    "FOR doc IN sparta_controls FILTER doc.control_id == @cid LIMIT 1 RETURN doc",
                    bind_vars={"cid": control_id},
                )
                ctrl_docs = list(ctrl_cursor)
                if ctrl_docs:
                    ctrl_desc = ctrl_docs[0].get("description", "")

            if not ctrl_desc:
                result["excluded_controls"].append({
                    "control_id": control_id,
                    "reason": "no_description",
                })
                continue

            result["comparable_controls"].append(control_id)

            # Extract control obligation frame
            ctrl_frame = extract_obligation_frame(ctrl_desc, use_scillm=True)

            # Check if QRA evidence mentions this control
            has_qra = any(
                control_id in (q.get("citation_id", "") or "")
                for q in qra_evidence
            )

            # Compute confidence
            confidence = compute_confidence(
                has_crosswalk=candidate["crosswalk_method"] is not None,
                crosswalk_method=candidate["crosswalk_method"],
                has_qra_evidence=has_qra,
                extraction_method=req_frame.get("extraction_method", "heuristic"),
                req_frame=req_frame,
                ctrl_frame=ctrl_frame,
            )

            # Determine relationship type from modality comparison
            req_mod = req_frame.get("modality", "shall")
            ctrl_mod = ctrl_frame.get("modality", "shall")

            if req_mod == ctrl_mod:
                relationship_type = "equivalent"
            elif req_mod in ("shall", "must") and ctrl_mod in ("should", "may"):
                relationship_type = "refines"
            elif req_mod in ("should", "may") and ctrl_mod in ("shall", "must"):
                relationship_type = "partial_coverage"
            elif "prohibited" in (req_mod, ctrl_mod) and req_mod != ctrl_mod:
                relationship_type = "conflicts"
            else:
                relationship_type = "equivalent"

            rel_record = {
                "control_id": control_id,
                "relationship_type": relationship_type,
                "confidence": round(confidence, 3),
                "crosswalk_method": candidate["crosswalk_method"],
                "extraction_method": req_frame.get("extraction_method", "heuristic"),
            }

            # Persist
            if not dry_run:
                edge_key, collection = persist_edge(
                    db=db,
                    chunk_key=chunk_key,
                    control_id=control_id,
                    relationship_type=relationship_type,
                    confidence=confidence,
                    confidence_threshold=confidence_threshold,
                    crosswalk_method=candidate["crosswalk_method"],
                    extraction_method=req_frame.get("extraction_method", "heuristic"),
                )
                rel_record["edge_key"] = edge_key
                rel_record["routed_to"] = collection

            result["relationships"].append(rel_record)

        if not result["comparable_controls"]:
            result["error"] = "No controls with descriptions found"

    except Exception as e:
        logger.exception("Match failed for {}", chunk_key)
        result["error"] = str(e)

    return result


def _sync_review_queue(limit: int) -> dict:
    """Fetch pending reviews and conflicts."""
    from ...arango_client import get_db

    db = get_db()

    pending = []
    try:
        cursor = db.aql.execute(
            "FOR d IN pending_review FILTER d._key != '_init' SORT d.matched_at DESC LIMIT @limit RETURN d",
            bind_vars={"limit": limit},
        )
        pending = list(cursor)
    except Exception as e:
        logger.warning("pending_review query failed: {}", e)

    conflicts = []
    try:
        cursor = db.aql.execute(
            "FOR d IN match_conflicts FILTER d._key != '_init' SORT d.matched_at DESC LIMIT 20 RETURN d",
        )
        conflicts = list(cursor)
    except Exception as e:
        logger.warning("match_conflicts query failed: {}", e)

    return {
        "pending": pending,
        "conflicts": conflicts,
        "pending_count": len(pending),
        "conflict_count": len(conflicts),
    }


def _sync_coverage(framework: str | None) -> dict:
    """Get matching statistics."""
    from ...arango_client import get_db

    db = get_db()

    try:
        cursor = db.aql.execute("""
            FOR e IN requirement_control_edges
            COLLECT rel = e.relationship_type WITH COUNT INTO cnt
            SORT cnt DESC
            RETURN {relationship_type: rel, count: cnt}
        """)
        by_relationship = list(cursor)
    except Exception as e:
        logger.warning("coverage query failed: {}", e)
        by_relationship = []

    by_framework = []
    if framework:
        try:
            cursor = db.aql.execute(
                """
                FOR e IN requirement_control_edges
                FILTER CONTAINS(UPPER(e._to), @framework)
                COLLECT rel = e.relationship_type WITH COUNT INTO cnt
                RETURN {relationship_type: rel, count: cnt}
                """,
                bind_vars={"framework": framework.upper()},
            )
            by_framework = list(cursor)
        except Exception as e:
            logger.warning("framework coverage query failed: {}", e)

    try:
        total_edges = db.collection("requirement_control_edges").count()
        total_pending = db.collection("pending_review").count()
        total_conflicts = db.collection("match_conflicts").count()
    except Exception as e:
        logger.warning("count query failed: {}", e)
        total_edges = total_pending = total_conflicts = 0

    return {
        "by_relationship": by_relationship,
        "by_framework": by_framework,
        "total_edges": total_edges,
        "total_pending": total_pending,
        "total_conflicts": total_conflicts,
    }


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.post("/requirement")
async def match_requirement_endpoint(req: MatchRequirementRequest) -> dict:
    """Match a single requirement to SPARTA controls."""
    return await asyncio.to_thread(
        _sync_match_requirement,
        req.chunk_key,
        req.confidence_threshold,
        req.dry_run,
    )


@router.post("/batch")
async def match_batch_endpoint(req: MatchBatchRequest) -> dict:
    """Batch match requirements from proof_jobs queue."""
    from ...arango_client import get_db

    db = get_db()

    try:
        cursor = db.aql.execute(
            "FOR d IN proof_jobs FILTER d.status == 'pending' LIMIT @limit RETURN d",
            bind_vars={"limit": req.limit},
        )
        jobs = list(cursor)
    except Exception as e:
        return {"error": str(e), "processed": 0}

    results = []
    for job in jobs:
        chunk_key = job.get("requirement_chunk")
        if not chunk_key:
            continue

        match_result = await asyncio.to_thread(
            _sync_match_requirement,
            chunk_key,
            req.confidence_threshold,
            req.dry_run,
        )

        if not req.dry_run:
            status = "completed" if not match_result.get("error") else "failed"
            try:
                db.collection("proof_jobs").update({
                    "_key": job["_key"],
                    "status": status,
                    "result": match_result,
                    "processed_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as e:
                logger.warning("Failed to update job {}: {}", job["_key"], e)

        results.append(match_result)

    return {
        "processed": len(results),
        "succeeded": sum(1 for r in results if not r.get("error")),
        "failed": sum(1 for r in results if r.get("error")),
        "results": results,
    }


@router.get("/review-queue")
async def review_queue_endpoint(limit: int = 50) -> dict:
    """List pending reviews and conflicts."""
    return await asyncio.to_thread(_sync_review_queue, limit)


@router.get("/coverage")
async def coverage_endpoint(framework: str | None = None) -> dict:
    """Get matching statistics."""
    return await asyncio.to_thread(_sync_coverage, framework)
