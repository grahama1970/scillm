"""ArangoDB infrastructure audit endpoint.

Runs 8 checks against the memory database and returns a structured report:
  1. Collection existence — verifies all required collections are present
  2. ArangoSearch view health — views exist and have linked collections
  3. Index coverage — embedding collections have vector indexes
  4. Embedding completeness — coverage percentage per collection
  5. Analyzer verification — text_en stemming, tokenization, case folding
  6. BM25 functional test — each view returns ranked results
  7. View sync check — view doc counts match backing collections
  8. Duplicate detection — hash-based dedup field uniqueness
  9. Schema validation — required fields present on every document

Inputs:
    None (GET request, no parameters).

Outputs:
    JSON with ``checks`` list (one dict per check) and ``summary``
    with ``passed`` / ``failed`` counts.

Failure modes:
    - ArangoDB unreachable → 503 with detail message.
    - Individual check failure → that check returns status="error" with
      detail; remaining checks still execute.
    - Performance budget: entire audit <5 s.
"""
from __future__ import annotations

# DEPRECATION NOTICE (2026-03-14): /db/* endpoints should migrate to /analytics/run.
# 4 callers remain. See feedback_endpoint_proliferation.md.

from typing import Any

from arango.exceptions import (
    ArangoServerError,
    CollectionListError,
    AQLQueryExecuteError,
)
from fastapi import APIRouter, HTTPException
from loguru import logger

from ...arango_client import get_db

router = APIRouter(prefix="/db", tags=["db-audit"])

# ── Constants ──────────────────────────────────────────────────────────────

REQUIRED_COLLECTIONS = [
    "lessons",
    "sparta_qra",
    "sparta_controls",
    "sparta_url_knowledge",
    "users",
    "persona_states",
    "user_agent_relationships",
    "domain_terms",
    "taxonomy_vocabulary",
]

REQUIRED_VIEWS = [
    "lessons_search",
    "sparta_qra_search",
    "sparta_controls_search",
    "sparta_unified_search",
]

EMBEDDING_COLLECTIONS = [
    "lessons",
    "sparta_controls",
    "sparta_qra",
    "technique_knowledge",
    "domain_terms",
]

VIEW_TO_COLLECTION = {
    "lessons_search": "lessons",
    "sparta_qra_search": "sparta_qra",
    "sparta_controls_search": "sparta_controls",
}

DEDUP_FIELDS = {
    "lessons": "problem_hash",
    "sparta_qra": "source_hash",
}

REQUIRED_FIELDS_MAP = {
    "lessons": ["problem", "solution"],
    "sparta_qra": ["question", "answer"],
    "sparta_controls": ["control_id", "name"],
    "sparta_url_knowledge": ["topic", "text", "url_id"],
}


# ── Individual checks ─────────────────────────────────────────────────────


def _check_collections(db: Any) -> dict[str, Any]:
    """Check 1: verify required collections exist and report doc counts."""
    try:
        existing = {
            c["name"]
            for c in db.collections()
            if not c["name"].startswith("_")
        }
    except (CollectionListError, ArangoServerError) as exc:
        return {"status": "error", "detail": str(exc)}

    results: dict[str, Any] = {}
    for name in REQUIRED_COLLECTIONS:
        exists = name in existing
        info: dict[str, Any] = {"exists": exists}
        if exists:
            try:
                info["count"] = db.collection(name).count()
            except (ArangoServerError, Exception) as exc:
                info["count"] = -1
                logger.warning("Count failed for {}: {}", name, exc)
        results[name] = info

    all_present = all(r["exists"] for r in results.values())
    return {"status": "pass" if all_present else "fail", "collections": results}


def _check_views(db: Any) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Check 2: verify ArangoSearch views exist and have links.

    Returns (check_result, views_status_map) — the map is reused by later checks.
    """
    try:
        existing = {v["name"] for v in db.views()}
    except (ArangoServerError, Exception) as exc:
        logger.error("Failed to list views: {}", exc)
        return {"status": "error", "detail": str(exc)}, {}

    views_status: dict[str, dict[str, Any]] = {}
    for name in REQUIRED_VIEWS:
        exists = name in existing
        info: dict[str, Any] = {"exists": exists}
        if exists:
            try:
                props = db.view(name)
                links = props.get("links", {})
                info["linked_collections"] = list(links.keys())
                info["has_links"] = len(links) > 0
            except (ArangoServerError, Exception):
                info["has_links"] = False
        views_status[name] = info

    all_ok = all(v.get("exists") for v in views_status.values())
    return {"status": "pass" if all_ok else "fail", "views": views_status}, views_status


def _check_indexes(db: Any, existing_collections: set[str]) -> dict[str, Any]:
    """Check 3: verify embedding collections have vector indexes."""
    results: dict[str, Any] = {}
    for name in EMBEDDING_COLLECTIONS:
        if name not in existing_collections:
            results[name] = {"exists": False, "reason": "collection_missing"}
            continue
        try:
            indexes = db.collection(name).indexes()
            found = False
            idx_type = None
            for idx in indexes:
                fields = idx.get("fields", [])
                if any("embedding" in str(f) for f in fields):
                    found = True
                    idx_type = idx.get("type", "")
                    break
            results[name] = {"exists": found, "type": idx_type}
        except (ArangoServerError, Exception) as exc:
            results[name] = {"exists": False, "error": str(exc)}

    all_ok = all(r.get("exists") for r in results.values())
    return {"status": "pass" if all_ok else "fail", "vector_indexes": results}


def _check_embedding_coverage(db: Any, existing_collections: set[str]) -> dict[str, Any]:
    """Check 4: embedding completeness per collection."""
    results: dict[str, Any] = {}
    for name in EMBEDDING_COLLECTIONS:
        if name not in existing_collections:
            results[name] = {"total": 0, "embedded": 0, "coverage_pct": 0.0}
            continue
        try:
            total = db.aql.execute(f"RETURN LENGTH({name})").next()
            embedded_aql = f"""
            FOR doc IN {name}
                FILTER doc.embedding != null
                    AND IS_LIST(doc.embedding)
                    AND LENGTH(doc.embedding) > 0
                COLLECT WITH COUNT INTO c
                RETURN c
            """
            embedded = db.aql.execute(embedded_aql).next()
            results[name] = {
                "total": total,
                "embedded": embedded,
                "missing": total - embedded,
                "coverage_pct": round(100.0 * embedded / total, 2) if total > 0 else 0.0,
            }
        except (AQLQueryExecuteError, ArangoServerError) as exc:
            logger.error("Embedding coverage query failed for {}: {}", name, exc)
            results[name] = {"error": str(exc)}

    return {"status": "pass", "embedding_coverage": results}


def _check_analyzer(db: Any) -> dict[str, Any]:
    """Check 5: verify text_en analyzer (stemming, tokenization, case folding)."""
    checks: dict[str, Any] = {}
    try:
        # Stemming: "running" should produce token "run"
        stemming_tokens = db.aql.execute(
            "RETURN TOKENS(@text, 'text_en')",
            bind_vars={"text": "running"},
        ).next()
        checks["stemming"] = {
            "input": "running",
            "tokens": stemming_tokens,
            "pass": "run" in stemming_tokens,
        }

        # Tokenization: text_en does NOT remove stop words
        tok_tokens = db.aql.execute(
            "RETURN TOKENS(@text, 'text_en')",
            bind_vars={"text": "the quick brown fox"},
        ).next()
        checks["tokenization"] = {
            "input": "the quick brown fox",
            "tokens": tok_tokens,
            "pass": len(tok_tokens) > 0,
        }

        # Case folding: "HELLO" → "hello"
        case_tokens = db.aql.execute(
            "RETURN TOKENS(@text, 'text_en')",
            bind_vars={"text": "HELLO"},
        ).next()
        checks["case_folding"] = {
            "input": "HELLO",
            "tokens": case_tokens,
            "pass": "hello" in case_tokens,
        }
    except (AQLQueryExecuteError, ArangoServerError) as exc:
        logger.error("Analyzer check failed: {}", exc)
        return {"status": "error", "detail": str(exc)}

    all_ok = all(c.get("pass") for c in checks.values())
    return {"status": "pass" if all_ok else "fail", "analyzer": checks}


def _check_bm25(db: Any, views_status: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Check 6: BM25 functional test — each view returns ranked results."""
    results: dict[str, Any] = {}
    for view_name in REQUIRED_VIEWS:
        if not views_status.get(view_name, {}).get("exists"):
            results[view_name] = {"pass": False, "reason": "view_missing"}
            continue
        try:
            bm25_aql = f"""
            FOR doc IN {view_name}
                SEARCH ANALYZER(
                    doc.problem IN TOKENS(@q, 'text_en')
                    OR doc.question IN TOKENS(@q, 'text_en')
                    OR doc.name IN TOKENS(@q, 'text_en')
                    OR doc.text IN TOKENS(@q, 'text_en')
                    OR doc.topic IN TOKENS(@q, 'text_en'),
                    'text_en'
                )
                SORT BM25(doc) DESC
                LIMIT 5
                RETURN {{ score: BM25(doc) }}
            """
            cursor = db.aql.execute(bm25_aql, bind_vars={"q": "access control"})
            rows = list(cursor)
            scores = [r["score"] for r in rows]
            has_results = len(scores) > 0
            positive = all(s > 0 for s in scores) if scores else False
            descending = (
                all(scores[i] >= scores[i + 1] for i in range(len(scores) - 1))
                if len(scores) > 1
                else True
            )
            results[view_name] = {
                "pass": has_results and positive and descending,
                "result_count": len(scores),
                "scores_positive": positive,
                "scores_descending": descending,
                "top_score": scores[0] if scores else 0,
            }
        except (AQLQueryExecuteError, ArangoServerError) as exc:
            logger.error("BM25 functional test failed for {}: {}", view_name, exc)
            results[view_name] = {"pass": False, "error": str(exc)}

    all_ok = all(r.get("pass") for r in results.values())
    return {"status": "pass" if all_ok else "fail", "bm25": results}


def _check_view_sync(
    db: Any,
    views_status: dict[str, dict[str, Any]],
    existing_collections: set[str],
) -> dict[str, Any]:
    """Check 7: view doc counts match backing collection counts."""
    results: dict[str, Any] = {}

    # Single-collection views
    for view_name, col_name in VIEW_TO_COLLECTION.items():
        if not views_status.get(view_name, {}).get("exists"):
            results[view_name] = {"pass": False, "reason": "view_missing"}
            continue
        if col_name not in existing_collections:
            results[view_name] = {"pass": False, "reason": "collection_missing"}
            continue
        try:
            sync_aql = f"""
            LET col_count = LENGTH({col_name})
            LET view_count = LENGTH(FOR d IN {view_name} SEARCH d._key != null RETURN 1)
            RETURN {{
                collection_count: col_count,
                view_count: view_count,
                synced: col_count == view_count,
                delta: ABS(col_count - view_count)
            }}
            """
            row = db.aql.execute(sync_aql).next()
            results[view_name] = {
                "pass": row["synced"],
                "collection_count": row["collection_count"],
                "view_count": row["view_count"],
                "delta": row["delta"],
            }
        except (AQLQueryExecuteError, ArangoServerError) as exc:
            logger.error("View sync check failed for {}: {}", view_name, exc)
            results[view_name] = {"pass": False, "error": str(exc)}

    # Multi-collection unified view
    unified = "sparta_unified_search"
    if views_status.get(unified, {}).get("exists"):
        linked = views_status[unified].get("linked_collections", [])
        present = [c for c in linked if c in existing_collections]
        try:
            sum_expr = " + ".join(f"LENGTH({c})" for c in present) or "0"
            sync_aql = f"""
            LET col_sum = {sum_expr}
            LET view_count = LENGTH(FOR d IN {unified} SEARCH d._key != null RETURN 1)
            RETURN {{
                collection_sum: col_sum,
                view_count: view_count,
                synced: col_sum == view_count,
                delta: ABS(col_sum - view_count)
            }}
            """
            row = db.aql.execute(sync_aql).next()
            results[unified] = {
                "pass": row["synced"],
                "linked_collections": present,
                "collection_sum": row["collection_sum"],
                "view_count": row["view_count"],
                "delta": row["delta"],
            }
        except (AQLQueryExecuteError, ArangoServerError) as exc:
            logger.error("View sync check failed for {}: {}", unified, exc)
            results[unified] = {"pass": False, "error": str(exc)}
    elif unified in set(REQUIRED_VIEWS):
        results[unified] = {"pass": False, "reason": "view_missing"}

    all_ok = all(r.get("pass") for r in results.values())
    return {"status": "pass" if all_ok else "fail", "view_sync": results}


def _check_duplicates(db: Any, existing_collections: set[str]) -> dict[str, Any]:
    """Check 8: hash-based duplicate detection."""
    results: dict[str, Any] = {}
    for col_name, dedup_field in DEDUP_FIELDS.items():
        if col_name not in existing_collections:
            results[col_name] = {"pass": False, "reason": "collection_missing"}
            continue
        try:
            dup_aql = f"""
            FOR doc IN {col_name}
                FILTER doc.@field != null
                COLLECT hash = doc.@field WITH COUNT INTO cnt
                FILTER cnt > 1
                SORT cnt DESC
                LIMIT 10
                RETURN {{ hash: hash, count: cnt }}
            """
            cursor = db.aql.execute(dup_aql, bind_vars={"field": dedup_field})
            duplicates = list(cursor)
            total_dups = sum(d["count"] - 1 for d in duplicates)
            results[col_name] = {
                "pass": len(duplicates) == 0,
                "duplicate_groups": len(duplicates),
                "total_duplicate_docs": total_dups,
                "worst": duplicates[:3] if duplicates else [],
                "dedup_field": dedup_field,
            }
        except (AQLQueryExecuteError, ArangoServerError) as exc:
            logger.error("Duplicate check failed for {}: {}", col_name, exc)
            results[col_name] = {"pass": False, "error": str(exc)}

    all_ok = all(r.get("pass") for r in results.values())
    return {"status": "pass" if all_ok else "fail", "duplicates": results}


def _check_schema(db: Any, existing_collections: set[str], collections_status: dict) -> dict[str, Any]:
    """Check 9: required fields present on every document."""
    results: dict[str, Any] = {}
    for col_name, fields in REQUIRED_FIELDS_MAP.items():
        if col_name not in existing_collections:
            results[col_name] = {"pass": False, "reason": "collection_missing"}
            continue
        try:
            conditions = " OR ".join(f"doc.{f} == null" for f in fields)
            schema_aql = f"""
            FOR doc IN {col_name}
                FILTER {conditions}
                COLLECT WITH COUNT INTO c
                RETURN c
            """
            missing_count = db.aql.execute(schema_aql).next()
            total = collections_status.get(col_name, {}).get("count", 0)
            results[col_name] = {
                "pass": missing_count == 0,
                "missing_required_fields": missing_count,
                "total": total,
                "checked_fields": fields,
            }
        except (AQLQueryExecuteError, ArangoServerError) as exc:
            logger.error("Schema check failed for {}: {}", col_name, exc)
            results[col_name] = {"pass": False, "error": str(exc)}

    all_ok = all(r.get("pass") for r in results.values())
    return {"status": "pass" if all_ok else "fail", "schema": results}


# ── Route ──────────────────────────────────────────────────────────────────


@router.get("/audit")
def run_audit() -> dict[str, Any]:
    """8-check ArangoDB infrastructure audit.

    Each check runs independently — a failure in one does not prevent the
    others from executing. The summary counts passed vs failed checks.
    """
    try:
        db = get_db()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"ArangoDB unavailable: {exc}") from exc

    # Build the set of existing collections once (reused by multiple checks)
    try:
        existing_collections = {
            c["name"] for c in db.collections() if not c["name"].startswith("_")
        }
    except (CollectionListError, ArangoServerError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Collection listing failed: {exc}",
        ) from exc

    checks: list[dict[str, Any]] = []

    # 1. Collections
    col_result = _check_collections(db)
    checks.append({"name": "collections", **col_result})
    # Extract counts for schema check reuse
    col_counts = {}
    if "collections" in col_result:
        col_counts = {
            k: v for k, v in col_result["collections"].items()
        }

    # 2. Views
    view_result, views_status = _check_views(db)
    checks.append({"name": "views", **view_result})

    # 3. Indexes
    checks.append({"name": "vector_indexes", **_check_indexes(db, existing_collections)})

    # 4. Embedding coverage
    checks.append({"name": "embedding_coverage", **_check_embedding_coverage(db, existing_collections)})

    # 5. Analyzer
    checks.append({"name": "analyzer", **_check_analyzer(db)})

    # 6. BM25
    checks.append({"name": "bm25", **_check_bm25(db, views_status)})

    # 7. View sync
    checks.append({"name": "view_sync", **_check_view_sync(db, views_status, existing_collections)})

    # 8. Duplicates
    checks.append({"name": "duplicates", **_check_duplicates(db, existing_collections)})

    # 9. Schema validation
    checks.append({"name": "schema", **_check_schema(db, existing_collections, col_counts)})

    passed = sum(1 for c in checks if c.get("status") == "pass")
    failed = len(checks) - passed

    return {
        "checks": checks,
        "summary": {"passed": passed, "failed": failed, "total": len(checks)},
    }
