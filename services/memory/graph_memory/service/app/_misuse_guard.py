"""Misuse detection for Memory API — helpful errors that teach correct usage.

Project agents in 2026 do not thoroughly read SKILL.md files. This module
implements server-side misuse detection with helpful error messages.

ALL MISUSES ARE LOGGED to the `misuse_events` collection for nightly
analysis by /monitor-misuse. This enables cross-skill pattern detection
and self-improving misuse guards.

Detected misuses:
1. Wrong collection names (plural vs singular, typos)
2. Missing _key on /store or /upsert
3. /store to lessons without tags
4. Using deprecated /learn endpoint
5. Using old 'lessons' collection instead of 'lessons_v2'
6. Empty query on /recall
7. Batch overload on /upsert
8. AQL patterns in /recall query
9. Unbounded /list that would return >25K docs (causes timeout)

See /best-practices-skills for the pattern.
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from fastapi import HTTPException
from loguru import logger


# ============================================================================
# Misuse Event Logging (to misuse_events collection for nightly analysis)
# ============================================================================

def _log_misuse_event(
    endpoint: str,
    error_type: str,
    sent_value: str,
    correct_value: str | None = None,
) -> None:
    """Log misuse to misuse_events collection for /monitor-misuse analysis.

    Non-blocking: failures are logged but don't affect the error response.
    Uses direct ArangoDB write since we're already in the memory service.
    """
    try:
        from ...arango_client import get_db

        key_source = f"memory:{endpoint}:{error_type}:{sent_value}"
        doc_key = hashlib.sha256(key_source.encode()).hexdigest()[:16]

        db = get_db()
        if not db.has_collection("misuse_events"):
            db.create_collection("misuse_events")

        coll = db.collection("misuse_events")
        doc = {
            "_key": doc_key,
            "skill": "memory",
            "endpoint": endpoint,
            "error_type": error_type,
            "sent_value": sent_value,
            "correct_value": correct_value,
            "was_known": correct_value is not None,
            "ts": int(time.time()),
        }

        if coll.has(doc_key):
            # Increment count on duplicate
            existing = coll.get(doc_key)
            doc["count"] = existing.get("count", 1) + 1
            doc["last_ts"] = doc["ts"]
            doc["ts"] = existing.get("ts", doc["ts"])  # Keep original timestamp
            coll.update(doc)
        else:
            doc["count"] = 1
            coll.insert(doc)

    except Exception as exc:
        # Never fail the request due to logging failure
        logger.debug(f"Misuse event logging failed (non-fatal): {exc}")

# ============================================================================
# Collection Name Corrections (only misuse patterns, not exhaustive list)
# ============================================================================

# Common typos/misuses → correct name
# These are the specific mistakes agents make, not a list of all collections
COLLECTION_CORRECTIONS = {
    # Plural → singular (agents guess plural)
    "sparta_qras": "sparta_qra",
    "lessons_v2s": "lessons_v2",
    "persona": "personas",
    "checkpoint": "checkpoints",
    "user": "users",
    "belief": "beliefs",
    "script": "scripts",
    "relationship": "relationships",
    # Singular → plural (these ARE plural)
    "sparta_control": "sparta_controls",
    "sparta_relationship": "sparta_relationships",
    # Typos
    "sparat_qra": "sparta_qra",
    "sparta_qar": "sparta_qra",
    "spartaqra": "sparta_qra",
    "lesons": "lessons_v2",
    "lessions": "lessons_v2",
}

# Collections that should warn (not error) when used
DEPRECATED_COLLECTIONS = {
    "lessons": (
        "Collection 'lessons' is deprecated (372K junk drawer). "
        "Use 'lessons_v2' (161K clean). "
        "See MEMORY.md: project_lessons_v2_separation.md"
    ),
}


def validate_collection_name(collection: str | None, endpoint: str) -> str | None:
    """Validate collection name, return corrected name or raise helpful error."""
    if not collection:
        return None

    collection = collection.strip()

    # Check for known corrections
    if collection in COLLECTION_CORRECTIONS:
        correct = COLLECTION_CORRECTIONS[collection]
        _log_misuse_event(
            endpoint=endpoint,
            error_type="wrong_collection",
            sent_value=collection,
            correct_value=correct,
        )
        raise HTTPException(
            400,
            f"COLLECTION MISUSE: '{collection}' is wrong. Use '{correct}'. "
            f"Example: POST {endpoint} with collection='{correct}'. "
            f"See /memory SKILL.md for collection names."
        )

    # Check for deprecated collections (warn but allow)
    if collection in DEPRECATED_COLLECTIONS:
        _log_misuse_event(
            endpoint=endpoint,
            error_type="deprecated_collection",
            sent_value=collection,
            correct_value="lessons_v2" if collection == "lessons" else None,
        )
        logger.warning(f"Deprecated collection '{collection}': {DEPRECATED_COLLECTIONS[collection]}")

    return collection


# ============================================================================
# Store/Upsert Validation
# ============================================================================

def validate_store_request(body: dict[str, Any]) -> None:
    """Validate /store request body, raise helpful errors for misuse."""
    document = body.get("document")
    collection = body.get("collection")

    if not document:
        _log_misuse_event(
            endpoint="/store",
            error_type="missing_document",
            sent_value="(none)",
        )
        raise HTTPException(
            400,
            "MISSING DOCUMENT: /store requires a 'document' field. "
            "Example: POST /store with {\"document\": {\"_key\": \"abc\", ...}, \"collection\": \"sparta_qra\"}"
        )

    # Check for missing _key when writing to non-lessons collections
    if collection and collection not in ("lessons", "lessons_v2"):
        if "_key" not in document:
            _log_misuse_event(
                endpoint="/store",
                error_type="missing_key",
                sent_value=f"collection={collection}",
            )
            raise HTTPException(
                400,
                f"MISSING _key: /store to '{collection}' requires document._key. "
                f"Generate a deterministic key: hashlib.sha256(canonical_fields.encode()).hexdigest()[:16]. "
                f"Example: {{\"document\": {{\"_key\": \"abc123...\", ...}}, \"collection\": \"{collection}\"}}"
            )

    # Check for lessons without tags (warning, not error)
    if collection in ("lessons", "lessons_v2", None):
        tags = document.get("tags", [])
        if not tags:
            _log_misuse_event(
                endpoint="/store",
                error_type="lessons_without_tags",
                sent_value=f"problem={document.get('problem', '?')[:50]}",
            )
            logger.warning(
                "STORE TO LESSONS WITHOUT TAGS: Document has no 'tags' field. "
                "/recall uses tags for multi-hop traversal. Add taxonomy tags: "
                "['Fragility', 'extraction'] or domain tags: ['skill_registry', 'checkpoint']"
            )


def validate_upsert_request(body: dict[str, Any]) -> None:
    """Validate /upsert request body, raise helpful errors for misuse."""
    documents = body.get("documents", [])
    collection = body.get("collection")

    if not documents:
        _log_misuse_event(
            endpoint="/upsert",
            error_type="missing_documents",
            sent_value="(empty list)",
        )
        raise HTTPException(
            400,
            "MISSING DOCUMENTS: /upsert requires a 'documents' list. "
            "Example: POST /upsert with {\"documents\": [{\"_key\": \"a\", ...}, {...}], \"collection\": \"sparta_qra\"}"
        )

    if not collection:
        _log_misuse_event(
            endpoint="/upsert",
            error_type="missing_collection",
            sent_value=f"doc_count={len(documents)}",
        )
        raise HTTPException(
            400,
            "MISSING COLLECTION: /upsert requires a 'collection' field. "
            "Example: POST /upsert with {\"documents\": [...], \"collection\": \"sparta_qra\"}"
        )

    # Check for batch overload
    if len(documents) > 1000:
        _log_misuse_event(
            endpoint="/upsert",
            error_type="batch_overload",
            sent_value=f"count={len(documents)}",
        )
        raise HTTPException(
            413,
            f"BATCH TOO LARGE: {len(documents)} documents exceeds 1000 limit. "
            f"Split into chunks: for chunk in chunks(docs, 500): POST /upsert {{documents: chunk}}"
        )

    # Check for missing _key
    missing_key_count = sum(1 for d in documents if "_key" not in d)
    if missing_key_count > 0:
        _log_misuse_event(
            endpoint="/upsert",
            error_type="missing_key",
            sent_value=f"missing={missing_key_count}/{len(documents)}",
        )
        raise HTTPException(
            400,
            f"MISSING _key: {missing_key_count}/{len(documents)} documents lack _key field. "
            f"Every document needs _key for upsert. Generate deterministic keys: "
            f"hashlib.sha256(canonical_fields.encode()).hexdigest()[:16]"
        )


# ============================================================================
# Recall Validation
# ============================================================================

def validate_recall_request(body: dict[str, Any]) -> None:
    """Validate /recall request body, raise helpful errors for misuse."""
    q = body.get("q", "")

    if not q or not q.strip():
        _log_misuse_event(
            endpoint="/recall",
            error_type="empty_query",
            sent_value="(empty)",
        )
        raise HTTPException(
            400,
            "EMPTY QUERY: /recall requires a non-empty 'q' field. "
            "Example: POST /recall with {\"q\": \"jamming satellite\", \"k\": 5}"
        )

    # Warn on very short queries
    if len(q.strip()) < 3:
        _log_misuse_event(
            endpoint="/recall",
            error_type="short_query",
            sent_value=q.strip(),
        )
        logger.warning(f"Very short recall query: '{q}' — may return poor results")

    # Check for reimplementation patterns in query (heuristic)
    reimpl_patterns = ["FOR doc IN", "FILTER", "RETURN", "LET "]
    if any(p in q for p in reimpl_patterns):
        _log_misuse_event(
            endpoint="/recall",
            error_type="aql_in_query",
            sent_value=q[:100],
        )
        logger.warning(
            f"POSSIBLE AQL IN QUERY: '{q[:50]}...' looks like AQL. "
            f"/recall takes natural language, not AQL. The daemon already does BM25+cosine+graph. "
            f"Just pass the search text, read confidence from the response."
        )


# ============================================================================
# Response Helpers
# ============================================================================

def add_response_hints(response: dict[str, Any], endpoint: str) -> dict[str, Any]:
    """Add helpful hints to response for common mistakes."""

    # Remind about correct key name
    if "items" in response and endpoint == "/recall":
        response["_hint"] = "Use data['items'] (NOT 'results') to access matches. Read data['confidence'] for grounding score."

    return response


# ============================================================================
# Deprecated Endpoint Detection
# ============================================================================

DEPRECATED_ENDPOINTS = {
    "/learn": (
        "DEPRECATED: /learn always writes to lessons collection. "
        "Use /store with explicit collection: POST /store {\"document\": {...}, \"collection\": \"sparta_qra\"}"
    ),
}


def check_deprecated_endpoint(path: str) -> None:
    """Raise helpful error for deprecated endpoints."""
    if path in DEPRECATED_ENDPOINTS:
        logger.warning(f"Deprecated endpoint called: {path}")
        # Don't block, just warn — /learn still works for lessons


# ============================================================================
# List Validation (prevents timeout on large result sets)
# ============================================================================

# Max docs /list can return in a single call (prevents timeout/OOM)
MAX_LIST_RESULTS = 25000


def validate_list_result_count(collection: str, total_matching: int, requested_limit: int) -> None:
    """Log warning when filter matches very large result set.

    Called AFTER count query, BEFORE fetching results. This logs a misuse
    event when callers are iterating through huge result sets, which may
    cause memory pressure when all docs are loaded client-side.

    Does NOT block - just logs for /monitor-misuse analysis. Pagination
    is forced by Pydantic schema (limit <= 500), but callers can still
    make 450 calls to load 225K docs into memory.
    """
    if total_matching > MAX_LIST_RESULTS:
        _log_misuse_event(
            endpoint="/list",
            error_type="large_result_set",
            sent_value=f"collection={collection} total={total_matching}",
            correct_value="Process in streaming batches, not load-all-then-process",
        )
        logger.warning(
            f"LARGE RESULT SET: /list filter matches {total_matching:,} docs in '{collection}'. "
            f"If loading all into memory, consider streaming batch processing instead. "
            f"Pattern: for each page, process immediately instead of collecting all first."
        )
