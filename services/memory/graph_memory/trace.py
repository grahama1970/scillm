"""Provenance tracing for retrieval chains.

Provides directed provenance graphs showing which QRAs, controls, documents,
and edges contributed to an answer. Three speed tiers:
- instant (~5ms): cached trace lookup
- fast (~200ms): BM25 recall + 1-hop graph, no LLM
- accurate (~3-5s): full recall + multi-hop BFS + LLM claim extraction

Inputs:
    - q: query text
    - answer: optional answer text to verify claims against
    - scope: scope filter for recall
    - mode: instant|fast|accurate
    - CLAIM_OVERLAP_THRESHOLD env var (default 0.4): word overlap threshold

Outputs:
    - Trace result dict with retrieval, verification, graph, evidence sections

Failure modes:
    - ArangoDB unavailable: raises from arango_client.get_db()
    - Cache miss on instant mode: falls through to fast
    - Dense scoring unavailable: degrades gracefully (logged)
    - Graph traversal failure: degrades gracefully (logged)

Dependencies:
    - arango_client (ArangoDB connection)
    - lessons.recall (bm25_rank, fuse_bm25_graph, graph_score_for_seed)
"""
from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from loguru import logger

# Control ID patterns (SPARTA, NIST, D3FEND, ISO, CWE)
_CONTROL_ID_RE = re.compile(
    r'\b(SV-[A-Z]{2,4}-\d+|'       # SPARTA: SV-AC-2
    r'[A-Z]{2}-\d+(?:\.\d+)*|'     # NIST: AC-2, AC-2.1
    r'd3f:\w+|'                     # D3FEND: d3f:Harden
    r'ISO\s*\d+-\d+|'              # ISO: ISO 27001-5
    r'CWE-\d+)\b',                  # CWE: CWE-89
    re.IGNORECASE,
)

# Configurable claim verification threshold (word overlap ratio)
_CLAIM_OVERLAP_THRESHOLD = float(os.getenv("CLAIM_OVERLAP_THRESHOLD", "0.4"))


def _extract_control_ids(text: str) -> list[str]:
    """Extract control IDs from text using regex (stable order)."""
    if not text:
        return []
    return sorted({cid.upper() for cid in _CONTROL_ID_RE.findall(text)})


def _normalize_node_id(raw_id: str) -> str:
    """Normalize graph node IDs to a stable 'collection:key' format."""
    if not raw_id:
        return raw_id
    if ":" in raw_id:
        return raw_id
    if "/" in raw_id:
        coll, key = raw_id.split("/", 1)
        if coll and key:
            return f"{coll}:{key}"
    return raw_id


def _cache_key(q: str, answer: str, scope: str, mode: str, k: int, depth: int, tags: list[str]) -> str:
    """SHA256 hash of trace parameters for cache lookup."""
    tag_key = ",".join(sorted(t.strip() for t in tags if t))
    raw = f"{q}|{answer}|{scope}|{mode}|{k}|{depth}|{tag_key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _check_cache(db: Any, key: str) -> dict[str, Any] | None:
    """Check trace_cache for a prior result. Returns None on miss or error."""
    try:
        if not db.has_collection("trace_cache"):
            return None
        rows = list(db.aql.execute(
            "FOR t IN trace_cache FILTER t.cache_key == @k LIMIT 1 RETURN t",
            bind_vars={"k": key},
        ))
        if rows:
            result = rows[0]
            for drop_key in ("_id", "_rev", "_key", "cache_key", "created_at"):
                result.pop(drop_key, None)
            result["cached"] = True
            return result
    except (ConnectionError, OSError) as exc:
        logger.warning("trace cache lookup failed (connectivity): {}", exc)
    except Exception as exc:
        logger.error("trace cache lookup failed: {}", exc)
    return None


def _store_cache(db: Any, key: str, result: dict[str, Any]) -> None:
    """Store trace result in cache with TTL. Best-effort, never raises."""
    try:
        if not db.has_collection("trace_cache"):
            return
        doc = dict(result)
        doc["cache_key"] = key
        doc["created_at"] = int(time.time())
        doc.pop("cached", None)
        db.collection("trace_cache").insert(doc)
    except (ConnectionError, OSError) as exc:
        logger.warning("trace cache store failed (connectivity): {}", exc)
    except Exception as exc:
        logger.error("trace cache store failed: {}", exc)


def _verify_claims_deterministic(
    answer: str,
    results: list[dict[str, Any]],
    control_ids: list[str],
) -> dict[str, Any]:
    """Deterministic citation verification via control ID matching + content overlap.

    Splits answer into sentence-level claims, then verifies each against
    retrieved results using (1) control ID string matching and (2) word overlap.
    """
    sentences = [s.strip() for s in re.split(r'[.!?]\s+', answer) if len(s.strip()) > 15]
    if not sentences:
        sentences = [answer.strip()] if answer.strip() else []

    details: list[dict[str, Any]] = []
    verified_count = 0

    # Build lookup: control_id -> result key
    cid_to_key: dict[str, str] = {}
    for r in results:
        cid = r.get("control_id") or ""
        if cid:
            cid_to_key[cid.upper()] = r.get("key", r.get("_key", ""))

    # Build content index: lowered title+solution text per result
    content_map: dict[str, set[str]] = {}
    for r in results:
        key = r.get("key", r.get("_key", ""))
        parts: list[str] = []
        for field in ("title", "solution", "answer", "content", "question"):
            v = r.get(field, "")
            if v:
                parts.append(str(v).lower())
        content_map[key] = set(" ".join(parts).split())

    threshold = _CLAIM_OVERLAP_THRESHOLD

    for sentence in sentences:
        claim: dict[str, Any] = {
            "claim_text": sentence, "source_key": None,
            "control_id": "", "verified": False, "method": "not_found",
        }

        # Method 1: control_id match
        ids_in_claim = _extract_control_ids(sentence)
        matched_by_id = False
        for cid in ids_in_claim:
            if cid.upper() in cid_to_key:
                claim["source_key"] = cid_to_key[cid.upper()]
                claim["control_id"] = cid
                claim["verified"] = True
                claim["method"] = "control_id_match"
                matched_by_id = True
                verified_count += 1
                break

        # Method 2: content overlap (word overlap > threshold)
        if not matched_by_id:
            claim_words = set(sentence.lower().split())
            if claim_words:
                best_overlap = 0.0
                best_key = None
                for key, content_words in content_map.items():
                    overlap = len(claim_words & content_words) / len(claim_words)
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_key = key
                if best_overlap > threshold and best_key:
                    claim["source_key"] = best_key
                    claim["verified"] = True
                    claim["method"] = "content_overlap"
                    verified_count += 1

        details.append(claim)

    return {
        "claims_total": len(details),
        "claims_verified": verified_count,
        "claims_unverified": len(details) - verified_count,
        "details": details,
    }


def _build_graph(
    query: str,
    results: list[dict[str, Any]],
    verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build force-graph compatible node/link structure."""
    nodes: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    seen_ids: dict[str, int] = {}  # id -> index in nodes list
    key_to_node_id: dict[str, str] = {}

    def _add_node(nid: str, group: int, label: str, ntype: str) -> None:
        if nid not in seen_ids:
            seen_ids[nid] = len(nodes)
            nodes.append({"id": nid, "group": group, "label": label, "type": ntype})

    # Query node
    q_id = "query:0"
    _add_node(q_id, 0, query[:60], "query")

    # Result nodes + links to query
    for i, r in enumerate(results):
        key = r.get("key", r.get("_key", f"r{i}"))
        node_id = _normalize_node_id(f"{r.get('collection', 'lessons')}/{key}")
        label = r.get("title", r.get("question", key))[:60]
        _add_node(node_id, 1, label, "qra")
        key_to_node_id[str(key)] = node_id

        scores = r.get("scores", {})
        weight = max(scores.get("bm25", 0), scores.get("dense", 0), scores.get("graph", 0), 0.1)
        links.append({"source": q_id, "target": node_id, "type": r.get("label", "retrieval"), "weight": round(weight, 3)})

        # Control nodes
        cid = r.get("control_id", "")
        if cid:
            ctrl_id = f"control:{cid}"
            _add_node(ctrl_id, 2, cid, "control")
            links.append({"source": node_id, "target": ctrl_id, "type": "maps_to", "weight": 0.9})

        # Graph path edges (from accurate mode)
        for edge in r.get("graph_path", []):
            raw_from = edge.get("from", "")
            raw_to = edge.get("to", "")
            from_id = _normalize_node_id(raw_from)
            to_id = _normalize_node_id(raw_to)
            if from_id and to_id:
                from_label = from_id.split(":", 1)[-1]
                to_label = to_id.split(":", 1)[-1]
                _add_node(from_id, 3, from_label, "qra")
                _add_node(to_id, 3, to_label, "qra")
                links.append({
                    "source": from_id, "target": to_id,
                    "type": edge.get("type", "related"),
                    "weight": round(edge.get("weight", 0.5), 3),
                })

    # Claim nodes (if verification provided)
    if verification:
        for j, detail in enumerate(verification.get("details", [])):
            claim_id = f"claim:{j}"
            _add_node(claim_id, 4, detail["claim_text"][:40], "claim")
            source_key = detail.get("source_key")
            if source_key:
                source_node = key_to_node_id.get(str(source_key))
                if source_node:
                    links.append({
                        "source": source_node, "target": claim_id,
                        "type": "supports" if detail["verified"] else "unverified",
                        "weight": 0.8 if detail["verified"] else 0.2,
                    })

    return {"nodes": nodes, "links": links}


def _compute_evidence_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute evidence richness metrics."""
    if not results:
        return {"richness_score": 0.0, "framework_coverage": {}, "taxonomy_overlap": 0.0}

    fw_counts: dict[str, int] = {}
    all_tags: list[str] = []
    for r in results:
        cid = r.get("control_id", "")
        if cid:
            upper = cid.upper()
            if upper.startswith("SV-"):
                fw_counts["SPARTA"] = fw_counts.get("SPARTA", 0) + 1
            elif re.match(r'^[A-Z]{2}-\d+', upper):
                fw_counts["NIST"] = fw_counts.get("NIST", 0) + 1
            elif upper.startswith("D3F:"):
                fw_counts["D3FEND"] = fw_counts.get("D3FEND", 0) + 1
            elif upper.startswith("ISO"):
                fw_counts["ISO"] = fw_counts.get("ISO", 0) + 1
            elif upper.startswith("CWE-"):
                fw_counts["CWE"] = fw_counts.get("CWE", 0) + 1
        all_tags.extend(r.get("tags", []))

    score_sum = 0.0
    score_count = 0
    for r in results:
        for v in r.get("scores", {}).values():
            if isinstance(v, (int, float)) and v > 0:
                score_sum += float(v)
                score_count += 1
    richness = (score_sum / score_count) if score_count > 0 else 0.0

    unique_tags = set(all_tags)
    tax_overlap = len(unique_tags) / max(1, len(all_tags))

    return {
        "richness_score": round(richness, 3),
        "framework_coverage": fw_counts,
        "taxonomy_overlap": round(tax_overlap, 3),
    }


def _extract_taxonomy_tags(q: str) -> list[str]:
    """Best-effort taxonomy tag extraction for trace queries.

    Mirrors the tag extraction used by the main recall pipeline so that
    trace verifies against the same boosted result set.
    """
    try:
        from .lessons.store import extract_bridges_fast
        return extract_bridges_fast(q)
    except Exception as exc:
        logger.error("taxonomy extraction failed: {}", exc)
    return []


def _do_recall(db: Any, q: str, scope: str, tags: list[str], k: int, depth: int, mode: str) -> tuple[
    list[dict[str, Any]], list[str], dict[str, float],
]:
    """Execute recall phase. Returns (fused_results, lanes_used, dense_scores)."""
    from .lessons.recall import bm25_rank, fuse_bm25_graph

    bm25 = bm25_rank(db, q=q, scope=scope, tags=tags, k=max(k, 20))
    if mode == "fast":
        graph_depth = 1
    else:
        graph_depth = max(1, min(4, depth))
    fused = fuse_bm25_graph(db, bm25=bm25, depth=graph_depth, k=k) if bm25 else []

    lanes_used = ["bm25"]
    dense_scores: dict[str, float] = {}

    if True:  # Semantic search is ALWAYS on — not gated by mode
        try:
            from .lessons.recall import _maybe_dense_scores
            dense_scores = _maybe_dense_scores(db, lessons=fused, q=q, k=k)
            if dense_scores:
                lanes_used.append("dense")
        except ImportError:
            logger.warning("dense scoring unavailable: sentence-transformers not installed")
        except (ConnectionError, OSError) as exc:
            logger.warning("dense scoring failed (connectivity): {}", exc)
        except Exception as exc:
            logger.warning("dense scoring failed: {}", exc)

    if mode in ("fast", "accurate"):
        lanes_used.append("graph")

    if bm25:
        n = len(bm25)
        bm25_rr = {
            r.get("_key"): (n - idx) / max(1, n - 1)
            for idx, r in enumerate(bm25)
            if r.get("_key")
        }
        for r in fused:
            key = r.get("_key")
            if not key:
                continue
            scores = dict(r.get("scores", {}) or {})
            scores.setdefault("bm25", float(bm25_rr.get(key, 0.0)))
            r["scores"] = scores

    return fused, lanes_used, dense_scores


def _score_result(
    db: Any, r: dict[str, Any], dense_scores: dict[str, float],
    use_graph: bool, depth: int, control_ids: list[str],
) -> dict[str, Any]:
    """Score a single retrieval result, optionally with graph path reconstruction."""
    from .lessons.recall import graph_score_for_seed

    key = r.get("_key", "")
    seed_id = f"lessons/{key}"

    scores = dict(r.get("scores", {})) or {"bm25": 0.0, "dense": 0.0, "graph": 0.0, "freshness": 0.0}
    if dense_scores:
        scores["dense"] = float(dense_scores.get(str(key), 0.0))

    graph_path: list[dict[str, Any]] = []
    graph_hops = 0
    if use_graph:
        try:
            score, paths = graph_score_for_seed(db, seed_id, depth=depth, return_paths=True)
            scores["graph"] = float(score)
            graph_hops = len(paths[0]) if paths else 0
            for path_edges in paths[:1]:
                for edge in path_edges:
                    graph_path.append({
                        "from": edge.get("_from", ""),
                        "to": edge.get("_to", ""),
                        "type": str(edge.get("type", "related")),
                        "weight": round(float(edge.get("weight", 0)), 3),
                    })
        except (ConnectionError, OSError) as exc:
            logger.warning("graph scoring failed for {}: {}", seed_id, exc)
        except Exception as exc:
            logger.error("graph scoring failed for {}: {}", seed_id, exc)

    # Label classification
    cid = r.get("control_id", "")
    if not cid:
        title_ids = _extract_control_ids(r.get("title", ""))
        cid = title_ids[0] if title_ids else ""

    if cid and any(c.upper() == cid.upper() for c in control_ids):
        label = "DIRECT"
    elif graph_hops > 0:
        label = "GRAPH_INFERRED"
    else:
        label = "SEMANTIC_MATCH"

    item: dict[str, Any] = {
        "key": key,
        "collection": r.get("_source", "lessons"),
        "control_id": cid,
        "title": r.get("title", ""),
        "scores": {sk: round(sv, 4) for sk, sv in scores.items() if isinstance(sv, (int, float))},
        "graph_hops": graph_hops,
        "label": label,
    }
    if graph_path:
        item["graph_path"] = graph_path
    return item


def trace(
    q: str,
    answer: str = "",
    scope: str = "",
    mode: str = "fast",
    k: int = 10,
    depth: int = 3,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Trace provenance for a query and optional answer.

    Returns directed provenance graph showing which documents,
    controls, and edges contributed to (or should have contributed
    to) the answer.

    Args:
        q: Query text
        answer: Optional answer text to verify claims against
        scope: Scope filter for recall
        mode: instant|fast|accurate
        k: Max retrieval results
        depth: Graph traversal depth (accurate mode)
        tags: Pre-extracted taxonomy tags (extracted automatically if None)

    Returns:
        dict with keys: trace_id, timestamp, mode, query, retrieval,
        graph, evidence, cached, took_ms, and optionally verification.
    """
    from .arango_client import get_db

    t0 = time.time()
    db = get_db()

    # Extract entities from query
    control_ids = _extract_control_ids(q)
    if answer:
        control_ids = sorted(set(control_ids + _extract_control_ids(answer)))

    # Extract taxonomy tags (same as main recall pipeline) unless pre-provided
    recall_tags = tags if tags is not None else _extract_taxonomy_tags(q)

    # Cache check (include k/depth/tags to avoid mismatches)
    if mode == "instant":
        instant_candidates = [
            ("accurate", max(1, min(4, depth))),
            ("fast", 1),
        ]
        for cand_mode, cand_depth in instant_candidates:
            ck = _cache_key(q, answer, scope, cand_mode, k, cand_depth, recall_tags)
            cached = _check_cache(db, ck)
            if cached:
                return cached
        mode = "fast"

    effective_depth = 1 if mode == "fast" else max(1, min(4, depth))
    ck = _cache_key(q, answer, scope, mode, k, effective_depth, recall_tags)
    if mode in ("fast", "accurate"):
        cached = _check_cache(db, ck)
        if cached:
            return cached

    # Recall
    use_graph = mode in ("fast", "accurate")
    fused, lanes_used, dense_scores = _do_recall(db, q, scope, recall_tags, k, effective_depth, mode)

    # Score each result
    retrieval_results = [
        _score_result(db, r, dense_scores, use_graph, effective_depth, control_ids)
        for r in fused[:k]
    ]

    # Verification (when answer provided)
    verification = None
    if answer.strip():
        verification = _verify_claims_deterministic(answer, retrieval_results, control_ids)

    # Build graph
    graph = _build_graph(q, retrieval_results, verification)

    # Evidence metrics
    evidence = _compute_evidence_metrics(retrieval_results)

    # Assemble output
    took_ms = int((time.time() - t0) * 1000)
    result: dict[str, Any] = {
        "trace_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "query": {
            "text": q,
            "scope": scope,
            "entities": control_ids,
            "bridges": recall_tags,
        },
        "retrieval": {
            "lanes_used": lanes_used,
            "results": retrieval_results,
        },
        "graph": graph,
        "evidence": evidence,
        "cached": False,
        "took_ms": took_ms,
    }
    if answer.strip():
        result["query"]["answer"] = answer
    if verification:
        result["verification"] = verification

    _store_cache(db, ck, result)
    return result
