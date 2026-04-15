from __future__ import annotations
from typing import List, Dict, Any
import time
import os

from .arango_client import get_db
from .lessons.recall import (
    bm25_rank,
    fuse_bm25_graph,
    get_last_graph_scores,
    _maybe_dense_scores,
    _expand_queries,
    graph_score_for_seed,
)
from .lessons.recall_sources import gather_supplemental_hits
from .lessons.store import get_mind_tags, get_heart_tags, VALID_MIND_TAGS, VALID_HEART_TAGS
from .lessons.residue import fetch_day_residue
import os as _os
from .events import log_event
from .lessons.arxiv_ingest import _fetch_arxiv

from loguru import logger


# Fields to strip from recall results (waste tokens, no value to LLM/human)
_STRIP_FIELDS = {"embedding", "embedding_visual", "embedding_2"}


def _strip_embeddings(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove embedding vectors from items to save tokens in API responses.
    
    Embeddings are 384-2048 floats each, ~3000-16000 tokens per item.
    They have zero value in chat/API responses - only useful for search internals.
    """
    for item in items:
        for field in _STRIP_FIELDS:
            if field in item:
                del item[field]
    return items


def _parse_updated_at(val) -> int:
    """Safely parse updated_at which may be int epoch or ISO string."""
    if not val:
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        try:
            return int(val)
        except ValueError:
            pass
        # ISO format: parse to epoch
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(val)
            return int(dt.timestamp())
        except Exception as exc:  # was bare
            return 0
    return 0

# ---------------------------------------------------------------------------
# Re-export peer module symbols so ``from graph_memory.api import X`` still
# works for every public name that previously lived here.
# ---------------------------------------------------------------------------
from .api_messaging import (  # noqa: F401
    add_message,
    list_messages,
    ack_message,
)
from .api_personas import (  # noqa: F401
    TOM_DEFAULT_TRUST,
    TOM_DEFAULT_RESPECT,
    TOM_HISTORY_TTL_DAYS,
    TOM_VALID_MOODS,
    TOM_VALID_SKILLS,
    _validate_mood,
    _validate_skill,
    get_or_create_user,
    update_user_profile,
    get_user_history,
    get_or_create_relationship,
    update_relationship,
    record_key_moment,
)
from .api_bdi import (  # noqa: F401
    get_or_create_persona_state,
    update_persona_state,
    get_persona_state_trend,
    infer_user_bdi,
    update_user_beliefs,
    set_user_belief,
    decay_belief_confidence,
)
from .api_scripts import (  # noqa: F401
    _extract_symbols_treesitter,
    _generate_script_embedding,
    learn_script,
    verify_script,
    list_scripts,
    search_scripts,
    deprecate_script,
    contradict_script,
    record_script_usage,
    trace_provenance,
)
from .api_client import (  # noqa: F401
    MemoryClient,
    record_assessment,
    _get_steering_skill,
    recall_with_steering,
)


BRIDGE_TAGS = {"Precision", "Resilience", "Fragility", "Corruption", "Loyalty", "Stealth"}


def _split_bridge_and_tags(tags: List[str] | None) -> tuple[List[str], List[str]]:
    """Split canonical bridge attributes out of free-form tags."""
    raw = [str(t) for t in (tags or []) if str(t).strip()]
    bridge = [t for t in raw if t in BRIDGE_TAGS]
    non_bridge = [t for t in raw if t not in BRIDGE_TAGS]
    return non_bridge, bridge


def search(q: str, scope: str = "", k: int = 5, collections: List[str] | None = None, tags: List[str] | None = None, entities: List[str] | None = None, crosswalk_methods: List[str] | None = None) -> Dict[str, Any]:
    db = get_db()
    t0 = time.time()
    thread_id = os.getenv('THREAD_ID', '')
    # multi-query expansion (soft)
    queries = _expand_queries(q, tags or []) or [q]
    cand: Dict[str, Dict[str, Any]] = {}
    seed_k = max(k, 3) if _os.getenv('RERANKER_GUIDED') in ('1','true','TRUE') else k
    search_errors: List[str] = []
    # BM25 search — collections filter routes to specific views only
    for qi in queries:
        for r in bm25_rank(db, q=qi, scope=scope, tags=tags or [], k=seed_k, collections=collections):
            cand[r["_key"]] = r
    bm25 = list(cand.values()) or bm25_rank(db, q=q, scope=scope, tags=tags or [], k=seed_k, collections=collections)

    # Extract query bridge attributes for fusion boost
    query_bridges = []
    try:
        from .lessons.store import extract_bridges_fast
        query_bridges = extract_bridges_fast(q)
    except Exception as exc:
        logger.error("extract_bridges_fast failed: {}", exc)

    # Fuse BM25 + graph for ALL results (lessons + SPARTA + any other collection)
    fused = fuse_bm25_graph(db, bm25=bm25, depth=2, k=max(k, len(bm25)), query_bridges=query_bridges, crosswalk_methods=crosswalk_methods)
    # Compute bm25 rr map against main query for transparency
    bm25_main = bm25_rank(db, q=q, scope=scope, tags=tags or [], k=max(k, 50), collections=collections)
    rr_map: Dict[str, float] = {}
    n = len(bm25_main)
    for idx, r in enumerate(bm25_main):
        # Normalize to [0, 1] range: first result = 1.0, last = 0.0 (or 1.0 if single result)
        rr_map[r['_key']] = (n - idx) / max(1, n) if n > 1 else 1.0
    # Graph scores per candidate for transparency (reuse from fuse_bm25_graph)
    g_score_map = get_last_graph_scores()
    g_scores = [float(g_score_map.get(str(r["_key"]), 0.0)) for r in fused]
    if g_scores:
        gmin, gmax = min(g_scores), max(g_scores)
    else:
        gmin, gmax = 0.0, 0.0
    # Optional subgraph score (operator-only)
    subgraph_scores: Dict[str, float] = {}
    if _os.getenv('RECALL_USE_SUBGRAPH') in ('1','true','TRUE'):
        try:
            from .lessons.recall import _build_query_subgraph, _score_subgraph_nodes
            sg = _build_query_subgraph(db, seeds=fused, max_edges=int(_os.getenv('SUBGRAPH_MAX_EDGES','500') or '500'), depth=2)
            raw = _score_subgraph_nodes(sg)
            if raw:
                vals = list(raw.values())
                mn, mx = (min(vals), max(vals)) if vals else (0.0, 0.0)
                for kx, v in raw.items():
                    subgraph_scores[str(kx)] = 0.0 if mx <= mn else (float(v) - mn) / (mx - mn)
        except Exception as exc:
            logger.error("subgraph scoring failed: {}", exc)
            subgraph_scores = {}
    # Dense blend when cached (with small freshness weight)
    used_dense = True  # Always on — every collection has embeddings
    dense_scores = _maybe_dense_scores(db, lessons=fused, q=q, k=max(k, 50))
    # Extra fields for tiny nudges
    want_recent = any(w in q.lower() for w in ('recent','recently','last','latest','when'))
    want_proc_intent = any(w in q.lower() for w in ('how to', 'fix', 'troubleshoot', 'stability'))
    extra_map: Dict[str, Dict[str, Any]] = {}
    if want_recent or want_proc_intent:
        try:
            keys = [r.get('_key') for r in fused if r.get('_key')]
            if keys:
                rows = list(db.aql.execute(
                    "FOR k IN @keys LET l = DOCUMENT('lessons_v2', k) RETURN { k: k, ts: l.time_span, pr: l.procedural, lt: l.is_longterm }",
                    bind_vars={'keys': keys}
                ))
                for row in rows:
                    extra_map[str(row.get('k'))] = {'time_span': row.get('ts'), 'procedural': bool(row.get('pr')), 'is_longterm': bool(row.get('lt'))}
        except Exception as exc:
            logger.error("extra_map fetch failed: {}", exc)
            extra_map = {}
    # Optional session/thread recency boost — previous top in same thread gets a tiny nudge
    session_boost_map: Dict[str, float] = {}
    if thread_id:
        try:
            row = list(db.aql.execute(
                "FOR e IN memory_events FILTER e.kind=='search' AND e.data.thread_id==@t AND (@s=='' OR e.data.scope==@s) SORT e.at DESC LIMIT 1 RETURN e",
                bind_vars={'t': thread_id, 's': scope or ''}
            ))
            if row:
                topk = (row[0].get('data') or {}).get('top')
                if topk:
                    session_boost_map[str(topk)] = 0.02
        except Exception as exc:
            logger.error("session recency boost failed: {}", exc)
            session_boost_map = {}

    # Distilled lesson penalty (from PDF extraction) - demote auto-generated content
    distilled_penalty = float(_os.getenv('RECALL_DISTILLED_PENALTY', '0.3') or '0.3')

    if dense_scores:
        vals = list(dense_scores.values())
        mn, mx = (min(vals), max(vals)) if vals else (0.0, 0.0)
        def nz(v):
            return 0.0 if mx <= mn else (v - mn) / (mx - mn)
        now = int(time.time())
        want_proc = want_proc_intent
        for i, r in enumerate(fused):
            rrw = rr_map.get(r["_key"], 0.0)
            dnw = nz(dense_scores.get(str(r["_key"]), 0.0))
            upd = _parse_updated_at(r.get('updated_at'))
            age_days = max(0.0, (now - upd) / 86400.0) if upd else 365.0
            fresh = pow(0.5, age_days / 90.0)
            mid = 0.25 if bool(r.get('is_midterm')) else 0.0
            lg = 0.15 if bool(r.get('is_longterm')) else 0.0
            ex = extra_map.get(str(r.get('_key')), {})
            proc_flag = bool(ex.get('procedural')) or any(w in (r.get('title') or '').lower() for w in ('how to','fix','troubleshooting','stability'))
            proc = 0.02 if want_proc and proc_flag else 0.0
            # optional integration tweak: small penalty when procedural intent and graph weak
            integ = 0.0
            if _os.getenv('INTEGRATION_TWEAK') in ('1','true','TRUE') and want_proc:
                gnorm = float(g_score_map.get(str(r.get('_key')), 0.0))
                gnorm = 0.0 if (gmax <= gmin) else (gnorm - gmin) / (gmax - gmin)
                if gnorm < 0.1:
                    integ = -0.02
            sgw = float(subgraph_scores.get(str(r.get('_key')), 0.0)) * float(_os.getenv('SUBGRAPH_BLEND','0.05') or '0.05')
            ts = ex.get('time_span') if isinstance(ex.get('time_span'), dict) else ex.get('time_span')
            recent_to = int((ts or {}).get('to') or 0) if isinstance(ts, dict) else 0
            recent = 0.02 if want_recent and recent_to >= (now - 60*86400) else 0.0
            sess = float(session_boost_map.get(str(r.get('_key')), 0.0))
            base_score = 0.65 * rrw + 0.25 * dnw + 0.10 * fresh + mid + lg + proc + recent + sess + integ + sgw
            # Apply distilled penalty (demote auto-extracted content from PDFs)
            tags_r = r.get('tags') or []
            if 'distilled' in tags_r and distilled_penalty > 0:
                base_score *= (1.0 - distilled_penalty)
            r["_final"] = base_score
        fused.sort(key=lambda x: (x.get("_final", 0.0), 1 if bool(x.get('is_longterm')) else 0), reverse=True)
        for r in fused:
            r.pop("_final", None)
    else:
        now = int(time.time())
        want_proc = want_proc_intent
        scored = []
        for i, r in enumerate(fused):
            rrw = rr_map.get(r["_key"], 0.0)
            upd = _parse_updated_at(r.get('updated_at'))
            age_days = max(0.0, (now - upd) / 86400.0) if upd else 365.0
            fresh = pow(0.5, age_days / 90.0)
            mid = 0.25 if bool(r.get('is_midterm')) else 0.0
            lg = 0.15 if bool(r.get('is_longterm')) else 0.0
            ex = extra_map.get(str(r.get('_key')), {})
            proc_flag = bool(ex.get('procedural')) or any(w in (r.get('title') or '').lower() for w in ('how to','fix','troubleshooting','stability'))
            proc = 0.02 if want_proc and proc_flag else 0.0
            integ = 0.0
            if _os.getenv('INTEGRATION_TWEAK') in ('1','true','TRUE') and want_proc:
                gnorm = float(g_score_map.get(str(r.get('_key')), 0.0))
                gnorm = 0.0 if (gmax <= gmin) else (gnorm - gmin) / (gmax - gmin)
                if gnorm < 0.1:
                    integ = -0.02
            sgw = float(subgraph_scores.get(str(r.get('_key')), 0.0)) * float(_os.getenv('SUBGRAPH_BLEND','0.05') or '0.05')
            ts = ex.get('time_span') if isinstance(ex.get('time_span'), dict) else ex.get('time_span')
            recent_to = int((ts or {}).get('to') or 0) if isinstance(ts, dict) else 0
            recent = 0.02 if want_recent and recent_to >= (now - 60*86400) else 0.0
            sess = float(session_boost_map.get(str(r.get('_key')), 0.0))
            base_score = 0.90 * rrw + 0.10 * fresh + mid + lg + proc + recent + sess + integ + sgw
            # Apply distilled penalty (demote auto-extracted content from PDFs)
            tags_r = r.get('tags') or []
            if 'distilled' in tags_r and distilled_penalty > 0:
                base_score *= (1.0 - distilled_penalty)
            r["_final"] = base_score
            scored.append(r)
        fused = sorted(scored, key=lambda x: (x.get("_final", 0.0), 1 if bool(x.get('is_longterm')) else 0), reverse=True)
        for r in fused:
            r.pop("_final", None)
    # cluster diversify
    seen = set()
    sel: List[Dict[str, Any]] = []
    rest: List[Dict[str, Any]] = []
    for r in fused:
        cid = r.get("cluster_id") or ""
        if cid and cid not in seen:
            sel.append(r)
            seen.add(cid)
        else:
            rest.append(r)
        if len(sel) >= k:
            break
    if len(sel) < k:
        sel.extend(rest[: (k - len(sel))])
    # Final guided reordering (cache-only) after diversification
    if _os.getenv('RERANKER_GUIDED') in ('1','true','TRUE'):
        try:
            model_name = _os.getenv('RERANKER_MODEL', 'bge-reranker-base')
            cached = list(db.aql.execute(
                "FOR r IN reranker_cache FILTER r.model==@m AND r.q==@q RETURN r",
                bind_vars={'m': model_name, 'q': q}
            ))
            if cached:
                cmap = {str(c.get('lesson_key')): float(c.get('score') or 0.0) for c in cached}
                sel.sort(key=lambda x: float(cmap.get(str(x.get('_key')), 0.0)), reverse=True)
        except Exception as exc:
            logger.error("dense reorder from cache failed: {}", exc)
    # Optional reranker reordering (cache-only) when flag set
    used_reranker = False
    if _os.getenv('RECALL_USE_RERANKER') in ('1','true','TRUE'):
        try:
            model_name = _os.getenv('RERANKER_MODEL', 'bge-reranker-base')
            cached = list(db.aql.execute(
                "FOR r IN reranker_cache FILTER r.model==@m AND r.q==@q RETURN r",
                bind_vars={'m': model_name, 'q': q}
            ))
            if cached:
                rmap = {str(c.get('lesson_key')): float(c.get('score') or 0.0) for c in cached}
                for r in fused:
                    r['_rerank'] = float(rmap.get(str(r.get('_key')), 0.0))
                fused.sort(key=lambda x: x.get('_rerank', 0.0), reverse=True)
                used_reranker = True
                for r in fused:
                    r.pop('_rerank', None)
        except Exception as exc:
            logger.error("reranker reorder failed: {}", exc)
    # Attach scores object: bm25 rr, graph norm, dense raw
    # ERROR (not warning) if dense scoring returned nothing despite having lessons results.
    # This means the vector-store is down and confidence is degraded.
    # Does NOT crash — that caused a NameError + broke all SPARTA collection queries.
    # But logs at ERROR so monitoring catches it.
    if not dense_scores and (collections is None or "lessons" in collections) and fused:
        logger.error("DENSE SCORING EMPTY — vector-store likely down. /recall confidence is BM25+graph only, no cosine rerank.")
    items_scored = []
    debug_on = _os.getenv('RECALL_DEBUG') in ('1','true','TRUE')
    debug_sample = _os.getenv('RECALL_DEBUG_SAMPLE') in ('1','true','TRUE')
    debug_max = int(_os.getenv('RECALL_DEBUG_MAX', '5') or '5')
    for i, r in enumerate(sel):
        key = r.get('_key')
        g_raw = float(g_score_map.get(str(key), 0.0))
        if gmax <= gmin:
            g_norm = 0.5 if g_raw > 0 else 0.0
        else:
            g_norm = (g_raw - gmin) / (gmax - gmin)
        rr = rr_map.get(key, 0.0)
        dn = float(dense_scores.get(str(key), 0.0)) if dense_scores else 0.0
        item = dict(r)
        # Ensure _source is always set for provenance tracking
        if "_source" not in item:
            item["_source"] = "lessons"
        # Freshness for transparency (not a separate feature flag)
        upd = _parse_updated_at(item.get('updated_at'))
        age_days = max(0.0, (int(time.time()) - upd) / 86400.0) if upd else 365.0
        fresh = pow(0.5, age_days / 90.0)
        item['scores'] = { 'bm25': rr, 'graph': g_norm, 'dense': dn, 'freshness': fresh }
        # Optional per-item debug trace with sampling and temporal edge hints
        if debug_on and (not debug_sample or i < max(1, debug_max)):
            dbg = {
                'bm25_rr': rr,
                'graph_norm': g_norm,
                'dense_raw': dn,
                'freshness': fresh,
            }
            try:
                row = list(db.aql.execute(
                    """
                    LET id = CONCAT('lessons/', @k)
                    LET ts = (
                      FOR e IN lesson_edges FILTER e._from==id OR e._to==id
                        COLLECT AGGREGATE
                          min_created = MIN(e.created_at),
                          max_verified = MAX(e.last_verified_at)
                        RETURN {min_created_at:min_created, max_verified_at:max_verified}
                    )
                    RETURN FIRST(ts)
                    """,
                    bind_vars={'k': key}
                ))
                if row and row[0]:
                    dbg['edge_ts_hint'] = {
                        'min_created_at': int(row[0].get('min_created_at') or 0),
                        'max_verified_at': int(row[0].get('max_verified_at') or 0),
                    }
            except Exception as exc:
                logger.error("edge timestamp hint fetch failed: {}", exc)
            item['debug'] = dbg
        items_scored.append(item)
    supplemental_hits: List[Dict[str, Any]] = []
    try:
        # Check if supplemental sources are excluded
        if collections is None or any(c != "lessons" for c in collections):
            supplemental_hits = gather_supplemental_hits(
                db,
                q,
                scope,
                tags or [],
                collections=collections,
                entities=entities,
            )
    except Exception as exc:
        search_errors.append(f"supplemental_sources_failed: {exc}")
        supplemental_hits = []
    took = int((time.time() - t0) * 1000)

    # --- Score supplemental hits with dense + graph (same as main pipeline) ---
    # Bug fix: supplemental sources (app_actions, etc.) were returned with
    # graph=0 and dense=0 because they bypassed the scoring pipeline entirely.
    if supplemental_hits:
        from .lessons.recall import _maybe_dense_scores as _supp_dense
        supp_dense = _supp_dense(db, supplemental_hits, q, k)
        if supp_dense:
            for hit in supplemental_hits:
                hit_key = str(hit.get('_key', ''))
                dn = float(supp_dense.get(hit_key, 0.0))
                if 'scores' not in hit:
                    hit['scores'] = {}
                hit['scores']['dense'] = dn
            logger.info("Supplemental dense scoring: {}/{} hits scored", len(supp_dense), len(supplemental_hits))
        # Graph scoring for supplemental hits via batch
        try:
            from .lessons.recall import batch_graph_scores
            supp_seeds = [{'_key': h.get('_key', ''), '_source': h.get('_source', 'app_actions')} for h in supplemental_hits]
            supp_graph = batch_graph_scores(db, supp_seeds, depth=1)
            for hit in supplemental_hits:
                hit_key = str(hit.get('_key', ''))
                g = float(supp_graph.get(hit_key, 0.0))
                if 'scores' not in hit:
                    hit['scores'] = {}
                hit['scores']['graph'] = g
        except Exception as exc:
            logger.error("Supplemental graph scoring failed: {}", exc)

    # --- Gap fix: Score-based merge of supplemental hits (not blind append) ---
    # Gap 1: Boost sparta_qra when query matches SPARTA domain
    # Gap 2: Use mind tags (tactical_tags compat) for taxonomy-aware ranking
    # Gap 3: Promote supplemental items when lessons results are weak/absent
    supplemental_promoted = 0
    if supplemental_hits:
        # Apply taxonomy boost: when supplemental item tags match query bridges
        if query_bridges:
            query_bridge_set = set(query_bridges)
            for hit in supplemental_hits:
                # get_mind_tags reads 'mind' field, falls back to 'tactical_tags' (compat)
                hit_tags = set(get_mind_tags(hit))
                overlap = hit_tags & query_bridge_set
                if overlap:
                    boost = min(0.3, len(overlap) * 0.1)
                    scores = hit.setdefault("scores", {})
                    scores["taxonomy_boost"] = boost

        if not items_scored:
            # No lessons results — supplemental items become primary (Gap 3)
            items_out = supplemental_hits
            supplemental_promoted = len(supplemental_hits)
        else:
            # Interleave: boosted supplemental items rank after top lessons
            # items but before weak ones; unboosted still append (Gap 1)
            boosted = [h for h in supplemental_hits
                       if h.get("scores", {}).get("taxonomy_boost", 0) > 0]
            unboosted = [h for h in supplemental_hits
                         if h.get("scores", {}).get("taxonomy_boost", 0) == 0]
            items_out = items_scored[:k]
            if boosted:
                # Insert boosted items after top 2 lessons (or fewer if k < 3)
                insert_at = min(2, len(items_out))
                items_out = items_out[:insert_at] + boosted + items_out[insert_at:]
                supplemental_promoted = len(boosted)
            items_out.extend(unboosted)
    else:
        items_out = items_scored[:k]
    # Final collection filter: when collections is specified, only return items from those collections
    # "lessons" and "lessons_v2" are aliases — the BM25 router uses "lessons" but
    # bm25_rank returns _source="lessons_v2".  Accept either name for both.
    if collections:
        coll_set = set(collections)
        if "lessons" in coll_set or "lessons_v2" in coll_set:
            coll_set.update(("lessons", "lessons_v2"))
        items_out = [item for item in items_out if item.get("_source", "lessons") in coll_set]
    meta = {"q": q, "scope": scope, "k": k, "used_dense": used_dense, "used_reranker": used_reranker, "took_ms": took}
    if collections:
        meta["collections_filter"] = collections
    if supplemental_hits:
        meta["supplemental_count"] = len(supplemental_hits)
        if supplemental_promoted:
            meta["supplemental_promoted"] = supplemental_promoted
    out = {"meta": meta, "items": items_out, "errors": search_errors}
    try:
        log_event(db, 'search', f"search:{scope or 'global'}:{q}", {
            'scope': scope or '',
            'k': k,
            'used_dense': used_dense,
            'used_reranker': used_reranker,
            'top': items_scored[0]['_key'] if items_scored else None,
            'thread_id': thread_id or '',
            'lat': took
        })
    except Exception as exc:
        logger.error("log_event for search failed: {}", exc)
    
    # Strip embedding vectors from results (saves ~3000-16000 tokens per item)
    _strip_embeddings(out.get("items", []))
    
    return out


def explain(key: str, q: str | None = None, scope: str = "") -> Dict[str, Any]:
    """Explain a lesson: return optional query-dependent scores, a short 'why', and a brief path summary."""
    db = get_db()
    _id = key if key.startswith('lessons/') else f'lessons/{key}'
    row = list(db.aql.execute("RETURN DOCUMENT(@id)", bind_vars={'id': _id}))
    if not row or not row[0]:
        return {"meta": {"key": key}, "items": [], "errors": ["lesson not found"]}
    doc = row[0]
    scores: Dict[str, float] = {}
    why_parts: List[str] = []
    if q:
        bm25 = bm25_rank(db, q=q, scope=scope, tags=[], k=50)
        rr = 0.0
        for idx, r in enumerate(bm25):
            if str(r.get('_key')) == str(doc.get('_key')):
                rr = (len(bm25) - idx) / max(1, len(bm25))
                break
        scores['bm25'] = rr
        if rr > 0.5:
            why_parts.append(f"high BM25 match ({rr:.2f})")
        elif rr > 0:
            why_parts.append(f"BM25 match ({rr:.2f})")
        g_map = get_last_graph_scores()
        g_raw = float(g_map.get(str(doc.get('_key')), 0.0))
        gvals = [float(v) for v in g_map.values()] if g_map else [0.0]
        gmn, gmx = min(gvals), max(gvals)
        g_norm = 0.0 if gmx <= gmn else (g_raw - gmn) / (gmx - gmn)
        scores['graph'] = g_norm
        if g_norm > 0.3:
            why_parts.append(f"strong graph links ({g_norm:.2f})")
        elif g_norm > 0:
            why_parts.append(f"graph links ({g_norm:.2f})")
        dense = _maybe_dense_scores(db, lessons=[doc], q=q, k=50)
        dn = float(dense.get(str(doc.get('_key')), 0.0)) if dense else 0.0
        scores['dense'] = dn
        if dn > 0.5:
            why_parts.append(f"high semantic similarity ({dn:.2f})")
    # path summary
    path: List[Dict[str, Any]] = []
    subgraph_reason = ""
    try:
        _id_full = doc.get('_id') or f"lessons/{doc.get('_key')}"
        edges = list(db.aql.execute(
            "FOR e IN lesson_edges FILTER e._from==@id OR e._to==@id SORT e.weight DESC LIMIT 5 RETURN KEEP(e, '_from','_to','type','weight')",
            bind_vars={'id': _id_full}
        ))
        for e in edges:
            path.append(e)
    except Exception as exc:
        logger.error("explain path fetch failed: {}", exc)
    # Subgraph reason attempt (compact)
    if _os.getenv('RECALL_USE_SUBGRAPH') in ('1','true','TRUE'):
        try:
            from .lessons.recall import _build_query_subgraph, _score_subgraph_nodes
            sg = _build_query_subgraph(db, seeds=[doc], max_edges=50, depth=1)
            sg_scores = _score_subgraph_nodes(sg)
            if sg_scores and doc.get('_key') in sg_scores:
                subgraph_reason = f"subgraph centrality score: {sg_scores[doc.get('_key')]:.3f}"
        except Exception as exc:
            logger.error("subgraph reason failed: {}", exc)
    item = {
        **{k: doc.get(k) for k in ('_key','title','scope','tags','problem','playbook','solution','updated_at')},
        'scores': scores,
        'why': ', '.join(why_parts[:3]) if why_parts else '',
        'path': path,
        'subgraph_reason': subgraph_reason,
    }
    return {"meta": {"key": key, "q": q or ''}, "items": [item], "errors": []}


def related(title: str, scope: str = "tabbed", k: int = 10) -> Dict[str, Any]:
    db = get_db()
    seed = list(db.aql.execute('FOR d IN lessons_v2 FILTER d.title==@t AND d.scope==@s LIMIT 1 RETURN d._id', bind_vars={'t': title, 's': scope}))
    if not seed:
        return {"meta": {"title": title, "scope": scope, "k": k}, "items": [], "errors": ["seed not found"]}
    sid = seed[0]
    aql = '''
    FOR e IN lesson_edges
      FILTER e.type!='' AND (e._from==@sid OR e._to==@sid)
      LET nid = e._from==@sid ? e._to : e._from
      LET key = SPLIT(nid,'/')[1]
      LET l = DOCUMENT('lessons_v2', key)
      SORT e.weight DESC
      LIMIT @k
      RETURN { neighbor: KEEP(l,['_key','title','scope','tags','cluster_id']), edge: KEEP(e,['weight','approved','status','raw_sim','rationale','type']) }
    '''
    items = list(db.aql.execute(aql, bind_vars={'sid': sid, 'k': max(1, k)}))
    return {"meta": {"title": title, "scope": scope, "k": k}, "items": items, "errors": []}


def multihop(title: str, scope: str = "tabbed", depth: int = 2, limit: int = 10) -> Dict[str, Any]:
    db = get_db()
    seed = list(db.aql.execute('FOR d IN lessons_v2 FILTER d.title==@t AND d.scope==@s LIMIT 1 RETURN d._id', bind_vars={'t': title, 's': scope}))
    if not seed:
        return {"meta": {"title": title, "scope": scope, "depth": depth, "limit": limit}, "items": [], "errors": ["seed not found"]}
    sid = seed[0]
    aql = '''
    FOR v, e, p IN 1..@depth ANY @seed lesson_edges
      OPTIONS { bfs: true, uniqueVertices: 'path' }
      FILTER v._id != @seed
      LIMIT @limit
      RETURN { target: KEEP(v,['_key','title','scope','tags','cluster_id']), edges: p.edges }
    '''
    rows = list(db.aql.execute(aql, bind_vars={'seed': sid, 'depth': max(1, min(4, depth)), 'limit': max(1, limit)}))
    return {"meta": {"title": title, "scope": scope, "depth": depth, "limit": limit}, "items": rows, "errors": []}


def add_edge(from_title: str, to_title: str, type: str, from_scope: str = "", to_scope: str = "", weight: float = 0.75, rationale: str = "Authored", approved: bool = True) -> Dict[str, Any]:
    from .lessons.relations import ALLOWED_TYPES, SYMMETRIC_TYPES
    db = get_db()
    if type not in ALLOWED_TYPES:
        return {"meta": {}, "items": [], "errors": ["invalid type"]}
    f = list(db.aql.execute("FOR d IN lessons_v2 FILTER d.title==@t AND (@s=='' OR d.scope==@s) LIMIT 1 RETURN d._id", bind_vars={"t": from_title, "s": from_scope or ''}))
    t = list(db.aql.execute("FOR d IN lessons_v2 FILTER d.title==@t AND (@s=='' OR d.scope==@s) LIMIT 1 RETURN d._id", bind_vars={"t": to_title, "s": to_scope or ''}))
    if not f or not t:
        return {"meta": {}, "items": [], "errors": ["from/to not found"]}
    a, b = f[0], t[0]
    import hashlib
    pid = hashlib.sha1((a + '|' + b).encode('utf-8')).hexdigest()
    ts = int(time.time())
    wrote = []
    dirs = ((a, b), (b, a)) if type in SYMMETRIC_TYPES else ((a, b),)
    for frm, to in dirs:
        db.aql.execute(
            "UPSERT { _from:@f, _to:@t, type:@ty } INSERT { _from:@f, _to:@t, type:@ty, weight:@w, confidence:@w, approved:@ap, status:@st, created_at:@ts, updated_at:@ts, last_verified_at:@ts, pair_id:@pid } UPDATE { weight:@w, confidence:@w, approved:@ap, status:@st, updated_at:@ts, last_verified_at:@ts, pair_id:@pid } IN lesson_edges",
            bind_vars={"f": frm, "t": to, "ty": type, "w": max(0.0, min(1.0, weight)), "ap": bool(approved), "st": 'active' if approved else 'pending', "ts": ts, "pid": pid},
        )
        wrote.append({"from": frm, "to": to, "type": type})
    return {"meta": {"ok": True}, "items": wrote, "errors": []}


def log_episode(status: str, title: str, scope: str = "", user_id: str = "", project_id: str = "", thread_id: str = "", tags: List[str] | None = None, details: str = "", promote_if_novel: bool = False, high_fidelity: bool = False) -> Dict[str, Any]:
    db = get_db()
    ts = int(time.time())
    clean_tags, bridge_from_tags = _split_bridge_and_tags(tags)

    # Auto-taxonomy extraction (Federated Taxonomy)
    bridge_attributes = list(set(bridge_from_tags))
    try:
        from graph_memory.lessons.store import extract_bridges_fast
        tax_bridge = extract_bridges_fast(f"{title} {details or ''}")
        bridge_attributes = list(set(bridge_attributes + tax_bridge))
        method = "extract_bridges_fast" if tax_bridge else "no_match"
    except Exception as exc:
        logger.error("taxonomy extraction for learn failed: {}", exc)
        method = "failed"

    all_tags = list(set(clean_tags))

    # Determine which field to write taxonomy tags into:
    # - SPARTA-scoped episodes use 'mind' (new canonical field, 8 tactical tags)
    # - All other episodes keep 'bridge_attributes' (compat window, non-SPARTA contexts)
    is_sparta_scope = isinstance(scope, str) and scope.startswith("sparta")
    ep: Dict[str, Any] = {
        'status': status,
        'title': title,
        'scope': scope,
        'user_id': user_id,
        'project_id': project_id,
        'thread_id': thread_id,
        'tags': all_tags,
        'taxonomy_method': method,
        'taxonomy': tax_res if 'tax_res' in locals() else {},
        'details': details,
        'created_at': ts
    }
    if is_sparta_scope:
        # Write to 'mind' — the new canonical field for SPARTA tactical tags
        ep['mind'] = bridge_attributes
    else:
        # Keep 'bridge_attributes' for non-SPARTA contexts during compat window
        ep['bridge_attributes'] = bridge_attributes
    rid = db.collection('episodes').insert(ep)['_key']

    if promote_if_novel:
        rows = list(db.aql.execute(
            "FOR d IN unified_search SEARCH ANALYZER(d.title IN TOKENS(@q, 'text_en') OR d.problem IN TOKENS(@q,'text_en'), 'text_en') FILTER @scope=='' OR d.scope==@scope LIMIT 1 RETURN d",
            bind_vars={'q': title, 'scope': scope or ''},
        ))
        if not rows:
            import hashlib
            doc = {
                'title': f"EP[{rid}] {title}",
                'problem': details[:500] if details else '',
                'playbook': '- Summarize steps here',
                'tags': all_tags,
                'bridge_attributes': bridge_attributes,
                'scope': scope,
                'status': 'active',
                'added_by': user_id or 'agent',
                'updated_at': ts,
                'problem_hash': hashlib.sha256(details.encode() if details else title.encode()).hexdigest()[:16]
            }
            from graph_memory.lessons.store import store_lesson
            out_doc = store_lesson(db, doc)
            out = [out_doc]
            db.collection('episodes').update({'_key': rid, 'promoted_lesson_id': out[0]['_id']})
            return {"meta": {"ok": True}, "items": [{"episode": f"episodes/{rid}", "lesson": out[0]['_id']}], "errors": []}
    return {"meta": {"ok": True}, "items": [{"episode": f"episodes/{rid}"}], "errors": []}


def feedback(lesson_title: str, lesson_scope: str = "", helpful: bool = True, note: str = "") -> Dict[str, Any]:
    db = get_db()
    ts = int(time.time())
    seed = list(db.aql.execute("FOR d IN lessons_v2 FILTER d.title==@t AND (@s=='' OR d.scope==@s) LIMIT 1 RETURN d._id", bind_vars={'t': lesson_title, 's': lesson_scope or ''}))
    if not seed:
        return {"meta": {}, "items": [], "errors": ["lesson not found"]}
    lid = seed[0]
    db.aql.execute("LET d = DOCUMENT(@lid) UPDATE d WITH { usage_count: (d.usage_count ? d.usage_count : 0) + 1, used_at: @ts } IN lessons_v2", bind_vars={'lid': lid, 'ts': ts})
    # Best-effort mid-term marker: usage_count >= 3 and used within last 30 days
    try:
        db.aql.execute(
            """
            LET d = DOCUMENT(@lid)
            LET cnt = TO_NUMBER(d.usage_count ? d.usage_count : 0)
            LET recent = TO_NUMBER(d.used_at ? d.used_at : 0)
            LET now = @ts
            LET is_mid = cnt >= 3 && (now - recent) <= (30*86400)
            UPDATE d WITH { is_midterm: is_mid } IN lessons_v2
            """,
            bind_vars={'lid': lid, 'ts': ts}
        )
    except Exception as exc:
        logger.error("midterm flag update failed for {}: {}", lid, exc)
    adj = 0.02 if helpful else -0.02
    db.aql.execute("FOR e IN lesson_edges FILTER e._from==@lid OR e._to==@lid LET base = TO_NUMBER(e.confidence ? e.confidence : 0.5) LET nc = base + @adj LET clamped = nc > 1.0 ? 1.0 : (nc < 0.0 ? 0.0 : nc) UPDATE e WITH { confidence: clamped, last_verified_at: @ts } IN lesson_edges", bind_vars={'lid': lid, 'adj': adj, 'ts': ts})
    return {"meta": {"ok": True}, "items": [{"lesson": lid, "helpful": helpful}], "errors": []}


def arxiv_search(q: str, max_results: int = 5) -> Dict[str, Any]:
    t0 = time.time()
    try:
        items = _fetch_arxiv(q=q, max_results=max_results)
        took = int((time.time() - t0) * 1000)
        return {"meta": {"q": q, "count": len(items), "took_ms": took}, "items": items, "errors": []}
    except Exception as exc:
        return {"meta": {"q": q}, "items": [], "errors": [str(exc)]}
