"""Unified hybrid search across all collections."""
from typing import Any, Dict, List
from loguru import logger

from ._bm25 import bm25_search_collection, search_code_symbols
from ._graph import graph_traversal
from ._lessons import hybrid_search_lessons, hybrid_search_lean_theorems
from ._sparta import hybrid_search_sparta_qra, hybrid_search_skill_descriptions


def unified_hybrid_search(
    db,
    query: str,
    scope: str = "",
    k: int = 10,
    sources: str = "all",
    include_graph: bool = False,
    graph_depth: int = 2,
) -> Dict[str, Any]:
    """Unified hybrid search across all collections.

    Combines:
    - lessons: BM25 + vector similarity + optional graph expansion
    - code_symbols: BM25
    - doc_chunks: BM25
    - lean_theorems: BM25
    - sparta_qra: BM25 + vector + graph
    - skill_descriptions: BM25 + vector

    Args:
        db: ArangoDB database connection
        query: Search query text
        scope: Optional scope filter
        k: Number of results per source
        sources: "all", "lessons", "code", or "theorems"
        include_graph: Whether to expand results via graph traversal
        graph_depth: Depth for graph expansion

    Returns:
        Unified results with source attribution
    """
    results = []
    errors = []

    # Lessons: hybrid BM25 + vector
    if sources in ["all", "lessons"]:
        try:
            lesson_results = hybrid_search_lessons(db, query, scope, k)
            for r in lesson_results:
                r["_source"] = "lessons"
            results.extend(lesson_results)

            # Optional graph expansion
            if include_graph and lesson_results:
                top_ids = [f"lessons/{r['_key']}" for r in lesson_results[:3]]
                for start_id in top_ids:
                    try:
                        graph_results = graph_traversal(db, start_id, depth=graph_depth, limit=5)
                        for gr in graph_results:
                            gr["_source"] = "graph"
                            gr["score"] = 0.5 / gr.get("depth", 1)  # Decay by depth
                        results.extend(graph_results)
                    except Exception as exc:
                        logger.error("Suppressed error in hybrid_search: {}", exc)
        except Exception as e:
            errors.append(f"lessons: {e}")

    # Code symbols: identity analyzer for underscore-delimited names
    if sources in ["all", "code"]:
        try:
            views = [v["name"] for v in db.views()]
            if "code_symbols_search" in views:
                code_results = search_code_symbols(db, query, scope, k)
                for r in code_results:
                    r["_source"] = "code_symbols"
                results.extend(code_results)
        except Exception as e:
            errors.append(f"code_symbols: {e}")

    # Doc chunks: BM25 (P2 ingest uses 'content' and 'source_path' fields)
    if sources in ["all", "code", "docs"]:
        try:
            views = [v["name"] for v in db.views()]
            if "doc_chunks_search" in views:
                doc_results = bm25_search_collection(
                    db,
                    "doc_chunks_search",
                    query,
                    scope,
                    k,
                    search_fields=["content"],
                    return_fields={
                        "doc._key": "_key",
                        "doc.source_path": "file",
                        "LEFT(doc.content, 300)": "text",
                    },
                )
                for r in doc_results:
                    r["_source"] = "doc_chunks"
                results.extend(doc_results)
        except Exception as e:
            errors.append(f"doc_chunks: {e}")

    # Lean theorems: Full hybrid (BM25 + vector + graph)
    if sources in ["all", "theorems"]:
        try:
            views = [v["name"] for v in db.views()]
            if "lean_theorems_search" in views:
                theorem_results = hybrid_search_lean_theorems(
                    db,
                    query,
                    scope,
                    k,
                    include_graph=include_graph,
                    graph_depth=graph_depth,
                )
                results.extend(theorem_results)
        except Exception as e:
            errors.append(f"lean_theorems: {e}")

    # SPARTA QRAs: Full hybrid (BM25 + vector + graph)
    if sources in ["all", "sparta", "qra"]:
        try:
            views = [v["name"] for v in db.views()]
            if "sparta_unified_search" in views:
                qra_results = hybrid_search_sparta_qra(
                    db, query, k,
                    include_graph=include_graph,
                    graph_depth=graph_depth,
                )
                results.extend(qra_results)
        except Exception as e:
            errors.append(f"sparta_qra: {e}")

    # Skill descriptions: Hybrid (BM25 + vector) — anti-silo capability detection
    if sources in ["all", "skills"]:
        try:
            views = [v["name"] for v in db.views()]
            if "skill_descriptions_search" in views:
                skill_results = hybrid_search_skill_descriptions(db, query, min(k, 5))
                results.extend(skill_results)
        except Exception as e:
            errors.append(f"skill_descriptions: {e}")

    # Sort by score and limit
    results.sort(key=lambda x: float(x.get("score", 0)), reverse=True)
    results = results[:k]

    return {
        "meta": {"query": query, "scope": scope, "sources": sources, "count": len(results)},
        "items": results,
        "errors": errors,
    }
