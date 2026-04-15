"""Taxonomy tag management endpoints for the Graph Memory Service.

Purpose:
    Expose taxonomy coverage stats, untagged-document sampling, and tag
    write operations so that the /taxonomy and /monitor-taxonomy skills
    can drive cascade tagging without touching ArangoDB directly.

Inputs:
    - Collection name (``lessons`` or ``sparta_qra``).
    - For writes: document ``_key`` plus one or more tag-field payloads.
    - For batch writes: list of update dicts (max 500).

Outputs:
    - ``/taxonomy/coverage``  -> per-field tagged/untagged counts + pct.
    - ``/taxonomy/sample``    -> list of untagged document stubs.
    - ``/taxonomy/tag``       -> confirmation dict after single-doc update.
    - ``/taxonomy/batch-tag`` -> summary with success/error counts.

Failure modes:
    - 404 if the requested collection does not exist in ArangoDB.
    - 400 for unknown collection names or >500-item batch payloads.
    - 500 for unexpected ArangoDB errors (logged, then re-raised).
"""
from __future__ import annotations

import time as _time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from ...arango_client import get_db
from ...lessons.store import (
    VALID_INTENT_TAGS,
    get_intent_tags,
    get_mind_tags,
)  # noqa: F401  (re-exported for callers)

router = APIRouter(prefix="/taxonomy", tags=["taxonomy"])

# ── constants ──────────────────────────────────────────────────────────

TAXONOMY_COLLECTIONS = ["lessons", "sparta_qra", "app_actions"]

TAG_FIELDS: dict[str, dict[str, Any]] = {
    "lessons": {
        # bridge_attributes: Wave 4 will rename → "bridge"; kept as-is for now.
        "bridge": "bridge_attributes",
        # heart: 5 emotion tags (anger, fear, joy, sadness, trust).
        # Legacy alias: collection_tags (still accepted by write endpoints).
        "heart": "heart",
        # intent: 8 UI interaction tags (Navigate, Expand, Filter, Analyze, Compare, Trace, Layout, Persist).
        "intent": "intent",
        "text_fields": ["problem", "solution"],
    },
    "sparta_qra": {
        # bridge_attributes: deferred to Wave 4.
        "bridge": "bridge_attributes",
        # mind: 8 SPARTA tactical tags (Detect/Evade/Exploit/Harden/Isolate/Model/Persist/Restore).
        # Legacy alias: tactical_tags (still accepted by write endpoints, compat window).
        "mind": "mind",
        "text_fields": ["question", "reasoning", "answer"],
    },
    "app_actions": {
        # intent: 8 UI interaction tags (Navigate, Expand, Filter, Analyze, Compare, Trace, Layout, Persist).
        "intent": "intent",
        "text_fields": ["app", "action", "doc_type"],
    },
}

_BATCH_MAX = 500

# ── request / response models ─────────────────────────────────────────


class TaxonomyCoverageResult(BaseModel):
    collection: str
    total: int
    tagged: dict[str, int]
    untagged: dict[str, int]
    coverage_pct: dict[str, float]


class TaxonomySampleRequest(BaseModel):
    collection: str = "lessons"
    field: str = "bridge_attributes"
    limit: int = 100
    randomize: bool = False


class TaxonomyUntaggedRequest(BaseModel):
    """List untagged docs with pagination + optional scope filter."""
    collection: str = "lessons"
    field: str = "bridge_attributes"
    limit: int = 100
    offset: int = 0
    scope: str | None = None


class TaxonomyQueryRequest(BaseModel):
    """Filter docs by metadata fields."""
    collection: str = "lessons"
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description="Key-value filters. Special keys: 'untagged_field' (str) checks that field is null/empty.",
    )
    limit: int = 100
    offset: int = 0
    return_fields: list[str] | None = None


class TaxonomyTagRequest(BaseModel):
    collection: str = "lessons"
    key: str
    # Wave 1 / 2 fields (canonical names)
    bridge_attributes: list[str] | None = None
    # heart: emotion tags for lessons (anger, fear, joy, sadness, trust)
    heart: list[str] | None = None
    # mind: SPARTA tactical tags for sparta_qra (Detect, Evade, Exploit, Harden, Isolate, Model, Persist, Restore)
    mind: list[str] | None = None
    # intent: UI interaction tags for lessons (Navigate, Expand, Filter, Analyze, Compare, Trace, Layout, Persist)
    intent: list[str] | None = None
    # Legacy aliases — accepted during compat window, written to canonical field on disk.
    # tactical_tags → mind;  collection_tags → heart.  Both removed in Wave 4.
    tactical_tags: list[str] | None = None
    collection_tags: dict[str, str] | None = None
    taxonomy_method: str = "cascade"


class BatchTagItem(BaseModel):
    key: str
    # Wave 1 / 2 fields (canonical names)
    bridge_attributes: list[str] | None = None
    heart: list[str] | None = None
    mind: list[str] | None = None
    # intent: UI interaction tags for lessons
    intent: list[str] | None = None
    # Legacy aliases — compat window only.
    tactical_tags: list[str] | None = None
    collection_tags: dict[str, str] | None = None
    taxonomy_method: str = "cascade"


class BatchTagRequest(BaseModel):
    collection: str = "lessons"
    updates: list[BatchTagItem] = Field(..., min_length=1, max_length=_BATCH_MAX)


# ── helpers ────────────────────────────────────────────────────────────


def _validate_collection(collection: str) -> None:
    """Raise 400 for unknown names, 404 if the collection is missing in DB."""
    if collection not in TAXONOMY_COLLECTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown collection: {collection}. Use: {TAXONOMY_COLLECTIONS}",
        )
    db = get_db()
    if not db.has_collection(collection):
        raise HTTPException(
            status_code=404,
            detail=f"Collection '{collection}' not found in ArangoDB",
        )


def _sync_intent_edges(doc_key: str, intent_tags: list[str]) -> None:
    """Upsert app_action_edges for app_actions that share intent taxonomy."""
    valid_tags = sorted({tag for tag in intent_tags if tag in VALID_INTENT_TAGS})
    if not valid_tags:
        return

    db = get_db()
    source_id = f"app_actions/{doc_key}"
    created_at = datetime.now(timezone.utc).isoformat()

    query_related = """
    FOR doc IN app_actions
        FILTER doc._key != @doc_key
        FILTER IS_LIST(doc.intent)
        LET shared_tags = INTERSECTION(doc.intent, @intent_tags)
        FILTER LENGTH(shared_tags) > 0
        RETURN {
            target_id: doc._id,
            shared_tags: shared_tags
        }
    """
    upsert_edge = """
    UPSERT {
        _from: @from,
        _to: @to,
        intent_tag: @intent_tag
    }
    INSERT {
        _from: @from,
        _to: @to,
        intent_tag: @intent_tag,
        type: "similar_to",
        weight: 1.0,
        created_at: @created_at
    }
    UPDATE {
        type: "similar_to",
        weight: 1.0
    }
    IN app_action_edges
    """

    try:
        related = list(
            db.aql.execute(
                query_related,
                bind_vars={"doc_key": doc_key, "intent_tags": valid_tags},
            )
        )
        edge_count = 0
        for item in related:
            target_id = item.get("target_id")
            if not target_id:
                continue
            for intent_tag in item.get("shared_tags") or []:
                if intent_tag not in VALID_INTENT_TAGS:
                    continue
                db.aql.execute(
                    upsert_edge,
                    bind_vars={
                        "from": source_id,
                        "to": target_id,
                        "intent_tag": intent_tag,
                        "created_at": created_at,
                    },
                )
                edge_count += 1
        logger.info(
            "Synced {} app_action_edges for app_actions/{} using intent tags {}",
            edge_count,
            doc_key,
            valid_tags,
        )
    except Exception as exc:
        logger.error("Intent edge sync failed for app_actions/{}: {}", doc_key, exc)
        raise


# ── endpoints ──────────────────────────────────────────────────────────


@router.get("/coverage", response_model=TaxonomyCoverageResult)
def taxonomy_coverage(collection: str = "lessons") -> TaxonomyCoverageResult:
    """Return taxonomy tag coverage stats for a collection."""
    _validate_collection(collection)
    db = get_db()

    fields = TAG_FIELDS.get(collection, TAG_FIELDS["lessons"])
    tag_field_names = [v for k, v in fields.items() if k != "text_fields"]

    try:
        total: int = db.aql.execute(
            "RETURN LENGTH(@@coll)",
            bind_vars={"@coll": collection},
        ).next()

        tagged: dict[str, int] = {}
        untagged: dict[str, int] = {}
        coverage_pct: dict[str, float] = {}

        for field_name in tag_field_names:
            count_aql = """
            FOR doc IN @@coll
                FILTER doc.@field != null
                    AND (IS_LIST(doc.@field) ? LENGTH(doc.@field) > 0
                         : IS_OBJECT(doc.@field) ? LENGTH(ATTRIBUTES(doc.@field)) > 0
                         : true)
                COLLECT WITH COUNT INTO c
                RETURN c
            """
            count: int = db.aql.execute(
                count_aql,
                bind_vars={"@coll": collection, "field": field_name},
            ).next()
            tagged[field_name] = count
            untagged[field_name] = total - count
            coverage_pct[field_name] = round(100.0 * count / total, 2) if total > 0 else 0.0

        return TaxonomyCoverageResult(
            collection=collection,
            total=total,
            tagged=tagged,
            untagged=untagged,
            coverage_pct=coverage_pct,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Taxonomy coverage query failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/sample")
def taxonomy_sample(body: TaxonomySampleRequest) -> dict[str, Any]:
    """Return untagged documents for cascade assessment."""
    _validate_collection(body.collection)
    db = get_db()

    fields = TAG_FIELDS.get(body.collection, TAG_FIELDS["lessons"])
    text_fields: list[str] = fields["text_fields"]
    max_limit = 2500 if body.randomize else 500
    limit = min(body.limit, max_limit)

    try:
        sort_clause = "SORT RAND()" if body.randomize else ""
        aql = f"""
        FOR doc IN @@coll
            FILTER doc.@field == null
                OR (IS_LIST(doc.@field) AND LENGTH(doc.@field) == 0)
                OR doc.@field == []
            {sort_clause}
            LIMIT @limit
            RETURN KEEP(doc, APPEND(["_key", "scope"], @text_fields))
        """
        cursor = db.aql.execute(
            aql,
            bind_vars={
                "@coll": body.collection,
                "field": body.field,
                "limit": limit,
                "text_fields": text_fields,
            },
        )
        results = list(cursor)
        return {
            "collection": body.collection,
            "field": body.field,
            "count": len(results),
            "documents": results,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Taxonomy sample query failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.patch("/tag")
def taxonomy_tag(body: TaxonomyTagRequest) -> dict[str, Any]:
    """Write taxonomy tags to a specific document."""
    _validate_collection(body.collection)
    db = get_db()

    update: dict[str, Any] = {
        "taxonomy_method": body.taxonomy_method,
        "taxonomy_updated_at": _time.time(),
    }

    if body.bridge_attributes is not None:
        update["bridge_attributes"] = body.bridge_attributes
    # Canonical fields (preferred)
    if body.mind is not None:
        update["mind"] = body.mind
    if body.heart is not None:
        update["heart"] = body.heart
    if body.intent is not None:
        update["intent"] = body.intent
    # Legacy alias: tactical_tags → written as mind (compat window, Wave 4 removes this)
    if body.tactical_tags is not None and body.mind is None:
        update["mind"] = body.tactical_tags
    # Legacy alias: collection_tags → written as heart (compat window, Wave 4 removes this)
    if body.collection_tags is not None and body.heart is None:
        update["heart"] = body.collection_tags

    # taxonomy_method + taxonomy_updated_at are always present (2 keys).
    # If nothing else was added, the caller forgot to supply tag fields.
    if len(update) <= 2:
        raise HTTPException(status_code=400, detail="No tag fields provided")

    try:
        col = db.collection(body.collection)
        col.update({"_key": body.key, **update})
        if body.collection == "app_actions":
            intent_tags = get_intent_tags(update)
            if intent_tags:
                _sync_intent_edges(body.key, intent_tags)
        return {
            "updated": True,
            "key": body.key,
            "collection": body.collection,
            "fields": list(update.keys()),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Taxonomy tag update failed for {}/{}: {}",
            body.collection, body.key, exc,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/batch-tag")
def taxonomy_batch_tag(body: BatchTagRequest) -> dict[str, Any]:
    """Batch write taxonomy tags to multiple documents. Max 500 per request."""
    _validate_collection(body.collection)

    if len(body.updates) > _BATCH_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"Batch size {len(body.updates)} exceeds maximum of {_BATCH_MAX}",
        )

    db = get_db()
    now = _time.time()
    success = 0
    errors = 0

    try:
        col = db.collection(body.collection)
        for item in body.updates:
            doc: dict[str, Any] = {
                "_key": item.key,
                "taxonomy_method": item.taxonomy_method,
                "taxonomy_updated_at": now,
            }
            # Canonical fields (preferred)
            if item.bridge_attributes is not None:
                doc["bridge_attributes"] = item.bridge_attributes
            if item.mind is not None:
                doc["mind"] = item.mind
            elif item.tactical_tags is not None:
                # Legacy alias: tactical_tags → mind (compat window)
                doc["mind"] = item.tactical_tags
            if item.heart is not None:
                doc["heart"] = item.heart
            elif item.collection_tags is not None:
                # Legacy alias: collection_tags → heart (compat window)
                doc["heart"] = item.collection_tags
            if item.intent is not None:
                doc["intent"] = item.intent
            try:
                col.update(doc)
                success += 1
            except Exception as exc:
                logger.warning("batch-tag: failed to update {}/{}: {}", body.collection, item.key, exc)
                errors += 1
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Taxonomy batch-tag failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "collection": body.collection,
        "success": success,
        "errors": errors,
        "total": len(body.updates),
    }


# ── gap-closing endpoints (2026-03-13) ────────────────────────────────
# These endpoints exist so agents NEVER need raw AQL for common queries.


_UNTAGGED_MAX = 1000
_QUERY_MAX = 500
# API parameter allowlist (not domain knowledge — just field name validation)
_SAFE_FILTER_FIELDS = {
    "scope", "taxonomy_method", "batch_import", "demo",
    "code_symbol", "source", "category",
}


@router.post("/untagged")
def taxonomy_untagged(body: TaxonomyUntaggedRequest) -> dict[str, Any]:
    """Paginated listing of untagged docs for a specific tag field.

    Unlike /sample (randomized), this is deterministic with offset/limit
    so agents can systematically iterate through all untagged docs.
    """
    _validate_collection(body.collection)
    db = get_db()

    fields = TAG_FIELDS.get(body.collection, TAG_FIELDS["lessons"])
    text_fields: list[str] = fields["text_fields"]
    limit = min(body.limit, _UNTAGGED_MAX)

    scope_filter = ""
    bind_vars: dict[str, Any] = {
        "@coll": body.collection,
        "field": body.field,
        "limit": limit,
        "offset": body.offset,
        "text_fields": text_fields,
    }
    if body.scope:
        scope_filter = "FILTER doc.scope == @scope"
        bind_vars["scope"] = body.scope

    try:
        aql = f"""
        FOR doc IN @@coll
            FILTER doc.@field == null
                OR (IS_LIST(doc.@field) AND LENGTH(doc.@field) == 0)
                OR doc.@field == []
            {scope_filter}
            LIMIT @offset, @limit
            RETURN KEEP(doc, APPEND(["_key", "scope", "tags"], @text_fields))
        """
        results = list(db.aql.execute(aql, bind_vars=bind_vars))

        # Also get total count for pagination info
        count_aql = f"""
        FOR doc IN @@coll
            FILTER doc.@field == null
                OR (IS_LIST(doc.@field) AND LENGTH(doc.@field) == 0)
                OR doc.@field == []
            {scope_filter}
            COLLECT WITH COUNT INTO c
            RETURN c
        """
        count_vars = {k: v for k, v in bind_vars.items()
                      if k not in ("limit", "offset", "text_fields")}
        total = db.aql.execute(count_aql, bind_vars=count_vars).next()

        return {
            "collection": body.collection,
            "field": body.field,
            "total_untagged": total,
            "offset": body.offset,
            "limit": limit,
            "count": len(results),
            "documents": results,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Taxonomy untagged query failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/query")
def taxonomy_query(body: TaxonomyQueryRequest) -> dict[str, Any]:
    """Filter docs by metadata fields. Replaces raw AQL for common queries.

    Supported filters:
      - Any field in _SAFE_FILTER_FIELDS: exact match (scope, taxonomy_method, etc.)
      - "untagged_field": name of a tag field to filter for null/empty

    Example: {"collection": "lessons", "filters": {"scope": "operational",
              "untagged_field": "bridge_attributes"}, "limit": 50}
    """
    _validate_collection(body.collection)
    db = get_db()

    fields = TAG_FIELDS.get(body.collection, TAG_FIELDS["lessons"])
    text_fields: list[str] = fields["text_fields"]
    limit = min(body.limit, _QUERY_MAX)

    filter_clauses: list[str] = []
    bind_vars: dict[str, Any] = {
        "@coll": body.collection,
        "limit": limit,
        "offset": body.offset,
    }

    for key, value in body.filters.items():
        if key == "untagged_field":
            bind_vars["untagged_field"] = value
            filter_clauses.append(
                "(doc.@untagged_field == null"
                " OR (IS_LIST(doc.@untagged_field) AND LENGTH(doc.@untagged_field) == 0)"
                " OR doc.@untagged_field == [])"
            )
        elif key in _SAFE_FILTER_FIELDS:
            param = f"fv_{key}"
            bind_vars[param] = value
            filter_clauses.append(f"doc.{key} == @{param}")
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Filter field '{key}' not allowed. Use: {sorted(_SAFE_FILTER_FIELDS)} or 'untagged_field'",
            )

    where = (" FILTER " + " AND ".join(filter_clauses)) if filter_clauses else ""

    # Determine return fields
    if body.return_fields:
        keep_fields = list(set(["_key", "scope"] + body.return_fields))
    else:
        keep_fields = ["_key", "scope", "tags", "bridge_attributes", "mind", "heart", "intent"] + text_fields

    bind_vars["keep_fields"] = keep_fields

    try:
        aql = f"""
        FOR doc IN @@coll
            {where}
            LIMIT @offset, @limit
            RETURN KEEP(doc, @keep_fields)
        """
        results = list(db.aql.execute(aql, bind_vars=bind_vars))
        return {
            "collection": body.collection,
            "filters": body.filters,
            "offset": body.offset,
            "limit": limit,
            "count": len(results),
            "documents": results,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Taxonomy query failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── /taxonomy/extract — text → structured taxonomy tags ──────────────
# Takes text (e.g., binary feature description), finds matching CWE/ATT&CK
# controls via BM25 search of sparta_controls + sparta_qra, then extracts
# mind tags and returns structured tags. No LLM — deterministic search.


class TaxonomyExtractRequest(BaseModel):
    """Extract taxonomy tags from free text."""
    text: str = Field(..., min_length=1, description="Text to extract taxonomy from")
    k: int = Field(10, ge=1, le=50, description="Max results per collection search")
    min_score: float = Field(3.0, ge=0, description="Minimum BM25 score to include a result")


class TaxonomyExtractResponse(BaseModel):
    """Structured taxonomy tags extracted from text."""
    cwe: list[str] = []
    attack: list[str] = []
    capec: list[str] = []
    d3fend: list[str] = []
    nist: list[str] = []
    mind: list[str] = []
    controls_searched: int = 0
    qra_searched: int = 0


@router.post("/extract")
def taxonomy_extract(body: TaxonomyExtractRequest) -> TaxonomyExtractResponse:
    """Extract CWE, ATT&CK, CAPEC, D3FEND, NIST, and mind tags from text.

    Searches sparta_controls and sparta_qra via BM25 to find matching
    security taxonomy. No LLM — purely search-based extraction.
    """
    import re

    db = get_db()
    cwe_ids: set[str] = set()
    attack_ids: set[str] = set()
    capec_ids: set[str] = set()
    d3fend_ids: set[str] = set()
    nist_ids: set[str] = set()
    mind_tags: set[str] = set()
    controls_searched = 0
    qra_searched = 0

    text = body.text.strip()

    try:
        # ── 1. Search sparta_controls for matching CWE/NIST/D3FEND ──
        if db.has_collection("sparta_controls"):
            aql_controls = """
            FOR doc IN sparta_controls_search
                SEARCH ANALYZER(
                    BOOST(PHRASE(doc.name, @text), 3) ||
                    BOOST(ANALYZER(doc.name IN TOKENS(@text, 'text_en'), 'text_en'), 2) ||
                    ANALYZER(doc.control_id IN TOKENS(@text, 'text_en'), 'text_en'),
                    'text_en'
                )
                SORT BM25(doc) DESC
                LIMIT @k
                RETURN {
                    control_id: doc.control_id,
                    name: doc.name,
                    framework: doc.framework,
                    pillar_cwe: doc.pillar_cwe,
                    capec_ids: doc.capec_ids,
                    score: BM25(doc)
                }
            """
            try:
                results = list(db.aql.execute(aql_controls, bind_vars={"text": text, "k": body.k}))
                controls_searched = len(results)
                for r in results:
                    if (r.get("score") or 0) < body.min_score:
                        continue
                    cid = r.get("control_id", "")
                    fw = r.get("framework", "")
                    if cid.startswith("CWE-"):
                        cwe_ids.add(cid)
                    elif cid.startswith("T") and re.match(r"^T\d{4}", cid):
                        attack_ids.add(cid)
                    elif cid.startswith("D3-"):
                        d3fend_ids.add(cid)
                    elif fw == "nist" or cid.startswith("AC-") or cid.startswith("IA-") or cid.startswith("SC-"):
                        nist_ids.add(cid)
                    # Harvest CAPEC IDs from control metadata
                    for capec in (r.get("capec_ids") or []):
                        capec_ids.add(f"CAPEC-{capec}" if not str(capec).startswith("CAPEC") else str(capec))
            except Exception as exc:
                logger.debug("sparta_controls search failed (view may not exist): {}", exc)

        # ── 2. Search sparta_qra for mind tags + additional CWEs ──
        if db.has_collection("sparta_qra"):
            aql_qra = """
            FOR doc IN sparta_qra_search
                SEARCH ANALYZER(
                    BOOST(PHRASE(doc.question, @text), 2) ||
                    ANALYZER(doc.question IN TOKENS(@text, 'text_en'), 'text_en') ||
                    ANALYZER(doc.answer IN TOKENS(@text, 'text_en'), 'text_en'),
                    'text_en'
                )
                SORT BM25(doc) DESC
                LIMIT @k
                RETURN {
                    mind: doc.mind,
                    tactical_tags: doc.tactical_tags,
                    tags: doc.tags,
                    control_id: doc.control_id,
                    score: BM25(doc)
                }
            """
            try:
                results = list(db.aql.execute(aql_qra, bind_vars={"text": text, "k": body.k}))
                qra_searched = len(results)
                for r in results:
                    if (r.get("score") or 0) < body.min_score:
                        continue
                    # Mind tags (SPARTA tactical: Detect, Evade, Exploit, etc.)
                    for tag in (r.get("mind") or r.get("tactical_tags") or []):
                        if tag:
                            mind_tags.add(tag)
                    # Tags may contain CWE/ATT&CK references
                    for tag in (r.get("tags") or []):
                        if isinstance(tag, str):
                            if tag.startswith("CWE-"):
                                cwe_ids.add(tag)
                            elif tag.startswith("control:CWE-"):
                                cwe_ids.add(tag.replace("control:", ""))
                            elif tag.startswith("control:CAPEC-"):
                                capec_ids.add(tag.replace("control:", ""))
                            elif re.match(r"^T\d{4}", tag):
                                attack_ids.add(tag)
                    # Control ID field
                    cid = r.get("control_id", "")
                    if isinstance(cid, str) and cid.startswith("CWE-"):
                        cwe_ids.add(cid)
            except Exception as exc:
                logger.debug("sparta_qra search failed (view may not exist): {}", exc)

        # ── 3. Search sparta_relationships for CWE→ATT&CK edges ──
        if cwe_ids and db.has_collection("sparta_relationships"):
            aql_rels = """
            FOR doc IN sparta_relationships
                FILTER doc.source_control_id IN @cwe_ids
                    OR doc.target_control_id IN @cwe_ids
                LIMIT 20
                RETURN {
                    source: doc.source_control_id,
                    source_fw: doc.source_framework,
                    target: doc.target_control_id,
                    target_fw: doc.target_framework
                }
            """
            try:
                rels = list(db.aql.execute(aql_rels, bind_vars={"cwe_ids": list(cwe_ids)}))
                for rel in rels:
                    for side in ["source", "target"]:
                        cid = rel.get(side, "")
                        fw = rel.get(f"{side}_fw", "")
                        if cid.startswith("CWE-"):
                            cwe_ids.add(cid)
                        elif fw == "attack" or re.match(r"^T\d{4}", cid):
                            attack_ids.add(cid)
                        elif cid.startswith("CAPEC-"):
                            capec_ids.add(cid)
            except Exception as exc:
                logger.debug("sparta_relationships lookup failed: {}", exc)

    except Exception as exc:
        logger.error("Taxonomy extract failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return TaxonomyExtractResponse(
        cwe=sorted(cwe_ids),
        attack=sorted(attack_ids),
        capec=sorted(capec_ids),
        d3fend=sorted(d3fend_ids),
        nist=sorted(nist_ids),
        mind=sorted(mind_tags),
        controls_searched=controls_searched,
        qra_searched=qra_searched,
    )
