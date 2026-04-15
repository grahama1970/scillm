"""Tier 2: DeepSeek Prover V2 assessment and universal proof system.

DEPRECATED (2026-01-20): DeepSeek Prover V2 (671B) is no longer available on
any serverless provider. Falls back to Tier 1 local classifier.
Use the ``lean4-prove`` skill in pi-mono instead.

Also contains universal proof helpers: has_proof, get_proof_id,
get_or_queue_proof, store_proof, link_proof_to_document.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from loguru import logger

from graph_memory.integrations.proof_assessment._tier1 import (
    get_provability_score,
    hash_claim,
)

# =============================================================================
# TIER 2: DeepSeek Prover V2 Assessment (DEPRECATED)
# =============================================================================

DEEPSEEK_PROVER_MODEL = os.getenv(
    "PROOF_ASSESSMENT_MODEL",
    "openrouter/deepseek/deepseek-prover-v2"  # DEPRECATED: No longer available
)

ASSESSMENT_PROMPT = """Analyze if this claim can be formalized and proved in Lean4.

Claim: "{claim}"

Return JSON only:
{{
  "provable": true/false,
  "confidence": 0.0-1.0,
  "reason": "brief explanation (1 sentence)",
  "lean4_sketch": "theorem signature if provable, else null",
  "suggested_tactics": ["tactic1", "tactic2"] or []
}}

Rules:
- provable=true only for mathematical/logical claims expressible in Lean4
- Heuristics, procedures, and preferences are NOT provable
- lean4_sketch should be a valid Lean4 theorem signature
- suggested_tactics should be relevant Lean4/Mathlib tactics

Return ONLY valid JSON, no markdown."""


async def assess_provability(
    claim: str,
    model: Optional[str] = None,
    skip_local_check: bool = False,
) -> Dict[str, Any]:
    """Tier 2: Assess provability using DeepSeek Prover V2.

    DEPRECATED: DeepSeek Prover V2 is no longer available on any serverless
    provider. This function will fall back to Tier 1 local classifier.
    Use lean4-prove skill in pi-mono for actual proof generation.

    Args:
        claim: The claim to assess
        model: Override the default model
        skip_local_check: If True, skip Tier 1 local check

    Returns:
        Dict with provability assessment:
        {
            "provable": bool,
            "confidence": float,
            "reason": str,
            "lean4_sketch": str | None,
            "suggested_tactics": list[str],
            "tier": 1 | 2,
            "elapsed_ms": int,
        }
    """
    t0 = time.time()

    # Tier 1: Fast local check
    if not skip_local_check:
        local_result = get_provability_score(claim)
        if not local_result["likely_provable"]:
            return {
                "provable": False,
                "confidence": 0.0,
                "reason": "No mathematical patterns detected (local classifier)",
                "lean4_sketch": None,
                "suggested_tactics": [],
                "tier": 1,
                "local_score": local_result,
                "elapsed_ms": int((time.time() - t0) * 1000),
            }

    # Tier 2: DeepSeek Prover V2 (via scillm HTTP API)
    import httpx

    api_base = os.getenv("SCILLM_API_BASE", "http://localhost:4001")
    api_key = os.getenv("SCILLM_PROXY_KEY", "sk-dev-proxy-123")

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{api_base}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "x-expect-json": "true",
                },
                json={
                    "model": model or DEEPSEEK_PROVER_MODEL,
                    "messages": [{"role": "user", "content": ASSESSMENT_PROMPT.format(claim=claim)}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.1,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "{}")
            result = json.loads(content)

        elapsed_ms = int((time.time() - t0) * 1000)

        return {
            "provable": result.get("provable", False),
            "confidence": result.get("confidence", 0.0),
            "reason": result.get("reason", ""),
            "lean4_sketch": result.get("lean4_sketch"),
            "suggested_tactics": result.get("suggested_tactics", []),
            "tier": 2,
            "elapsed_ms": elapsed_ms,
        }

    except Exception as exc:
        logger.error("DeepSeek assessment failed: {}", exc)
        elapsed_ms = int((time.time() - t0) * 1000)
        return {
            "provable": False,
            "confidence": 0.0,
            "reason": f"Assessment failed: {exc!s}",
            "lean4_sketch": None,
            "suggested_tactics": [],
            "tier": 2,
            "error": str(exc),
            "elapsed_ms": elapsed_ms,
        }


# =============================================================================
# Universal Proof System
# =============================================================================


def has_proof(claim: str, db=None) -> Optional[Dict[str, Any]]:
    """Check if a proof exists for this claim.

    Args:
        claim: The claim text to check
        db: Optional ArangoDB connection (will get_db() if not provided)

    Returns:
        The proof document if found, else None
    """
    if db is None:
        from graph_memory.arango_client import get_db
        from graph_memory.setup_schema import ensure_collections_and_view
        ensure_collections_and_view()
        db = get_db()

    claim_hash = hash_claim(claim)

    try:
        cursor = db.aql.execute(
            """
            FOR p IN proofs
                FILTER p.claim_hash == @hash
                LIMIT 1
                RETURN p
            """,
            bind_vars={"hash": claim_hash}
        )
        proofs = list(cursor)
        return proofs[0] if proofs else None
    except Exception as exc:
        logger.warning("Error checking for proof: {}", exc)
        return None


def get_proof_id(claim: str, db=None) -> Optional[str]:
    """Get proof_id for a claim if it exists.

    Args:
        claim: The claim text to check
        db: Optional ArangoDB connection

    Returns:
        "proofs/<key>" if proof exists, else None
    """
    proof = has_proof(claim, db)
    return f"proofs/{proof['_key']}" if proof else None


async def get_or_queue_proof(
    claim: str,
    source_id: Optional[str] = None,
    source_type: Optional[str] = None,
    skip_assessment: bool = False,
) -> Dict[str, Any]:
    """Universal entry point: get existing proof or queue for proving.

    This is the main function to call when you have a claim that might be provable.
    It handles the full flow:
    1. Check if proof already exists -> return it
    2. Run assessment (Tier 1 + Tier 2)
    3. If provable, queue for background proof
    4. Return status

    Args:
        claim: The claim text
        source_id: Optional reference to source document (e.g., "episodes/xyz")
        source_type: Optional type hint ("episode", "lesson", etc.)
        skip_assessment: If True, skip Tier 2 and just check Tier 1

    Returns:
        {
            "proof_id": "proofs/xyz" or None,
            "proof_status": "proved" | "pending" | "not_provable" | "queued",
            "assessment": {...} or None,
            "job_id": "proof_jobs/xyz" or None,
        }
    """
    from graph_memory.arango_client import get_db
    from graph_memory.setup_schema import ensure_collections_and_view
    ensure_collections_and_view()
    db = get_db()

    # Step 1: Check if proof already exists
    existing_proof = has_proof(claim, db)
    if existing_proof:
        return {
            "proof_id": f"proofs/{existing_proof['_key']}",
            "proof_status": "proved",
            "assessment": None,
            "job_id": None,
            "lean4_code": existing_proof.get("lean4_code"),
        }

    # Step 2: Run assessment
    if skip_assessment:
        # Tier 1 only
        local_result = get_provability_score(claim)
        if not local_result["likely_provable"]:
            return {
                "proof_id": None,
                "proof_status": "not_provable",
                "assessment": {"tier": 1, **local_result},
                "job_id": None,
            }
        assessment = {"tier": 1, "provable": True, **local_result}
    else:
        # Full Tier 1 + Tier 2 assessment
        assessment = await assess_provability(claim)
        if not assessment.get("provable"):
            return {
                "proof_id": None,
                "proof_status": "not_provable",
                "assessment": assessment,
                "job_id": None,
            }

    # Step 3: Check if already queued
    claim_hash = hash_claim(claim)
    try:
        cursor = db.aql.execute(
            """
            FOR j IN proof_jobs
                FILTER j.claim_hash == @hash AND j.status IN ["pending", "proving"]
                LIMIT 1
                RETURN j
            """,
            bind_vars={"hash": claim_hash}
        )
        existing_jobs = list(cursor)
        if existing_jobs:
            job = existing_jobs[0]
            return {
                "proof_id": None,
                "proof_status": "pending",
                "assessment": assessment,
                "job_id": f"proof_jobs/{job['_key']}",
            }
    except Exception as exc:
        logger.error("Suppressed error in proof_assessment: {}", exc)

    # Step 4: Queue for proof
    ts = int(time.time())
    job = {
        "_key": uuid.uuid4().hex[:16],
        "claim": claim,
        "claim_hash": claim_hash,
        "source_id": source_id,
        "source_type": source_type,
        "lean4_sketch": assessment.get("lean4_sketch"),
        "suggested_tactics": assessment.get("suggested_tactics", []),
        "status": "pending",
        "attempts": 0,
        "max_attempts": 3,
        "created_at": ts,
        "updated_at": ts,
        "error": None,
        "proof_id": None,
    }

    try:
        db.collection("proof_jobs").insert(job)
        logger.info("Queued proof job: {} for claim: {}...", job['_key'], claim[:50])
    except Exception as exc:
        logger.error("Failed to queue proof job: {}", exc)
        return {
            "proof_id": None,
            "proof_status": "error",
            "assessment": assessment,
            "job_id": None,
            "error": str(exc),
        }

    return {
        "proof_id": None,
        "proof_status": "queued",
        "assessment": assessment,
        "job_id": f"proof_jobs/{job['_key']}",
    }


def store_proof(
    claim: str,
    lean4_code: str,
    tactics_used: Optional[List[str]] = None,
    compile_time_ms: Optional[int] = None,
    db=None,
) -> Dict[str, Any]:
    """Store a completed proof in the proofs collection.

    Args:
        claim: The original claim text
        lean4_code: The Lean4 proof code
        tactics_used: List of tactics used
        compile_time_ms: Compilation time
        db: Optional ArangoDB connection

    Returns:
        The created proof document
    """
    if db is None:
        from graph_memory.arango_client import get_db
        from graph_memory.setup_schema import ensure_collections_and_view
        ensure_collections_and_view()
        db = get_db()

    claim_hash = hash_claim(claim)

    # Check if proof already exists (dedup)
    existing = has_proof(claim, db)
    if existing:
        logger.info("Proof already exists: {}", existing['_key'])
        return existing

    ts = int(time.time())
    proof = {
        "_key": uuid.uuid4().hex[:16],
        "claim": claim,
        "claim_hash": claim_hash,
        "lean4_code": lean4_code,
        "tactics_used": tactics_used or [],
        "compile_time_ms": compile_time_ms or 0,
        "created_at": ts,
    }

    db.collection("proofs").insert(proof)
    logger.info("Stored proof: {} for claim: {}...", proof['_key'], claim[:50])

    return proof


def link_proof_to_document(
    collection: str,
    doc_key: str,
    proof_id: str,
    db=None,
) -> bool:
    """Link a proof to any document by setting its proof_id field.

    Args:
        collection: Collection name ("episodes", "lessons", etc.)
        doc_key: Document key
        proof_id: Proof ID (e.g., "proofs/xyz")
        db: Optional ArangoDB connection

    Returns:
        True if successful, False otherwise
    """
    if db is None:
        from graph_memory.arango_client import get_db
        db = get_db()

    try:
        db.collection(collection).update({
            "_key": doc_key,
            "proof_id": proof_id,
        })
        logger.info("Linked proof {} to {}/{}", proof_id, collection, doc_key)
        return True
    except Exception as exc:
        logger.warning("Failed to link proof: {}", exc)
        return False


# =============================================================================
# CLI Interface
# =============================================================================

async def main():
    """CLI for proof assessment."""
    import argparse

    parser = argparse.ArgumentParser(description="Proof assessment for memory lessons")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # assess command
    assess_parser = subparsers.add_parser("assess", help="Assess if claim is provable")
    assess_parser.add_argument("claim", help="The claim to assess")
    assess_parser.add_argument("--local-only", action="store_true", help="Use local classifier only")
    assess_parser.add_argument("--model", help="Override assessment model")

    # score command
    score_parser = subparsers.add_parser("score", help="Get detailed provability score")
    score_parser.add_argument("claim", help="The claim to score")

    args = parser.parse_args()

    if args.command == "assess":
        if args.local_only:
            result = get_provability_score(args.claim)
            result["tier"] = 1
        else:
            result = await assess_provability(args.claim, model=args.model)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "score":
        result = get_provability_score(args.claim)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
