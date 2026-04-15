"""Core endpoints: recall, learn, related, residue, trace, edges, clarify, deflect."""
from __future__ import annotations

import asyncio
import threading
from functools import partial

from fastapi import APIRouter, HTTPException
from loguru import logger

from ...api import MemoryClient
from ... import api as tom_api
from ._models import (
    RecallRequest,
    ByKeysRequest,
    ListRequest,
    UpsertRequest,
    LearnRequest,
    RelatedRequest,
    ResidueRequest,
    TraceRequest,
    AddEdgeRequest,
    AddEdgesBatchRequest,
    ClarifyRequest,
    DeflectRequest,
    ExploreRequest,
    QueryRequest,
)
from ._misuse_guard import (
    validate_collection_name,
    validate_recall_request,
    validate_upsert_request,
    check_deprecated_endpoint,
)

router = APIRouter()
_client = MemoryClient()


# ---- Recall / Learn / Related / Residue ------------------------------------


@router.post("/recall")
async def recall(req: RecallRequest) -> dict:
    # Validate request — catches empty queries, AQL in query string
    validate_recall_request({"q": req.q})
    loop = asyncio.get_running_loop()
    k = req.effective_k()
    return await loop.run_in_executor(
        None,
        partial(
            _client.recall,
            q=req.q,
            scope=req.scope or _client.default_scope,
            k=k,
            threshold=req.threshold,
            collections=req.collections,
            tags=req.tags,
            crosswalk_methods=req.crosswalk_methods,
        ),
    )


@router.post("/learn")
async def learn(req: LearnRequest) -> dict:
    """Deprecated: use POST /store with collection='lessons' instead.
    Kept for backward compatibility — redirects to /store internally."""
    check_deprecated_endpoint("/learn")  # Logs warning about deprecation
    if not req.problem.strip() or not req.solution.strip():
        raise HTTPException(status_code=400, detail="problem and solution are required")
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None,
            partial(
                _client.learn,
                problem=req.problem,
                solution=req.solution,
                scope=req.scope or _client.default_scope,
                tags=req.tags,
                code_symbol=req.code_symbol,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---- By-Keys / List (gap-closing, 2026-03-13) --------------------------------


# Sort fields are validated because sort_field is interpolated into AQL (not bind-variable safe)
_ALLOWED_SORT_FIELDS = {"_key", "created_at", "updated_at", "scope", "taxonomy_updated_at", "last_validated", "nrs_score", "combined_score", "control_id"}
_ALLOWED_FILTER_FIELDS = {  # kept for backward compat log but not enforced
    "source_framework", "scope", "category", "control_id", "control_type", "status",
    "heart", "mind", "collection_tag", "tags",
    "binary_name", "node_type", "namespace", "cluster", "edge_type",
    # app_actions fields (added 2026-03-31)
    "app", "action", "doc_type", "element_id", "labeler",
    # Relationship scoring (added 2026-03-29 for 08b_score_relationships)
    "method", "source_control_id", "target_control_id", "tier",
    # Datalake chunk fields (added 2026-03-31 for F-36 typed retrieval)
    "asset_type", "doc_id", "content_type", "source",
    # Datalake document fields (added 2026-03-31 for Datalake Explorer)
    "filename", "pages", "format", "classification",
    # LLM call log fields (added 2026-04-05 for /learn-timeout and /orchestrate)
    "model_requested", "model_served", "provider", "task_type", "date", "caller",
}


_ALLOWED_KEY_FIELDS = {"_key", "_to", "_from", "url_id", "control_id", "source_control_id", "target_control_id", "user_id", "agent_id", "scope", "doc_id"}


@router.post("/recall/by-keys")
def recall_by_keys(req: ByKeysRequest) -> dict:
    """Fetch documents by key list. Supports batch lookups on any allowlisted field."""
    if req.collection.startswith("_"):
        raise HTTPException(status_code=400, detail=f"System collection not accessible: {req.collection}")
    if req.key_field not in _ALLOWED_KEY_FIELDS:
        raise HTTPException(status_code=400, detail=f"Key field not allowed: {req.key_field}")

    from ...arango_client import get_db
    db = get_db()
    if not db.has_collection(req.collection):
        raise HTTPException(status_code=404, detail=f"Collection '{req.collection}' not found")

    try:
        filter_clause = f"FILTER doc.{req.key_field} IN @keys"
        if req.return_fields:
            keep = list(set(["_key", req.key_field] + req.return_fields))
            aql = f"FOR doc IN @@coll {filter_clause} RETURN KEEP(doc, @keep)"
            bind: dict = {"@coll": req.collection, "keys": req.keys, "keep": keep}
        else:
            aql = f"FOR doc IN @@coll {filter_clause} RETURN doc"
            bind = {"@coll": req.collection, "keys": req.keys}

        results = list(db.aql.execute(aql, bind_vars=bind))
        return {"collection": req.collection, "count": len(results), "documents": results}
    except Exception as exc:
        logger.error("recall/by-keys failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/list")
def list_documents(req: ListRequest) -> dict:
    """Paginated collection listing. Eliminates need for raw AQL LIMIT queries."""
    if req.collection.startswith("_"):
        raise HTTPException(status_code=400, detail=f"System collection not accessible: {req.collection}")
    if req.sort_field not in _ALLOWED_SORT_FIELDS:
        raise HTTPException(status_code=400, detail=f"Sort field not allowed: {req.sort_field}")
    if req.sort_order not in ("ASC", "DESC"):
        raise HTTPException(status_code=400, detail="sort_order must be ASC or DESC")

    from ...arango_client import get_db
    db = get_db()
    if not db.has_collection(req.collection):
        raise HTTPException(status_code=404, detail=f"Collection '{req.collection}' not found")

    # Block system fields only
    if req.filters:
        for field in req.filters:
            if field.startswith("_") and field not in ("_key",):
                raise HTTPException(status_code=400, detail=f"System field not filterable: {field}")

    try:
        sort_clause = f"SORT doc.{req.sort_field} {req.sort_order}"
        if req.return_fields:
            keep = list(set(["_key"] + req.return_fields))
            ret = "RETURN KEEP(doc, @keep)"
            bind: dict = {"@coll": req.collection, "limit": req.limit, "offset": req.offset, "keep": keep}
        else:
            ret = "RETURN doc"
            bind = {"@coll": req.collection, "limit": req.limit, "offset": req.offset}

        # Build FILTER clause — supports equality, null checks, and array-contains
        filter_parts = []
        if req.filters:
            for field, value in req.filters.items():
                bind_key = f"f_{field}"
                if value is None:
                    # Handle missing fields and null values
                    filter_parts.append(f"(!HAS(doc, '{field}') OR doc.{field} == null)")
                else:
                    filter_parts.append(f"doc.{field} == @{bind_key}")
                    bind[bind_key] = value
        if req.tags:
            for i, tag in enumerate(req.tags):
                bind_key = f"tag_{i}"
                filter_parts.append(f"POSITION(doc.tags, @{bind_key})")
                bind[bind_key] = tag
        filter_clause = ("FILTER " + " AND ".join(filter_parts)) if filter_parts else ""

        # Count FIRST (before fetching) to detect unbounded result sets
        count_bind: dict = {"@coll": req.collection}
        if filter_clause:
            if req.filters:
                for field, value in req.filters.items():
                    if value is not None:
                        count_bind[f"f_{field}"] = value
            if req.tags:
                for i, tag in enumerate(req.tags):
                    count_bind[f"tag_{i}"] = tag
            count_aql = f"RETURN LENGTH(FOR doc IN @@coll {filter_clause} RETURN 1)"
        else:
            count_aql = "RETURN LENGTH(@@coll)"
        total = db.aql.execute(count_aql, bind_vars=count_bind).next()

        # Misuse guard: prevent timeout on unbounded large result sets
        from ._misuse_guard import validate_list_result_count
        validate_list_result_count(req.collection, total, req.limit)

        aql = f"FOR doc IN @@coll {filter_clause} {sort_clause} LIMIT @offset, @limit {ret}"
        results = list(db.aql.execute(aql, bind_vars=bind))

        return {
            "collection": req.collection,
            "total": total,
            "offset": req.offset,
            "limit": req.limit,
            "count": len(results),
            "documents": results,
        }
    except Exception as exc:
        logger.error("list failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/count")
def count_documents(req: ListRequest) -> dict:
    """Count-only endpoint — same filters as /list but returns just the count."""
    if req.collection.startswith("_"):
        raise HTTPException(status_code=400, detail=f"System collection not accessible: {req.collection}")

    from ...arango_client import get_db
    db = get_db()
    if not db.has_collection(req.collection):
        raise HTTPException(status_code=404, detail=f"Collection '{req.collection}' not found")

    try:
        bind: dict = {"@coll": req.collection}
        filter_parts: list = []
        if req.filters:
            for field, value in req.filters.items():
                if field.startswith("_") and field != "_key":
                    raise HTTPException(status_code=400, detail=f"System field not filterable: {field}")
                bind_key = f"f_{field}"
                if value is None:
                    # Handle missing fields and null values
                    filter_parts.append(f"(!HAS(doc, '{field}') OR doc.{field} == null)")
                else:
                    filter_parts.append(f"doc.{field} == @{bind_key}")
                    bind[bind_key] = value
        if req.tags:
            for i, tag in enumerate(req.tags):
                bind_key = f"tag_{i}"
                filter_parts.append(f"POSITION(doc.tags, @{bind_key})")
                bind[bind_key] = tag

        if filter_parts:
            filter_clause = "FILTER " + " AND ".join(filter_parts)
            aql = f"RETURN LENGTH(FOR doc IN @@coll {filter_clause} RETURN 1)"
        else:
            aql = "RETURN LENGTH(@@coll)"
        count = db.aql.execute(aql, bind_vars=bind).next()
        return {"collection": req.collection, "count": count}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("count failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---- Upsert (generic collection writes, 2026-03-17) -------------------------

# /upsert blocks system collections only — all user collections are writable
# and domain_terms/taxonomy_vocabulary (managed by taxonomy pipeline).
_BLOCKED_COLLECTIONS = {"_graphs", "_analyzers", "_jobs", "_queues", "_statistics"}
_FORBIDDEN_FIELDS = {"_id", "_rev"}  # ArangoDB internals — never accept from client


@router.post("/upsert")
def upsert_documents(req: UpsertRequest) -> dict:
    """Upsert documents into any collection. Each document must have _key.
    Existing documents are merged (updated), new ones are inserted.
    Only ArangoDB system collections are blocked. Auto-creates missing collections."""
    # Validate collection name and request body
    validate_collection_name(req.collection, "/upsert")
    validate_upsert_request({"documents": req.documents, "collection": req.collection})

    if req.collection.startswith("_") or req.collection in _BLOCKED_COLLECTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"System collection not writable via /upsert: {req.collection}",
        )

    # Validate all documents have _key and no forbidden fields
    for i, doc in enumerate(req.documents):
        if "_key" not in doc:
            raise HTTPException(status_code=400, detail=f"Document {i} missing _key")
        for field in _FORBIDDEN_FIELDS:
            if field in doc:
                raise HTTPException(status_code=400, detail=f"Document {i} contains forbidden field: {field}")

    from ...arango_client import get_db
    db = get_db()
    if not db.has_collection(req.collection):
        db.create_collection(req.collection)

    try:
        coll = db.collection(req.collection)
        errors = []

        # --- Phase 1: Batch fetch existing docs (one AQL query, not O(n) has() calls) ---
        keys = [doc["_key"] for doc in req.documents]
        existing_map: dict[str, dict] = {}
        if keys:
            cursor = db.aql.execute(
                "FOR doc IN @@coll FILTER doc._key IN @keys RETURN doc",
                bind_vars={"@coll": req.collection, "keys": keys},
            )
            for edoc in cursor:
                existing_map[edoc["_key"]] = edoc

        # Build merged docs
        merged_docs: list[dict] = []
        for doc in req.documents:
            existing = existing_map.get(doc["_key"])
            if existing:
                # Merge: overlay new fields onto existing
                merged = {**existing, **doc}
                # Remove _rev to avoid revision conflicts
                merged.pop("_rev", None)
            else:
                merged = doc
            merged_docs.append(merged)

        # --- Phase 2: Batch embedding (only if skip_embedding not set) ---
        docs_needing_embed: list[tuple[int, dict, str]] = []
        if not getattr(req, "skip_embedding", False):
            for i, merged in enumerate(merged_docs):
                embed_text = _embed_text_for_doc(merged)
                if embed_text.strip():
                    docs_needing_embed.append((i, merged, embed_text))

            if docs_needing_embed:
                from ...embeddings import encode_texts
                texts = [t for _, _, t in docs_needing_embed]
                try:
                    vectors = encode_texts(texts)
                    for (idx, _, _), vec in zip(docs_needing_embed, vectors):
                        if vec:
                            merged_docs[idx]["embedding"] = vec
                except Exception as exc:
                    logger.warning("Batch embedding failed, proceeding without: {}", exc)

        # --- Phase 3: Batch upsert with AQL (one query, not O(n) insert/update calls) ---
        # Use UPSERT with MERGE(OLD, doc) for server-side merge
        result = db.aql.execute(
            """
            LET existing_keys = (FOR doc IN @@coll FILTER doc._key IN @keys RETURN doc._key)
            FOR doc IN @docs
                UPSERT {_key: doc._key}
                INSERT doc
                UPDATE MERGE(OLD, doc)
                IN @@coll
                OPTIONS {ignoreRevs: true}
                RETURN {_key: doc._key, _is_new: doc._key NOT IN existing_keys}
            """,
            bind_vars={"@coll": req.collection, "keys": keys, "docs": merged_docs},
        )

        inserted = 0
        updated = 0
        for r in result:
            if r.get("_is_new"):
                inserted += 1
            else:
                updated += 1

        return {
            "collection": req.collection,
            "inserted": inserted,
            "updated": updated,
            "errors": errors,
            "total": len(req.documents),
        }
    except Exception as exc:
        logger.error("upsert failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# Text fields to embed per collection — concatenated for the embedding vector.
_EMBED_FIELDS: dict[str, list[str]] = {
    "sparta_controls": ["name", "description", "control_id"],
    "sparta_qra": ["question", "answer"],
    "sparta_urls": ["url", "title", "description"],
    "sparta_url_content": ["content", "title"],
    "sparta_url_knowledge": ["knowledge", "title"],
    "sparta_relationships": ["description", "relationship_type"],
    "technique_knowledge": ["name", "description"],
    "binary_features": ["name", "description", "category"],
}


def _embed_text_for_doc(doc: dict) -> str:
    """Build embedding text from a document's text fields based on its collection."""
    # Try collection-specific fields first (set by caller context)
    # Fall back to common text fields
    parts = []
    for field in ("name", "description", "question", "answer", "title",
                  "problem", "solution", "content", "knowledge", "control_id"):
        val = doc.get(field)
        if val and isinstance(val, str):
            parts.append(val)
    tags = doc.get("tags") or doc.get("collection_tag") or []
    if isinstance(tags, list) and tags:
        parts.append(" ".join(str(t) for t in tags))
    elif isinstance(tags, str) and tags:
        parts.append(tags)
    return " ".join(parts)


# ---- Query (Safe AQL execution, 2026-03-21) --------------------------------


@router.post("/query")
def execute_query(req: QueryRequest) -> dict:
    """Execute raw AQL safely (read-only)."""
    # Basic safety check to prevent destructive AQL
    aql_upper = req.aql.upper()
    forbidden = ["INSERT ", "UPDATE ", "REPLACE ", "REMOVE ", "UPSERT "]
    for f in forbidden:
        if f in aql_upper:
            raise HTTPException(status_code=400, detail=f"Destructive AQL forbidden: {f.strip()}")

    from ...arango_client import get_db
    db = get_db()
    try:
        results = list(db.aql.execute(req.aql, bind_vars=req.bind_vars or {}))
        return {"count": len(results), "documents": results, "aql": req.aql}
    except Exception as exc:
        logger.error("query failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/explore")
def explore_query(req: ExploreRequest) -> dict:
    """Dynamic NL-to-AQL graph traversal (Server-side LLM execution)."""
    from ...llm.client import call_llm_json
    
    prompt = f"""You are an ArangoDB AQL expert. Convert the user's natural language query into a single, valid, read-only AQL query.

### Graph Schema
- `binary_features` (Document Collection): represents nodes. Fields: `_key`, `_id`, `label`, `nodeType`, `cluster`, `tier`
- `binary_feature_edges` (Edge Collection): represents connections. Fields: `_key`, `_id`, `_from`, `_to`, `edge_type`

### Instructions
1. MUST be read-only (NO INSERT, UPDATE, REMOVE, UPSERT).
2. If searching by string, use `FILTER LIKE(v.label, '%...%')` instead of exact IDs if the exact ID isn't known.
3. For paths/hops: `FOR start IN binary_features FILTER LIKE(start.label, '%<node_name>%') LIMIT 1 FOR v, e, p IN 1..2 ANY start binary_feature_edges RETURN v`

User Query: {req.q}

Return a single JSON object with the key "aql" containing the raw query string."""

    try:
        payload, _ = call_llm_json(prompt, profile="fast")
        aql = payload.get("aql")
        if not aql:
            raise HTTPException(status_code=500, detail="LLM failed to generate AQL")
            
        logger.info(f"Generated AQL for query '{req.q}': {aql}")
        return execute_query(QueryRequest(aql=aql))
    except Exception as exc:
        logger.error("explore failed: {}", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc



@router.post("/related")
def related(req: RelatedRequest) -> dict:
    if not req.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    return _client.related(title=req.title, scope=req.scope, k=req.k)


@router.post("/residue")
def residue(req: ResidueRequest) -> dict:
    return _client.residue(limit=req.limit)


# ---- Trace -----------------------------------------------------------------


@router.post("/trace")
async def trace_endpoint(req: TraceRequest) -> dict:
    """Trace provenance for a query and optional answer.

    Returns directed provenance graph showing which documents,
    controls, and edges contributed to the answer.
    """
    if not req.q.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")
    if req.mode not in ("instant", "fast", "accurate"):
        raise HTTPException(status_code=400, detail="mode must be instant|fast|accurate")
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            partial(
                tom_api.trace_provenance,
                q=req.q,
                answer=req.answer,
                scope=req.scope,
                mode=req.mode,
                k=req.k,
                depth=req.depth,
            ),
        )
    except (ConnectionError, OSError) as exc:
        logger.error("trace failed (connectivity): {}", exc)
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}")
    except Exception as exc:
        logger.error("trace failed: {}", exc)
        raise HTTPException(status_code=500, detail=f"Trace error: {exc}")


# ---- Edges -----------------------------------------------------------------


@router.post("/add-edge")
def add_edge_endpoint(req: AddEdgeRequest) -> dict:
    return _client.add_edge(
        from_title=req.from_title,
        to_title=req.to_title,
        type=req.type,
        from_scope=req.from_scope,
        to_scope=req.to_scope,
        weight=req.weight,
        rationale=req.rationale,
    )


@router.post("/add-edges")
def add_edges_batch_endpoint(req: AddEdgesBatchRequest) -> dict:
    """Batch add edges -- much faster than individual calls."""
    stored = 0
    failed = 0
    not_found = 0
    for edge in req.edges:
        try:
            result = _client.add_edge(
                from_title=edge.from_title,
                to_title=edge.to_title,
                type=edge.type,
                from_scope=edge.from_scope,
                to_scope=edge.to_scope,
                weight=edge.weight,
                rationale=edge.rationale,
            )
            if result.get("meta", {}).get("ok"):
                stored += 1
            elif "from/to not found" in (result.get("errors") or []):
                not_found += 1
            else:
                failed += 1
        except Exception as exc:
            logger.error("Suppressed error in app: {}", exc)
            failed += 1
    return {"stored": stored, "failed": failed, "not_found": not_found, "total": len(req.edges)}


# ---- Clarify ---------------------------------------------------------------


@router.post("/clarify")
async def clarify_endpoint(req: ClarifyRequest) -> dict:
    """Detect ambiguous queries and generate specific clarifying questions.

    Combines intent mapping + taxonomy extraction + QRA corpus correlation
    to determine if a query needs clarification. When it does, returns
    persona-voiced follow-up questions referencing specific SPARTA controls.
    """
    if not req.q.strip():
        raise HTTPException(status_code=400, detail="q is required")

    from ...cli.query import _clarify_direct

    result = await asyncio.get_event_loop().run_in_executor(
        None,
        partial(
            _clarify_direct,
            q=req.q,
            persona=req.persona_id,
            scope=req.scope,
            context=req.context,
            k=req.k,
        ),
    )
    return result


# ---- Deflect ---------------------------------------------------------------

# Lazy classifier singleton (thread-safe)
_ambiguity_predictor = None
_ambiguity_load_attempted = False
_ambiguity_lock = threading.Lock()


def _get_ambiguity_predictor():
    global _ambiguity_predictor, _ambiguity_load_attempted
    if _ambiguity_load_attempted:
        return _ambiguity_predictor
    with _ambiguity_lock:
        if _ambiguity_load_attempted:
            return _ambiguity_predictor
        _ambiguity_load_attempted = True
        try:
            from graph_memory.classifiers import AmbiguityPredictor
            _ambiguity_predictor = AmbiguityPredictor.load()
            if _ambiguity_predictor:
                logger.info("Ambiguity classifier loaded for /deflect endpoint")
        except Exception as exc:
            logger.warning(f"Ambiguity classifier not available: {exc}")
    return _ambiguity_predictor


@router.post("/deflect")
async def deflect_endpoint(req: DeflectRequest) -> dict:
    """Classify query intent and deflect off-topic/inappropriate queries."""
    if not req.q.strip():
        raise HTTPException(status_code=400, detail="q is required")

    intent_action = req.intent_action
    classifier_result = None

    # Auto-classify if no explicit intent
    if intent_action is None:
        predictor = _get_ambiguity_predictor()
        if predictor:
            classifier_result = predictor.predict(req.q)
            intent_action = classifier_result.get("action", "QUERY")

    # Run deflection in thread pool (file I/O for session store)
    from persona.bridge.deflect import deflect as run_deflect

    # Extract safety flags from classifier result
    classifier_flags = None
    if classifier_result:
        classifier_flags = {
            "is_abusive": classifier_result.get("is_abusive", False),
            "is_sexual": classifier_result.get("is_sexual", False),
            "is_suspicious": classifier_result.get("is_suspicious", False),
        }

    result = await asyncio.get_event_loop().run_in_executor(
        None,
        partial(
            run_deflect,
            query=req.q,
            persona_id=req.persona_id,
            intent_action=intent_action,
            classifier_flags=classifier_flags,
            user_id=req.user_id,
            session_id=req.session_id,
        ),
    )

    output = result.to_dict()
    if classifier_result:
        output["classifier"] = {
            "action": classifier_result.get("action"),
            "confidence": classifier_result.get("confidence"),
            "labels": classifier_result.get("labels", []),
            "is_abusive": classifier_result.get("is_abusive", False),
            "is_sexual": classifier_result.get("is_sexual", False),
            "is_suspicious": classifier_result.get("is_suspicious", False),
        }
    return output
