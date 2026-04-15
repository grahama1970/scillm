"""Query execution and supplemental hit gathering for RecallSources."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from ._declarations import RecallSource, _safe, builtin_sources
from ._formatters import apply_formatter


def _cosine_rerank_inline(rows: List[Dict[str, Any]], q: str) -> Dict[str, float]:
    """Cosine rerank using inline doc.embedding. Returns {_key: score}."""
    try:
        import numpy as np
    except ImportError:
        logger.error("numpy not available — semantic rerank DISABLED")
        return {}

    vecs = []
    keys = []
    missing = 0
    for row in rows:
        doc = row.get("doc") or {}
        emb = doc.get("embedding")
        if emb and isinstance(emb, list) and len(emb) > 10:
            vecs.append(emb)
            keys.append(doc.get("_key", ""))
        else:
            missing += 1

    if missing > 0:
        logger.error(
            "INLINE EMBEDDING MISSING on {}/{} supplemental docs — cosine rerank degraded",
            missing, len(rows),
        )
    if not vecs:
        return {}

    # Get query vector from embedding service
    from ...config import EMBEDDING_SERVICE_URL
    qvec = None
    if EMBEDDING_SERVICE_URL:
        try:
            from graph_memory.http_clients import get_session
            resp = get_session().post(
                f"{EMBEDDING_SERVICE_URL.rstrip('/')}/embed",
                json={"text": q},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                vec = data.get("embedding") or data.get("vector", [])
                if vec:
                    qvec = np.array(vec, dtype="float32").reshape(1, -1)
        except Exception as exc:
            logger.error("Embedding service FAILED for supplemental rerank: {}", exc)

    if qvec is None:
        logger.error("No query embedding — supplemental cosine rerank DISABLED")
        return {}

    emb_matrix = np.array(vecs, dtype="float32")
    norms = np.linalg.norm(emb_matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    emb_normed = emb_matrix / norms
    qnorm = np.linalg.norm(qvec)
    if qnorm <= 0:
        return {}
    qvec_normed = qvec / qnorm
    scores = (emb_normed @ qvec_normed.T).flatten()
    return {keys[i]: float(scores[i]) for i in range(len(keys)) if scores[i] > 0}


def sanitize_fields(source: RecallSource) -> RecallSource:
    for field_name in [source.view, source.collection, *(source.text_fields or []), *(source.result_fields or [])]:
        if field_name:
            _safe(field_name)
    if source.scope_field:
        _safe(source.scope_field)
    if source.tags_field:
        _safe(source.tags_field)
    return source


def _load_json_config(raw: str) -> List[RecallSource]:
    data = json.loads(raw)
    sources: List[RecallSource] = []
    if isinstance(data, dict):
        candidates = data.get("sources", [])
    else:
        candidates = data
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        try:
            src = RecallSource(
                name=entry["name"],
                kind=entry.get("kind", entry["name"]),
                view=entry["view"],
                collection=entry.get("collection", entry["view"].split("_search", 1)[0]),
                text_fields=list(entry.get("text_fields", [])),
                result_fields=list(entry.get("result_fields", [])),
                limit=int(entry.get("limit", 5)),
                weight=float(entry.get("weight", 1.0)),
                scope_field=entry.get("scope_field"),
                tags_field=entry.get("tags_field"),
                include_edges=bool(entry.get("include_edges", False)),
                edge_limit=int(entry.get("edge_limit", 5)),
                formatter=entry.get("formatter"),
                global_access=bool(entry.get("global_access", False)),
                extra_edge_collections=entry.get("extra_edge_collections"),
            )
        except KeyError:
            continue
        sources.append(src)
    return sources


def load_supplemental_sources(db) -> List[RecallSource]:
    """Load supplemental recall sources (built-in + user-configured)."""

    sources: List[RecallSource] = []
    # Built-ins are only appended if the backing view exists.
    try:
        existing_views = {v.get("name") for v in db.views()}
    except Exception as exc:
        logger.error("load_supplemental_sources get failed: {exc}", exc=exc)
        existing_views = set()
    for src in builtin_sources():
        if src.view in existing_views:
            sources.append(src)
    raw_json = os.getenv("RECALL_SOURCES_JSON")
    config_path = os.getenv("RECALL_SOURCES_FILE")
    parsed: List[RecallSource] = []
    if raw_json:
        try:
            parsed.extend(_load_json_config(raw_json))
        except Exception as exc:
            logger.error("load_supplemental_sources caught error: {exc}", exc=exc)
    if config_path:
        try:
            text = Path(config_path).expanduser().read_text()
            parsed.extend(_load_json_config(text))
        except Exception as exc:
            logger.error("load_supplemental_sources caught error: {exc}", exc=exc)
    for src in parsed:
        if src.view not in existing_views:
            continue
        sources.append(src)
    return sources


def _build_search_expression(source: RecallSource) -> str:
    fields = source.text_fields or []
    if not fields:
        fields = ["body"]
    parts = [f"doc.{_safe(field)} IN TOKENS(@q, 'text_en')" for field in fields]
    return " OR ".join(parts)


def _build_filter_clause(source: RecallSource) -> str:
    clauses: List[str] = []
    if source.scope_field:
        clauses.append(f"(@scope == '' OR doc.{_safe(source.scope_field)} == @scope)")
    if source.tags_field:
        clauses.append(f"LENGTH(@tags) == 0 OR doc.{_safe(source.tags_field)} ANY IN @tags")
    if not clauses:
        return ""
    return "FILTER " + " AND ".join(clauses)


def _edge_block(include: bool, extra_edge_collections: Optional[List[str]] = None) -> str:
    if not include:
        return "LET edges = []"

    # Standard lesson_edges traversal
    block = """
      LET lesson_edge_hits = (
        FOR e IN lesson_edges
          FILTER e._from == doc._id OR e._to == doc._id
          SORT e.updated_at DESC
          LIMIT @edge_limit
          LET neighbor_id = e._from == doc._id ? e._to : e._from
          LET neighbor = DOCUMENT(neighbor_id)
          RETURN {
            type: e.type,
            stance: e.stance,
            weight: e.weight_llm,
            updated_at: e.updated_at,
            target: KEEP(neighbor, ['_key','title','scope','tags']),
            edge_collection: 'lesson_edges'
          }
      )
    """

    if extra_edge_collections:
        # Add traversal for each extra edge collection
        extra_lets = []
        union_names = ["lesson_edge_hits"]

        for i, coll in enumerate(extra_edge_collections):
            var_name = f"extra_edges_{i}"
            union_names.append(var_name)
            extra_lets.append(f"""
      LET {var_name} = (
        FOR e IN {coll}
          FILTER e._from == doc._id OR e._to == doc._id
          LIMIT @edge_limit
          LET neighbor_id = e._from == doc._id ? e._to : e._from
          LET neighbor = DOCUMENT(neighbor_id)
          RETURN {{
            type: e.edge_type,
            control_id: e.control_id,
            framework: e.framework,
            confidence: e.confidence,
            lean4_status: e.lean4_status,
            target: KEEP(neighbor, ['_key','control_id','name','description','source_framework','text']),
            edge_collection: '{coll}'
          }}
      )
    """)

        block += "\n".join(extra_lets)
        block += f"\n      LET edges = UNION({', '.join(union_names)})\n"
    else:
        block += "\n      LET edges = lesson_edge_hits\n"

    return block


def query_source(db, source: RecallSource, q: str, scope: str, tags: Optional[List[str]] = None, entities: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    src = sanitize_fields(source)
    # When a scope is requested but this source has no scope_field,
    # skip it entirely -- unscoped sources would contaminate scoped results.
    # Exception: global_access sources (e.g. datalake_chunks) are shared knowledge
    # accessible to all scopes including personas.
    if scope and not src.scope_field and not src.global_access:
        return []
    search_expr = _build_search_expression(src)
    filter_clause = _build_filter_clause(src)
    edge_block = _edge_block(src.include_edges, src.extra_edge_collections)

    # Entity-boosted search: when entities are provided and source has entity_field,
    # search for BOTH text matches AND entity matches (OR). This ensures
    # entity-matched results are always included alongside BM25 text matches.
    use_entity_search = bool(entities and src.entity_field)
    if use_entity_search:
        entity_f = _safe(src.entity_field)
        search_clause = f"""SEARCH (ANALYZER({search_expr}, 'text_en'))
          OR (ANALYZER(doc.{entity_f} IN @entities, 'identity'))"""
    else:
        search_clause = f"SEARCH ANALYZER({search_expr}, 'text_en')"

    # When entity search is active, sort entity-matched docs first, then by BM25
    if use_entity_search:
        entity_f = _safe(src.entity_field)
        sort_clause = f"SORT (doc.{entity_f} IN @entities ? 1 : 0) DESC, BM25(doc) DESC"
    else:
        sort_clause = "SORT BM25(doc) DESC"

    aql = f"""
    FOR doc IN {src.view}
      {search_clause}
      {filter_clause}
      {sort_clause}
      LIMIT @limit
      LET kept = MERGE(KEEP(doc, @fields), {{ _id: doc._id, _key: doc._key, embedding: doc.embedding }})
      {edge_block}
      RETURN {{ doc: kept, edges: edges, bm25: BM25(doc) }}
    """
    bind_vars: Dict[str, Any] = {
        "q": q,
        "limit": max(1, src.limit),
        "fields": list({"_id", "_key", *src.result_fields}),
    }
    # Only include scope/tags if the source uses them
    if src.scope_field:
        bind_vars["scope"] = scope or ""
    if src.tags_field:
        bind_vars["tags"] = tags or []
    if use_entity_search:
        bind_vars["entities"] = entities
    if src.include_edges:
        bind_vars["edge_limit"] = max(1, src.edge_limit)
    rows = list(db.aql.execute(aql, bind_vars=bind_vars))

    # ── Cosine rerank using inline embeddings ──────────────────────
    dense_scores = _cosine_rerank_inline(rows, q)

    out: List[Dict[str, Any]] = []
    for row in rows:
        doc = row.get("doc") or {}
        scores = doc.get("scores", {}) or {}
        scores["bm25"] = float(row.get("bm25", 0.0))
        key = doc.get("_key", "")
        scores["dense"] = dense_scores.get(key, 0.0)
        doc["scores"] = scores
        if row.get("edges"):
            doc["edges"] = row["edges"]
        formatted = apply_formatter(src, doc)
        if src.weight != 1.0:
            formatted.setdefault("scores", {})["weight"] = src.weight
        formatted["_source"] = src.name
        formatted["source"] = src.name
        formatted["type"] = src.kind
        out.append(formatted)

    # Re-sort by blended BM25 + dense score
    if dense_scores:
        bm25_w = 0.6
        dense_w = 0.4
        for item in out:
            s = item.get("scores", {})
            s["_blend"] = bm25_w * s.get("bm25", 0.0) + dense_w * s.get("dense", 0.0)
        out.sort(key=lambda x: x.get("scores", {}).get("_blend", 0.0), reverse=True)
        for item in out:
            item.get("scores", {}).pop("_blend", None)

    return out


def gather_supplemental_hits(
    db, q: str, scope: str, tags: Optional[List[str]] = None, collections: Optional[List[str]] = None,
    entities: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Gather hits from supplemental sources in parallel with timeout.

    Each source is queried concurrently. Sources that timeout or fail are
    silently skipped - recall returns whatever is available.

    Args:
        entities: Optional list of entity IDs (e.g. control IDs) to boost
                  in sources that have entity_field set.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

    all_sources = load_supplemental_sources(db)
    if not all_sources:
        return []

    # Filter sources if collections is provided
    if collections:
        sources = [s for s in all_sources if s.name in collections]
    else:
        sources = all_sources

    if not sources:
        return []

    timeout_sec = float(os.getenv("RECALL_SUPPLEMENTAL_TIMEOUT", "2.0"))
    max_workers = min(len(sources), int(os.getenv("RECALL_SUPPLEMENTAL_WORKERS", "4")))

    hits: List[Dict[str, Any]] = []

    def query_one(src: RecallSource) -> List[Dict[str, Any]]:
        try:
            return query_source(db, src, q, scope, tags or [], entities=entities)
        except Exception as exc:
            logger.warning("supplemental source query failed for {}: {}", src.name, exc)
            return []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_src = {executor.submit(query_one, src): src for src in sources}
        try:
            for future in as_completed(future_to_src, timeout=timeout_sec):
                src = future_to_src[future]
                try:
                    result = future.result(timeout=0.1)
                    hits.extend(result)
                except Exception as exc:
                    logger.warning("supplemental source result failed for {}: {}", src.name, exc)
                    continue
        except FuturesTimeoutError:
            logger.warning(
                "supplemental recall timeout after {:.2f}s (partial results returned)",
                timeout_sec,
            )

    return hits
