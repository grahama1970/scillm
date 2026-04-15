from __future__ import annotations
import time
import json
import os
import typer
from typing import List, Dict, Any

from loguru import logger
from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view
from .recall import bm25_rank, fuse_bm25_graph, _maybe_dense_scores, _expand_queries, graph_score_for_seed  # type: ignore[reportPrivateUsage]

app = typer.Typer(add_completion=False)


def _cluster_diversify(items: List[Dict[str, Any]], k: int) -> List[Dict[str, Any]]:
    if not items:
        return []
    seen = set()
    sel: List[Dict[str, Any]] = []
    rest: List[Dict[str, Any]] = []
    for r in items:
        cid = r.get('cluster_id') or ''
        if cid and cid not in seen:
            sel.append(r)
            seen.add(cid)
            if len(sel) >= k:
                break
        else:
            rest.append(r)
    if len(sel) < k:
        sel.extend(rest[: (k - len(sel))])
    return sel


@app.command()
def search(
    q: str = typer.Option(..., help="Search query"),
    scope: str = typer.Option("", help="Optional scope filter"),
    k: int = typer.Option(5, help="Top K"),
    anchor: str = typer.Option("", help="Optional anchor lesson id 'lessons/<key>' to lightly boost proximity"),
    as_of: int = typer.Option(0, help="Unix ts for point-in-time graph traversal (0=off)"),
    within_days: int = typer.Option(0, help="Minimum edge verification age window in days (0=off)"),
    include_archived: bool = typer.Option(False, "--include-archived", help="Include archived lessons in results"),
    only_archived: bool = typer.Option(False, "--only-archived", help="Return only archived lessons"),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON envelope {meta,items,errors}"),
):
    # Fake-DB contract mode for deterministic tests (no Arango required)
    if os.getenv('GM_FAKE_DB') in ('1','true','TRUE'):
        items = [
            { '_key': 'fake1', 'title': f"(FAKE) {q} — A", 'scope': scope or '', 'scores': { 'bm25': 0.9, 'graph': 0.1, 'dense': 0.0 }, 'why': 'bm25' },
            { '_key': 'fake2', 'title': f"(FAKE) {q} — B", 'scope': scope or '', 'scores': { 'bm25': 0.7, 'graph': 0.2, 'dense': 0.0 }, 'why': 'bm25, graph' },
        ][:k]
        took = 1
        out = { 'meta': { 'q': q, 'scope': scope, 'k': k, 'used_dense': False, 'used_reranker': False, 'took_ms': took, 'anchor': anchor or '', 'as_of': int(as_of or 0), 'within_days': int(within_days or 0) }, 'items': items, 'errors': [] }
        print(json.dumps(out, ensure_ascii=False))
        return
    t0 = time.time()
    # Ensure collections/views exist for first-run robustness
    ensure_collections_and_view()
    db = get_db()
    import os as _os
    thread_id = _os.getenv('THREAD_ID', '')
    # Soft multi-query expansion when needed
    candidates: Dict[str, Dict[str, Any]] = {}
    queries = _expand_queries(q, [])
    if not queries:
        queries = [q]
    seed_k = max(k, 3) if os.getenv('RERANKER_GUIDED') in ('1','true','TRUE') else k
    for qi in queries:
        for r in bm25_rank(db, q=qi, scope=scope, tags=[], k=seed_k):
            candidates[r['_key']] = r
    bm25 = list(candidates.values()) or bm25_rank(db, q=q, scope=scope, tags=[], k=seed_k)
    # Lightweight view catch-up: retry briefly if empty (fresh inserts)
    if not bm25:
        for _ in range(5):
            time.sleep(0.05)
            bm25 = bm25_rank(db, q=q, scope=scope, tags=[], k=seed_k)
            if bm25:
                break
    # Last-resort fallback: naive LIKE search over title/problem when view hasn't caught up
    if not bm25:
        try:
            aql = """
            FOR d IN lessons_v2
              FILTER @scope=='' OR d.scope==@scope
              FILTER @only_archived ? d.status=='archived' : (@include_archived ? true : (d.status==null OR d.status!='archived'))
              LET hay = LOWER(CONCAT_SEPARATOR(' ', d.title, d.problem))
              LET toks = SPLIT(LOWER(@q), ' ')
              FILTER LENGTH(toks) == 0 OR (
                ANY t IN toks SATISFIES t != '' AND CONTAINS(hay, t) END
              )
              LIMIT @k
              RETURN KEEP(d, '_key','title','scope','tags','cluster_id','updated_at')
            """
            bm25 = list(db.aql.execute(aql, bind_vars={'q': q, 'k': max(seed_k, 5), 'scope': scope or '', 'include_archived': bool(include_archived), 'only_archived': bool(only_archived)}))
        except Exception as exc:
            logger.error("LIKE fallback search failed: {}", exc)
            bm25 = []
    # Post-filter archived for view/textstore results (status not returned by bm25 view)
    try:
        if bm25 and (only_archived or not include_archived):
            keys = [r.get('_key') for r in bm25 if r.get('_key')]
            if keys:
                rows = list(db.aql.execute(
                    "FOR k IN @keys LET l = DOCUMENT('lessons_v2', k) RETURN {k:k, status:l.status}",
                    bind_vars={'keys': keys}
                ))
                status_map = {str(row.get('k')): (row.get('status') or '') for row in rows}
                def _keep(key: str) -> bool:
                    st = status_map.get(str(key), '')
                    if only_archived:
                        return st == 'archived'
                    # default (include_archived False): exclude archived
                    return st != 'archived'
                bm25 = [r for r in bm25 if _keep(r.get('_key'))]
    except Exception as exc:
        logger.error("Post-filter archived lessons failed: {}", exc)
    # (removed) avoid pre-fuse reranker reordering for stability
    # Fuse BM25 + graph
    # Optional temporal filters via env (applies to graph scoring)
    prev_asof = os.getenv('RECALL_AS_OF')
    prev_within = os.getenv('RECALL_WITHIN_DAYS')
    if int(as_of or 0) > 0:
        os.environ['RECALL_AS_OF'] = str(int(as_of))
    if int(within_days or 0) > 0:
        os.environ['RECALL_WITHIN_DAYS'] = str(int(within_days))
    try:
        fused = fuse_bm25_graph(db, bm25=bm25, depth=2, k=max(k, len(bm25)))
    finally:
        # restore envs
        if prev_asof is None:
            os.environ.pop('RECALL_AS_OF', None)
        else:
            os.environ['RECALL_AS_OF'] = prev_asof
        if prev_within is None:
            os.environ.pop('RECALL_WITHIN_DAYS', None)
        else:
            os.environ['RECALL_WITHIN_DAYS'] = prev_within
    # Optional anchor proximity boost (graph distance 1..3)
    anchor_boosts: Dict[str, float] = {}
    if anchor and isinstance(anchor, str) and anchor.startswith('lessons/') and fused:
        try:
            keys = [f"lessons/{r['_key']}" for r in fused if r.get('_key')]
            boosts: Dict[str, float] = {}
            for cid in keys[: max(60, len(keys)) ]:
                aql = """
                FOR v, e, p IN 1..3 ANY @a lesson_edges
                  OPTIONS { bfs: true, uniqueVertices: 'path' }
                  FILTER v._id == @c
                  LIMIT 1
                  RETURN LENGTH(p.edges)
                """
                rows = list(db.aql.execute(aql, bind_vars={'a': anchor, 'c': cid}))
                if rows:
                    L = int(rows[0] or 0)
                    if L >= 1:
                        boosts[cid.split('/')[1]] = round(0.03 / float(L), 4)
            anchor_boosts = boosts
        except Exception as exc:
            logger.error("Anchor proximity boost failed: {}", exc)
            anchor_boosts = {}
    # Apply reranker-guided ordering over fused list (cache-only)
    if os.getenv('RERANKER_GUIDED') in ('1','true','TRUE'):
        try:
            model_name = os.getenv('RERANKER_MODEL', 'bge-reranker-base')
            cached = list(db.aql.execute(
                "FOR r IN reranker_cache FILTER r.model==@m AND r.q==@q RETURN r",
                bind_vars={'m': model_name, 'q': q}
            ))
            cmap = {str(c.get('lesson_key')): float(c.get('score') or 0.0) for c in cached}
            if cmap:
                fused.sort(key=lambda r: float(cmap.get(str(r.get('_key')), 0.0)), reverse=True)
        except Exception as exc:
            logger.error("Reranker-guided ordering failed: {}", exc)
    # Scores: compute bm25 reciprocal rank (against main query only)
    bm25_main = bm25_rank(db, q=q, scope=scope, tags=[], k=max(k, 50))
    rr_map: Dict[str, float] = {}
    n = len(bm25_main)
    for idx, r in enumerate(bm25_main):
        rr_map[r['_key']] = (n - idx) / max(1, n - 1)
    # Compute graph scores per candidate and min-max normalize
    g_scores = []
    for r in fused:
        seed = f"lessons/{r['_key']}"
        g_scores.append(graph_score_for_seed(db, seed, depth=2))
    if g_scores:
        gmin, gmax = min(g_scores), max(g_scores)
    else:
        gmin, gmax = 0.0, 0.0
    # Optional subgraph score (operator-only)
    subgraph_scores: Dict[str, float] = {}
    if os.getenv('RECALL_USE_SUBGRAPH') in ('1','true','TRUE'):
        try:
            from .recall import _build_query_subgraph, _score_subgraph_nodes
            sg = _build_query_subgraph(db, seeds=fused, max_edges=int(os.getenv('SUBGRAPH_MAX_EDGES','500') or '500'), depth=2)
            raw = _score_subgraph_nodes(sg)
            if raw:
                vals = list(raw.values())
                mn, mx = (min(vals), max(vals)) if vals else (0.0, 0.0)
                for kx, v in raw.items():
                    subgraph_scores[str(kx)] = 0.0 if mx <= mn else (float(v) - mn) / (mx - mn)
        except Exception as exc:
            logger.error("Subgraph score computation failed: {}", exc)
            subgraph_scores = {}
    # Fetch lightweight attributes for ranking nudges (summary/midterm/longterm)
    attr_map: Dict[str, Dict[str, Any]] = {}
    try:
        keys = [r.get('_key') for r in fused if r.get('_key')]
        if keys:
            rows = list(db.aql.execute(
                "FOR k IN @keys LET l = DOCUMENT('lessons_v2', k) RETURN { k: k, is_summary: l.is_summary, is_midterm: l.is_midterm, is_longterm: l.is_longterm, usage_count: l.usage_count }",
                bind_vars={'keys': keys}
            ))
            for row in rows:
                attr_map[str(row.get('k'))] = {
                    'is_summary': bool(row.get('is_summary')),
                    'is_midterm': bool(row.get('is_midterm')),
                    'is_longterm': bool(row.get('is_longterm')),
                    'usage_count': int(row.get('usage_count') or 0),
                }
    except Exception as exc:
        logger.error("Fetch ranking attributes failed: {}", exc)
        attr_map = {}
# Dense blending (0.25) if we have cached embeddings
    dense_scores = _maybe_dense_scores(db, lessons=fused, q=q, k=max(k, 50))
    want_recent = any(w in q.lower() for w in ('recent','recently','last','latest','when'))
    want_proc_intent = any(w in q.lower() for w in ('how to','fix','troubleshoot','stability'))
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
            logger.error("Fetch extra time_span/procedural attributes failed: {}", exc)
            extra_map = {}
    # Optional session/thread recency boost via memory_events
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
            logger.error("Session recency boost lookup failed: {}", exc)
            session_boost_map = {}

    if dense_scores:
        vals = list(dense_scores.values())
        mn, mx = (min(vals), max(vals)) if vals else (0.0, 0.0)
        def nz(v: float):
            return 0.0 if mx <= mn else (v - mn) / (mx - mn)
        now = int(time.time())
        want_proc = want_proc_intent
        for i, r in enumerate(fused):
            # Preserve current fused order as the base reciprocal-rank weight
            ncur = len(fused)
            rrw = (ncur - i) / max(1, ncur) if ncur > 1 else 1.0  # Normalize to [0,1]
            dnw = nz(dense_scores.get(str(r['_key']), 0.0))
            upd = int(r.get('updated_at') or 0)
            age_days = max(0.0, (now - upd) / 86400.0) if upd else 365.0
            fresh = pow(0.5, age_days / 90.0)
            attrs = attr_map.get(str(r.get('_key')), {}); mid = 0.25 if bool(attrs.get('is_midterm')) else 0.0
            ex = extra_map.get(str(r.get('_key')), {})
            proc_flag = bool(ex.get('procedural')) or any(w in (r.get('title') or '').lower() for w in ('how to','fix','troubleshooting','stability'))
            proc = 0.02 if want_proc and proc_flag else 0.0
            lg = 0.15 if (bool(attrs.get('is_longterm')) or bool(ex.get('is_longterm'))) else 0.0; summ = 0.20 if bool(attrs.get('is_summary')) else 0.0
            # Optional integration tweak and subgraph blend
            integ = 0.0
            if os.getenv('INTEGRATION_TWEAK') in ('1','true','TRUE') and want_proc_intent:
                gnorm = g_scores[i] if i < len(g_scores) else 0.0
                gnorm = 0.0 if (gmax <= gmin) else (gnorm - gmin) / (gmax - gmin)
                if gnorm < 0.1:
                    integ = -0.02
            sgw = float(subgraph_scores.get(str(r.get('_key')), 0.0)) * float(os.getenv('SUBGRAPH_BLEND','0.05') or '0.05')
            ts = ex.get('time_span') if isinstance(ex.get('time_span'), dict) else ex.get('time_span')
            recent_to = int((ts or {}).get('to') or 0) if isinstance(ts, dict) else 0
            recent = 0.02 if want_recent and recent_to >= (now - 60*86400) else 0.0
            sess = float(session_boost_map.get(str(r.get('_key')), 0.0))
            ab = float(anchor_boosts.get(str(r.get('_key')), 0.0))
            r['_final'] = 0.64 * rrw + 0.25 * dnw + 0.10 * fresh + mid + lg + summ + proc + recent + sess + integ + sgw + ab
        fused.sort(key=lambda x: x.get('_final', 0.0), reverse=True)
        for r in fused:
            r.pop('_final', None)
    else:
        # No dense: lightly blend freshness with rr
        now = int(time.time())
        want_proc = want_proc_intent
        scored = []
        for i, r in enumerate(fused):
            # Preserve current fused order as the base reciprocal-rank weight
            ncur = len(fused)
            rrw = (ncur - i) / max(1, ncur) if ncur > 1 else 1.0  # Normalize to [0,1]
            upd = int(r.get('updated_at') or 0)
            age_days = max(0.0, (now - upd) / 86400.0) if upd else 365.0
            fresh = pow(0.5, age_days / 90.0)
            attrs = attr_map.get(str(r.get('_key')), {}); mid = 0.25 if bool(attrs.get('is_midterm')) else 0.0
            ex = extra_map.get(str(r.get('_key')), {})
            proc_flag = bool(ex.get('procedural')) or any(w in (r.get('title') or '').lower() for w in ('how to','fix','troubleshooting','stability'))
            proc = 0.02 if want_proc and proc_flag else 0.0
            lg = 0.15 if (bool(attrs.get('is_longterm')) or bool(ex.get('is_longterm'))) else 0.0; summ = 0.20 if bool(attrs.get('is_summary')) else 0.0
            integ = 0.0
            if os.getenv('INTEGRATION_TWEAK') in ('1','true','TRUE') and want_proc_intent:
                gnorm = g_scores[i] if i < len(g_scores) else 0.0
                gnorm = 0.0 if (gmax <= gmin) else (gnorm - gmin) / (gmax - gmin)
                if gnorm < 0.1:
                    integ = -0.02
            sgw = float(subgraph_scores.get(str(r.get('_key')), 0.0)) * float(os.getenv('SUBGRAPH_BLEND','0.05') or '0.05')
            ts = ex.get('time_span') if isinstance(ex.get('time_span'), dict) else ex.get('time_span')
            recent_to = int((ts or {}).get('to') or 0) if isinstance(ts, dict) else 0
            recent = 0.02 if want_recent and recent_to >= (now - 60*86400) else 0.0
            sess = float(session_boost_map.get(str(r.get('_key')), 0.0))
            ab = float(anchor_boosts.get(str(r.get('_key')), 0.0))
            r['_final'] = 0.90 * rrw + 0.10 * fresh + mid + lg + summ + proc + recent + sess + integ + sgw + ab
            scored.append(r)
        fused = sorted(scored, key=lambda x: (x.get('_final', 0.0), 1 if bool(x.get('is_longterm')) else 0), reverse=True)
        for r in fused:
            r.pop('_final', None)
    # Cluster-aware diversification
    fused = _cluster_diversify(fused, k)
    # (removed) avoid post-diversify reordering for stability
    # Optional reranker on-if-cached
    used_reranker = False
    if os.getenv('RECALL_USE_RERANKER') in ('1','true','TRUE'):
        # Already handled by recall for end-to-end; here: on-if-cached behavior
        model_name = os.getenv('RERANKER_MODEL', 'bge-reranker-base')
        cached = list(db.aql.execute(
            "FOR r IN reranker_cache FILTER r.model==@m AND r.q==@q RETURN r",
            bind_vars={'m': model_name, 'q': q}
        ))
        if cached:
            cmap = {c.get('lesson_key'): float(c.get('score') or 0.0) for c in cached}
            for r in fused:
                r['_rerank'] = float(cmap.get(r['_key'], 0.0))
            fused.sort(key=lambda x: x.get('_rerank', 0.0), reverse=True)
            used_reranker = True
            # leave no temp fields
            for r in fused:
                r.pop('_rerank', None)
    # Attach scores + a lightweight 'why' string for transparency
    scored_items = []
    for i, r in enumerate(fused):
        key = r.get('_key')
        g_raw = g_scores[i] if i < len(g_scores) else 0.0
        g_norm = 0.0 if gmax <= gmin else (g_raw - gmin) / (gmax - gmin)
        rr = rr_map.get(str(key or ''), 0.0)
        dn = float(dense_scores.get(str(key), 0.0)) if dense_scores else 0.0
        item = dict(r)
        item['scores'] = { 'bm25': rr, 'graph': g_norm, 'dense': dn }
        if anchor_boosts:
            item['scores']['anchor'] = float(anchor_boosts.get(str(item.get('_key')), 0.0))
        # Build 'why'
        reasons = []
        if rr >= 0.4:
            reasons.append('bm25')
        if g_norm >= 0.3:
            reasons.append('graph')
        if dense_scores and dn >= 0.1:
            reasons.append('dense')
        # Token/tag overlap heuristic
        q_toks = set([t.lower() for t in q.split() if t])
        itags = set([t.lower() for t in (item.get('tags') or [])])
        tovl = len(q_toks & itags)
        if tovl >= 1:
            reasons.append(f'tags overlap {tovl}')
        cid = item.get('cluster_id')
        if cid:
            reasons.append('cluster')
        item['why'] = ', '.join(reasons[:3]) if reasons else ''
        scored_items.append(item)
    took = int((time.time() - t0) * 1000)
    out = {
        'meta': {
            'q': q, 'scope': scope, 'k': k, 'used_dense': bool(dense_scores), 'used_reranker': bool(used_reranker), 'took_ms': took, 'anchor': anchor or '', 'as_of': int(as_of or 0), 'within_days': int(within_days or 0)
        },
        'items': scored_items[:k],
        'errors': [],
    }
    # Log event with optional thread id
    try:
        from ..events import log_event
        log_event(db, 'search', f"search:{scope or 'global'}:{q}", {
            'scope': scope or '', 'k': k, 'used_dense': bool(dense_scores), 'used_reranker': bool(used_reranker),
            'top': scored_items[0]['_key'] if scored_items else None, 'thread_id': thread_id or ''
        })
    except Exception as exc:
        logger.error("Failed to log search event: {}", exc)
    if json_out:
        print(json.dumps(out, ensure_ascii=False))
    else:
        import pprint
        pprint.pp(out)
