"""Graph traversal and subgraph expansion."""
from typing import Any, Dict, List
from loguru import logger


def graph_traversal(
    db,
    start_id: str,
    edge_collection: str = "lesson_edges",
    depth: int = 2,
    direction: str = "OUTBOUND",
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Multi-hop graph traversal.

    Args:
        db: ArangoDB database connection
        start_id: Starting document ID (e.g., "lessons/abc123")
        edge_collection: Edge collection name
        depth: Maximum traversal depth
        direction: OUTBOUND, INBOUND, or ANY
        limit: Maximum results

    Returns:
        List of traversal paths with scores
    """
    aql = f"""
    FOR v, e, p IN 1..@depth {direction} @start {edge_collection}
    LIMIT @limit
    RETURN {{
        _key: v._key,
        _id: v._id,
        title: v.title,
        depth: LENGTH(p.edges),
        edge_type: LAST(p.edges).type,
        path_weight: SUM(p.edges[*].weight)
    }}
    """

    return list(db.aql.execute(aql, bind_vars={
        "start": start_id,
        "depth": depth,
        "limit": limit,
    }))


def expand_via_control_ids(
    db,
    seed_control_ids: List[str],
    depth: int = 2,
    k: int = 10,
) -> List[Dict[str, Any]]:
    """Expand QRA results by traversing sparta_qra_edges from seed control IDs.

    This replaces the embedded AQL in brandon_simulacrum.py's
    _expand_qras_via_subgraph() — all graph expansion goes through /memory.
    """
    # Step 1: Find QRA document IDs for seed control IDs
    start_aql = """
    FOR q IN sparta_qra
        FILTER q.control_id IN @cids
        FILTER q.grounding_score >= 0.7
        SORT q.grounding_score DESC
        COLLECT cid = q.control_id INTO grp
        LET best = FIRST(grp[*].q)
        RETURN {_id: best._id, _key: best._key, control_id: cid}
    """
    try:
        starts = list(db.aql.execute(start_aql, bind_vars={"cids": seed_control_ids}))
    except Exception as exc:
        logger.error("Suppressed error in hybrid_search: {}", exc)
        return []

    if not starts:
        return []

    # Step 2: Traverse edges from seed QRAs
    start_ids = [s["_id"] for s in starts]
    traverse_aql = f"""
    FOR start IN @starts
        FOR v, e, p IN 1..@depth ANY start sparta_qra_edges
            OPTIONS {{uniqueVertices: "global", bfs: true}}
            FILTER v._id NOT IN @starts
            LIMIT @limit
            RETURN {{
                _key: v._key, _id: v._id,
                question: v.question, answer: v.answer,
                control_id: v.control_id,
                grounding_score: v.grounding_score,
                domain: v.domain,
                persona_scope: v.persona_scope,
                edge_type: LAST(p.edges).edge_type,
                depth: LENGTH(p.edges),
                score: v.grounding_score * (1.0 / LENGTH(p.edges))
            }}
    """
    try:
        expanded = list(db.aql.execute(traverse_aql, bind_vars={
            "starts": start_ids,
            "depth": depth,
            "limit": k * 2,
        }))
    except Exception as exc:
        logger.error("Suppressed error in hybrid_search: {}", exc)
        return []

    # Deduplicate by _key, keeping highest score
    by_key: Dict[str, Dict] = {}
    for r in expanded:
        key = r["_key"]
        if key not in by_key or r.get("score", 0) > by_key[key].get("score", 0):
            by_key[key] = r

    results = sorted(by_key.values(), key=lambda x: x.get("score", 0), reverse=True)
    return results[:k]
