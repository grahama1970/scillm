"""Hybrid search on lessons and lean theorems."""
from typing import Any, Dict, List
from loguru import logger

from ._embedding import get_query_embedding
from ._graph import graph_traversal


def hybrid_search_lessons(
    db,
    query: str,
    scope: str = "",
    k: int = 10,
    bm25_weight: float = 0.4,
    vector_weight: float = 0.6,
    bm25_threshold: float = 0.0,
    similarity_threshold: float = 0.3,
) -> List[Dict[str, Any]]:
    """Hybrid search on lessons using BM25 + vector similarity.

    Args:
        db: ArangoDB database connection
        query: Search query text
        scope: Optional scope filter
        k: Number of results
        bm25_weight: Weight for BM25 scores (0-1)
        vector_weight: Weight for vector similarity (0-1)
        bm25_threshold: Minimum BM25 score
        similarity_threshold: Minimum cosine similarity

    Returns:
        List of results with combined scores
    """
    # Get query embedding
    query_vector = get_query_embedding(query)

    if query_vector and db.has_collection("lesson_embeddings"):
        # Two-stage: BM25 top-N candidates → cosine rerank on embeddings.
        # Avoids brute-force scan of 300K+ embeddings.
        aql = """
        LET bm25_candidates = (
            FOR doc IN unified_search
              SEARCH ANALYZER(
                doc.title IN TOKENS(@query, 'text_en') OR
                doc.problem IN TOKENS(@query, 'text_en') OR
                doc.playbook IN TOKENS(@query, 'text_en'),
                'text_en'
              )
              FILTER @scope == "" OR doc.scope == @scope
              LET bm25 = BM25(doc)
              FILTER bm25 >= @bm25_threshold
              SORT bm25 DESC
              LIMIT @pool
              RETURN {
                _key: doc._key, _id: doc._id,
                title: doc.title, problem: doc.problem,
                scope: doc.scope, bm25_score: bm25
              }
        )

        LET reranked = (
            FOR doc IN bm25_candidates
              LET emb = FIRST(
                FOR e IN lesson_embeddings
                  FILTER e.lesson_id == doc._id
                  LIMIT 1
                  RETURN e.embedding
              )
              LET sim = emb != null ? COSINE_SIMILARITY(emb, @embedding) : 0.0
              LET norm_bm25 = MIN([doc.bm25_score / 20.0, 1.0])
              LET combined = (@bm25_weight * norm_bm25) + (@vector_weight * sim)
              RETURN MERGE(doc, {
                similarity_score: sim,
                score: combined
              })
        )

        FOR r IN reranked
          SORT r.score DESC
          LIMIT @k
          RETURN r
        """

        pool_size = max(k * 5, 100)
        results = list(db.aql.execute(aql, bind_vars={
            "query": query,
            "embedding": query_vector,
            "scope": scope,
            "k": k,
            "pool": pool_size,
            "bm25_threshold": bm25_threshold,
            "bm25_weight": bm25_weight,
            "vector_weight": vector_weight,
        }))
    else:
        # BM25 only (no embeddings available)
        aql = """
        FOR doc IN unified_search
        SEARCH ANALYZER(
            doc.title IN TOKENS(@query, 'text_en') OR
            doc.problem IN TOKENS(@query, 'text_en') OR
            doc.playbook IN TOKENS(@query, 'text_en'),
            'text_en'
        )
        FILTER @scope == "" OR doc.scope == @scope
        LET bm25 = BM25(doc)
        SORT bm25 DESC
        LIMIT @k
        RETURN {
            _key: doc._key,
            _id: doc._id,
            title: doc.title,
            problem: doc.problem,
            scope: doc.scope,
            bm25_score: bm25,
            similarity_score: 0.0,
            score: bm25
        }
        """
        results = list(db.aql.execute(aql, bind_vars={
            "query": query,
            "scope": scope,
            "k": k,
        }))

    return results


def hybrid_search_lean_theorems(
    db,
    query: str,
    scope: str = "",
    k: int = 10,
    bm25_weight: float = 0.4,
    vector_weight: float = 0.4,
    graph_weight: float = 0.2,
    bm25_threshold: float = 0.0,
    similarity_threshold: float = 0.3,
    include_graph: bool = True,
    graph_depth: int = 2,
) -> List[Dict[str, Any]]:
    """Hybrid search on lean_theorems using BM25 + vector similarity + graph traversal.

    Args:
        db: ArangoDB database connection
        query: Search query text (theorem statement or requirement)
        scope: Optional scope filter
        k: Number of results
        bm25_weight: Weight for BM25 scores (0-1)
        vector_weight: Weight for vector similarity (0-1)
        graph_weight: Weight for graph-discovered results (0-1)
        bm25_threshold: Minimum BM25 score
        similarity_threshold: Minimum cosine similarity
        include_graph: Whether to expand via graph traversal
        graph_depth: Depth for graph expansion

    Returns:
        List of results with combined scores, deduped by _key
    """
    # Get query embedding
    query_vector = get_query_embedding(query)

    results_by_key: Dict[str, Dict[str, Any]] = {}

    # 1. BM25 search on lean_theorems_search view
    views = [v["name"] for v in db.views()]
    if "lean_theorems_search" in views:
        bm25_aql = """
        FOR doc IN lean_theorems_search
        SEARCH ANALYZER(
            doc.formal_statement IN TOKENS(@query, 'text_en') OR
            doc.requirement IN TOKENS(@query, 'text_en') OR
            doc.goal IN TOKENS(@query, 'text_en'),
            'text_en'
        )
        FILTER doc.status IN ["proven", "ok"]
        LET bm25 = BM25(doc)
        FILTER bm25 >= @bm25_threshold
        SORT bm25 DESC
        LIMIT @k * 2
        RETURN {
            _key: doc._key,
            _id: doc._id,
            formal_statement: doc.formal_statement,
            formal_proof: doc.formal_proof,
            header: doc.header,
            tactics: doc.tactics,
            source: doc.source,
            bm25_score: bm25
        }
        """
        try:
            bm25_results = list(db.aql.execute(bm25_aql, bind_vars={
                "query": query,
                "k": k,
                "bm25_threshold": bm25_threshold,
            }))
            for r in bm25_results:
                key = r["_key"]
                if key not in results_by_key:
                    results_by_key[key] = {
                        **r,
                        "bm25_score": r.get("bm25_score", 0),
                        "similarity_score": 0.0,
                        "graph_score": 0.0,
                        "_source": "lean_theorems",
                    }
                else:
                    results_by_key[key]["bm25_score"] = max(
                        results_by_key[key]["bm25_score"],
                        r.get("bm25_score", 0)
                    )
        except Exception as exc:
            logger.error("Suppressed error in hybrid_search: {}", exc)

    # 2. Vector similarity search via lesson_embeddings
    # Supports both V1 (key: "lean_theorems_{key}", field: "vector")
    # and V2 (key: "{key}", field: "embedding") formats
    if query_vector and db.has_collection("lesson_embeddings"):
        vector_aql = """
        FOR emb IN lesson_embeddings
        FILTER emb.source == "lean_theorem" OR STARTS_WITH(emb._key, "lean_theorems_")
        LET vec = emb.embedding
        FILTER vec != null
        LET similarity = COSINE_SIMILARITY(vec, @embedding)
        FILTER similarity >= @similarity_threshold
        LET thm_key = STARTS_WITH(emb._key, "lean_theorems_")
            ? SUBSTRING(emb._key, 14)
            : emb._key
        LET theorem = DOCUMENT(CONCAT("lean_theorems/", thm_key))
        FILTER theorem != null
        FILTER theorem.status IN ["proven", "ok"]
        SORT similarity DESC
        LIMIT @k * 2
        RETURN {
            _key: theorem._key,
            _id: theorem._id,
            formal_statement: theorem.formal_statement,
            formal_proof: theorem.formal_proof,
            header: theorem.header,
            tactics: theorem.tactics,
            source: theorem.source,
            similarity_score: similarity
        }
        """
        try:
            vector_results = list(db.aql.execute(vector_aql, bind_vars={
                "embedding": query_vector,
                "k": k,
                "similarity_threshold": similarity_threshold,
            }))
            for r in vector_results:
                key = r["_key"]
                if key not in results_by_key:
                    results_by_key[key] = {
                        **r,
                        "bm25_score": 0.0,
                        "similarity_score": r.get("similarity_score", 0),
                        "graph_score": 0.0,
                        "_source": "lean_theorems",
                    }
                else:
                    results_by_key[key]["similarity_score"] = max(
                        results_by_key[key]["similarity_score"],
                        r.get("similarity_score", 0)
                    )
        except Exception as e:
            logger.warning(f"Vector search failed: {e}")

    # 3. Graph expansion from top BM25/vector hits
    if include_graph and results_by_key and db.has_collection("lesson_edges"):
        # Get top 3 hits for graph expansion
        top_keys = sorted(
            results_by_key.keys(),
            key=lambda k: results_by_key[k]["bm25_score"] + results_by_key[k]["similarity_score"],
            reverse=True
        )[:3]

        for start_key in top_keys:
            start_id = f"lean_theorems/{start_key}"
            graph_aql = """
            FOR v, e, p IN 1..@depth ANY @start lesson_edges
            FILTER v._id LIKE "lean_theorems/%"
            FILTER v.status IN ["proven", "ok"]
            LET path_weight = SUM(p.edges[*].weight) / LENGTH(p.edges)
            LIMIT @limit
            RETURN {
                _key: v._key,
                _id: v._id,
                formal_statement: v.formal_statement,
                formal_proof: v.formal_proof,
                header: v.header,
                tactics: v.tactics,
                source: v.source,
                edge_type: LAST(p.edges).type,
                depth: LENGTH(p.edges),
                graph_score: path_weight / LENGTH(p.edges)
            }
            """
            try:
                graph_results = list(db.aql.execute(graph_aql, bind_vars={
                    "start": start_id,
                    "depth": graph_depth,
                    "limit": 5,
                }))
                for r in graph_results:
                    key = r["_key"]
                    if key not in results_by_key:
                        results_by_key[key] = {
                            **r,
                            "bm25_score": 0.0,
                            "similarity_score": 0.0,
                            "graph_score": r.get("graph_score", 0.5),
                            "_source": "lean_theorems",
                            "_via_graph": True,
                        }
                    else:
                        results_by_key[key]["graph_score"] = max(
                            results_by_key[key].get("graph_score", 0),
                            r.get("graph_score", 0.5)
                        )
            except Exception as exc:
                logger.error("Suppressed error in hybrid_search: {}", exc)

    # 4. Compute combined scores and sort
    results = []
    for key, r in results_by_key.items():
        # Normalize BM25 to 0-1 (assuming max ~20)
        norm_bm25 = min(r["bm25_score"] / 20.0, 1.0)
        combined = (
            bm25_weight * norm_bm25 +
            vector_weight * r["similarity_score"] +
            graph_weight * r.get("graph_score", 0)
        )
        r["score"] = combined
        r["scores"] = {
            "bm25": r["bm25_score"],
            "dense": r["similarity_score"],
            "graph": r.get("graph_score", 0),
            "combined": combined,
        }
        results.append(r)

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:k]
