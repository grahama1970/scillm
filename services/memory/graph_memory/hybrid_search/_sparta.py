"""Hybrid search on SPARTA QRAs and skill descriptions."""
from typing import Any, Dict, List, Optional
from loguru import logger

from ._embedding import get_query_embedding
from ._graph import graph_traversal, expand_via_control_ids


def hybrid_search_sparta_qra(
    db,
    query: str,
    k: int = 10,
    bm25_weight: float = 0.4,
    vector_weight: float = 0.6,
    similarity_threshold: float = 0.3,
    include_graph: bool = True,
    graph_depth: int = 2,
    expand_via_qra_edges: bool = True,
    seed_control_ids: Optional[List[str]] = None,
    domain_filter: Optional[str] = None,
    persona_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Hybrid search on SPARTA QRAs using BM25 + vector similarity + graph.

    Returns QRAs with combined scores from BM25 (question/answer/reasoning)
    and dense vector similarity (via sparta_qra_embeddings collection).

    Args:
        expand_via_qra_edges: Expand via sparta_qra_edges (cross-persona edges)
        seed_control_ids: Additional control IDs for subgraph expansion
        domain_filter: Filter by domain (nist, do178c, stpa, ics_ot, formal)
        persona_filter: Filter by persona_scope
    """
    from concurrent.futures import ThreadPoolExecutor

    # Fire embedding HTTP request and collection check concurrently.
    # Embedding: ~30-100ms (HTTP), has_collection: ~1-3ms (ArangoDB metadata).
    # Both must complete before the combined AQL can run.
    with ThreadPoolExecutor(max_workers=2) as _pool:
        embed_future = _pool.submit(get_query_embedding, query)
        has_coll_future = _pool.submit(db.has_collection, "sparta_qra_embeddings")
        query_vector = embed_future.result()
        has_emb_collection = has_coll_future.result()

    has_embeddings = query_vector and has_emb_collection

    if has_embeddings:
        # Two-stage: BM25 top-N candidates → cosine rerank on embeddings.
        # Single server-side AQL — faster than two round-trips.
        aql = """
        LET bm25_candidates = (
            FOR doc IN sparta_unified_search
              SEARCH ANALYZER(
                doc.question IN TOKENS(@query, 'text_en') OR
                doc.answer IN TOKENS(@query, 'text_en') OR
                doc.reasoning IN TOKENS(@query, 'text_en') OR
                doc.name IN TOKENS(@query, 'text_en') OR
                doc.description IN TOKENS(@query, 'text_en') OR
                doc.text IN TOKENS(@query, 'text_en') OR
                doc.topic IN TOKENS(@query, 'text_en'),
                'text_en'
              )
              LET bm25 = BM25(doc)
              FILTER bm25 > 0
              SORT bm25 DESC
              LIMIT @pool
              RETURN {
                _key: doc._key, _id: doc._id,
                question: doc.question, answer: doc.answer,
                reasoning: doc.reasoning, control_id: doc.control_id,
                grounding_score: doc.grounding_score,
                reasoning_grade: doc.reasoning_grade,
                mind: (doc.mind != null ? doc.mind : doc.tactical_tags),
                lineage: doc.lineage,
                evidence_case: doc.evidence_case,
                bm25_score: bm25
              }
        )

        LET reranked = (
            FOR doc IN bm25_candidates
              LET emb = FIRST(
                FOR e IN sparta_qra_embeddings
                  FILTER e.qra_id == doc._id
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
            "query": query, "embedding": query_vector, "k": k,
            "pool": pool_size,
            "bm25_weight": bm25_weight, "vector_weight": vector_weight,
        }))
    else:
        # BM25 only (no embeddings available)
        aql = """
        FOR doc IN sparta_unified_search
        SEARCH ANALYZER(
            doc.question IN TOKENS(@query, 'text_en') OR
            doc.answer IN TOKENS(@query, 'text_en') OR
            doc.reasoning IN TOKENS(@query, 'text_en') OR
            doc.name IN TOKENS(@query, 'text_en') OR
            doc.description IN TOKENS(@query, 'text_en') OR
            doc.text IN TOKENS(@query, 'text_en') OR
            doc.topic IN TOKENS(@query, 'text_en'),
            'text_en'
        )
        LET bm25 = BM25(doc)
        SORT bm25 DESC
        LIMIT @k
        RETURN {
            _key: doc._key, _id: doc._id,
            question: doc.question, answer: doc.answer,
            reasoning: doc.reasoning, control_id: doc.control_id,
            grounding_score: doc.grounding_score,
            reasoning_grade: doc.reasoning_grade,
            mind: (doc.mind != null ? doc.mind : doc.tactical_tags),
            lineage: doc.lineage,
            evidence_case: doc.evidence_case,
            bm25_score: bm25, similarity_score: 0.0,
            score: bm25
        }
        """
        results = list(db.aql.execute(aql, bind_vars={"query": query, "k": k}))

    # Graph expansion via sparta_qra_edges (cross-persona relationships)
    if include_graph and expand_via_qra_edges and results and db.has_collection("sparta_qra_edges"):
        seen_keys = {r["_key"] for r in results}
        for r in results[:3]:
            try:
                edge_results = graph_traversal(
                    db, r["_id"],
                    edge_collection="sparta_qra_edges",
                    depth=graph_depth,
                    direction="ANY",
                    limit=5,
                )
                related = []
                for gr in edge_results:
                    if gr["_key"] not in seen_keys:
                        seen_keys.add(gr["_key"])
                        related.append(gr)
                if related:
                    r["related"] = related
            except Exception as exc:
                logger.error("Suppressed error in hybrid_search: {}", exc)

    # Subgraph expansion from seed control IDs (for brandon_simulacrum integration)
    if seed_control_ids and db.has_collection("sparta_qra_edges"):
        try:
            expanded = expand_via_control_ids(db, seed_control_ids, graph_depth, k)
            seen_keys = {r["_key"] for r in results}
            for er in expanded:
                if er["_key"] not in seen_keys:
                    er["_source"] = "sparta_qra"
                    er["_via_subgraph"] = True
                    results.append(er)
                    seen_keys.add(er["_key"])
        except Exception as e:
            logger.error(f"Subgraph expansion failed: {e}")

    # Apply domain/persona filters
    if domain_filter:
        results = [r for r in results if r.get("domain", "sparta") == domain_filter]
    if persona_filter:
        results = [r for r in results if r.get("persona_scope", "brandon_bailey") == persona_filter]

    for r in results:
        r["_source"] = "sparta_qra"

    return results[:k]


def hybrid_search_skill_descriptions(
    db,
    query: str,
    k: int = 5,
    bm25_weight: float = 0.4,
    vector_weight: float = 0.6,
    similarity_threshold: float = 0.3,
) -> List[Dict[str, Any]]:
    """Hybrid search on skill descriptions using BM25 + vector similarity.

    Surfaces existing skills that match a capability query — prevents
    agents from building parallel systems (anti-silo).
    """
    query_vector = get_query_embedding(query)
    has_embeddings = query_vector and db.has_collection("skill_description_embeddings")

    if has_embeddings:
        aql = """
        LET queryVector = @embedding

        LET bm25_results = (
            FOR doc IN skill_descriptions_search
            SEARCH ANALYZER(
                doc.name IN TOKENS(@query, 'text_en') OR
                doc.description IN TOKENS(@query, 'text_en') OR
                doc.provides IN TOKENS(@query, 'text_en') OR
                doc.triggers IN TOKENS(@query, 'text_en') OR
                doc.body IN TOKENS(@query, 'text_en'),
                'text_en'
            )
            LET bm25 = BM25(doc)
            FILTER bm25 > 0
            LIMIT @k * 2
            RETURN {
                _key: doc._key, _id: doc._id,
                name: doc.name, description: doc.description,
                provides: doc.provides, composes: doc.composes,
                triggers: doc.triggers, taxonomy: doc.taxonomy,
                skill_path: doc.skill_path,
                bm25_score: bm25, similarity_score: 0.0
            }
        )

        LET vector_results = (
            FOR emb IN skill_description_embeddings
            LET vec = emb.embedding
            FILTER vec != null
            LET similarity = COSINE_SIMILARITY(vec, queryVector)
            FILTER similarity >= @similarity_threshold
            LET skill = DOCUMENT(emb.skill_id)
            FILTER skill != null
            SORT similarity DESC
            LIMIT @k * 2
            RETURN {
                _key: skill._key, _id: skill._id,
                name: skill.name, description: skill.description,
                provides: skill.provides, composes: skill.composes,
                triggers: skill.triggers, taxonomy: skill.taxonomy,
                skill_path: skill.skill_path,
                bm25_score: 0.0, similarity_score: similarity
            }
        )

        LET merged = (
            FOR result IN UNION(bm25_results, vector_results)
            COLLECT key = result._key INTO group
            LET first = FIRST(group[*].result)
            LET max_bm25 = MAX(group[*].result.bm25_score)
            LET max_sim = MAX(group[*].result.similarity_score)
            LET norm_bm25 = MIN([max_bm25 / 20.0, 1.0])
            LET combined = (@bm25_weight * norm_bm25) + (@vector_weight * max_sim)
            RETURN MERGE(first, {
                bm25_score: max_bm25, similarity_score: max_sim, score: combined
            })
        )

        FOR result IN merged
        SORT result.score DESC
        LIMIT @k
        RETURN result
        """
        results = list(db.aql.execute(aql, bind_vars={
            "query": query, "embedding": query_vector, "k": k,
            "similarity_threshold": similarity_threshold,
            "bm25_weight": bm25_weight, "vector_weight": vector_weight,
        }))
    else:
        aql = """
        FOR doc IN skill_descriptions_search
        SEARCH ANALYZER(
            doc.name IN TOKENS(@query, 'text_en') OR
            doc.description IN TOKENS(@query, 'text_en') OR
            doc.provides IN TOKENS(@query, 'text_en') OR
            doc.triggers IN TOKENS(@query, 'text_en') OR
            doc.body IN TOKENS(@query, 'text_en'),
            'text_en'
        )
        LET bm25 = BM25(doc)
        SORT bm25 DESC
        LIMIT @k
        RETURN {
            _key: doc._key, _id: doc._id,
            name: doc.name, description: doc.description,
            provides: doc.provides, composes: doc.composes,
            triggers: doc.triggers, taxonomy: doc.taxonomy,
            skill_path: doc.skill_path,
            bm25_score: bm25, similarity_score: 0.0, score: bm25
        }
        """
        results = list(db.aql.execute(aql, bind_vars={"query": query, "k": k}))

    for r in results:
        r["_source"] = "skill_descriptions"

    return results
