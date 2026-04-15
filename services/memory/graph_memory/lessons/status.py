from __future__ import annotations

from loguru import logger as _logger

# Best-effort .env auto-loading for symmetry with Make/MCP
try:
    from dotenv import load_dotenv, find_dotenv  # type: ignore
    _p = find_dotenv(usecwd=True)
    load_dotenv(_p or None)
except Exception as exc:
    _logger.debug("dotenv loading skipped: {}", exc)
import typer
from ..arango_client import get_db
import os
from ..setup_schema import ensure_collections_and_view

app = typer.Typer(add_completion=False)


@app.command()
def status(json_out: bool = typer.Option(False, "--json")):
    ensure_collections_and_view()
    db = get_db()
    aql = """
    LET lessons_total = LENGTH(lessons)
    LET lessons_demo = LENGTH(FOR d IN lessons_v2 FILTER d.demo==true RETURN 1)
    LET per_scope = (
      FOR d IN lessons_v2
        COLLECT s = d.scope WITH COUNT INTO ct
        RETURN { scope: s, count: ct }
    )
    LET edges_total = LENGTH(lesson_edges)
    LET edges_active = LENGTH(FOR e IN lesson_edges FILTER e.status=='active' AND e.approved==true RETURN 1)
    LET edges_pending = LENGTH(FOR e IN lesson_edges FILTER e.status=='pending' AND (e.approved==false OR e.approved==null) RETURN 1)
    LET edge_types = (
      FOR e IN lesson_edges COLLECT t = e.type WITH COUNT INTO ct RETURN { type: t, count: ct }
    )
    LET top_clusters = (
      FOR d IN lessons_v2 FILTER d.cluster_id != null COLLECT cid = d.cluster_id WITH COUNT INTO ct SORT ct DESC LIMIT 10 RETURN { cluster_id: cid, size: ct }
    )
    LET rejected = LENGTH(rejected_pairs)
    LET embeds = LENGTH(lesson_embeddings)
    LET archived_count = LENGTH(FOR d IN lessons_v2 FILTER d.status=='archived' RETURN 1)
    LET archived_sample = (
      FOR d IN lessons_v2 FILTER d.status=='archived' SORT d.updated_at DESC LIMIT 5 RETURN KEEP(d, ['title','scope','updated_at'])
    )
    LET symbol_total = LENGTH(symbol_snapshots)
    LET symbol_lsp = LENGTH(FOR s IN symbol_snapshots FILTER s.lsp_enrichment != null RETURN 1)
    RETURN {
      lessons: { total: lessons_total, demo: lessons_demo, per_scope: per_scope, archived: { count: archived_count, sample: archived_sample } },
      edges: { total: edges_total, active: edges_active, pending: edges_pending, types: edge_types },
      clusters: top_clusters,
      rejected_pairs: rejected,
      embeddings: embeds,
      tree_sitter_status: { total_snapshots: symbol_total },
      lsp_status: { enriched: symbol_lsp }
    }
    """
    out = list(db.aql.execute(aql))[0]
    # Optional issues summary (cross-project tracker)
    try:
        issues = list(db.aql.execute(
            """
            LET total = LENGTH(issues)
            LET open_ct = LENGTH(FOR i IN issues FILTER i.status=='open' RETURN 1)
            LET by_project = (
              FOR i IN issues
                COLLECT p = i.project WITH COUNT INTO ct
                SORT ct DESC
                LIMIT 8
                RETURN { project: p, count: ct }
            )
            RETURN { total: total, open: open_ct, by_project: by_project }
            """
        ))
        if issues:
            out['issues'] = issues[0]
    except Exception as exc:
        _logger.error("issues summary query failed: {}", exc)
    # Optional light observability: p50/p95 latency and cache usage proxy over recent searches
    if (os.getenv('OBSERVABILITY_LIGHT') in ('1','true','TRUE')):
        try:
            stats = list(db.aql.execute(
                """
                LET rec = (
                  FOR e IN memory_events FILTER e.kind=='search' AND e.data.lat!=null SORT e.at DESC LIMIT 200 RETURN e
                )
                LET lats = (FOR e IN rec RETURN TO_NUMBER(e.data.lat))
                LET p50 = PERCENTILE(lats, 50)
                LET p95 = PERCENTILE(lats, 95)
                LET dense_used = LENGTH(FOR e IN rec FILTER e.data.used_dense==true RETURN 1)
                LET rerank_used = LENGTH(FOR e IN rec FILTER e.data.used_reranker==true RETURN 1)
                LET total = LENGTH(rec)
                RETURN {
                  latency_ms: { p50: p50, p95: p95, samples: total },
                  cache_proxy: { dense_rate: (total>0? dense_used/total : 0), reranker_rate: (total>0? rerank_used/total : 0) }
                }
                """
            ))[0]
            out['observability'] = stats
            # LLM escalation counters (recent window)
            try:
                llm = list(db.aql.execute(
                    """
                    LET rec = (
                      FOR e IN memory_events FILTER e.kind=='llm_escalate' SORT e.at DESC LIMIT 500 RETURN e
                    )
                    LET total = LENGTH(rec)
                    LET p95_ms = PERCENTILE(FOR e IN rec FILTER e.data.took_ms!=null RETURN TO_NUMBER(e.data.took_ms), 95)
                    // Compute 24h window and per-scope breakdown
                    LET recent_24h_arr = (
                      FOR e IN rec
                        LET eat_ms = e.at >= 1000000000000 ? e.at : e.at * 1000
                        FILTER eat_ms >= DATE_NOW() - 24*3600*1000
                        RETURN e
                    )
                    LET recent_24h = LENGTH(recent_24h_arr)
                    LET by_scope = (
                      FOR e IN recent_24h_arr
                        COLLECT s = e.data.scope WITH COUNT INTO ct
                        SORT ct DESC
                        LIMIT 8
                        RETURN { scope: s, count: ct }
                    )
                    RETURN { escalations: { total: total, recent_24h: recent_24h, p95_ms: p95_ms }, escalations_by_scope: by_scope }
                    """
                ))[0]
                out.setdefault('observability',{}).update(llm)
            except Exception as exc:
                _logger.error("LLM escalation counters query failed: {}", exc)
            # Optional quality evaluation on a tiny internal corpus
            qfile = os.getenv('OBS_QUALITY_FILE', '').strip()
            if qfile:
                import json as _json
                from .. import api as _api
                try:
                    qdata = _json.loads(open(qfile,'r',encoding='utf-8').read())
                except Exception as exc:
                    _logger.error("quality file {} load failed: {}", qfile, exc)
                    qdata = {}
                qs = qdata.get('queries') or []
                kval = int(os.getenv('OBS_QUALITY_K','5') or '5')
                tot = 0
                hits = 0
                mrr_sum = 0.0
                for q in qs:
                    qq = q.get('q') or ''
                    sc = q.get('scope') or ''
                    exp = str(q.get('expected') or '')
                    if not qq or not exp:
                        continue
                    try:
                        res = _api.search(qq, scope=sc, k=kval)
                        keys = [str(it.get('_key')) for it in (res.get('items') or [])]
                        tot += 1
                        if exp in keys:
                            hits += 1
                            rank = keys.index(exp) + 1
                            mrr_sum += 1.0/float(rank)
                    except Exception as exc:
                        _logger.error("quality eval query {!r} failed: {}", qq, exc)
                if tot>0:
                    out.setdefault('observability',{})['quality'] = {
                        'recall_at_k': hits/float(tot),
                        'mrr': mrr_sum/float(tot),
                        'k': kval,
                        'samples': tot,
                    }
        except Exception as exc:
            _logger.error("observability stats query failed: {}", exc)
    if json_out:
        import json
        print(json.dumps(out, ensure_ascii=False))
        return
    print("Lessons:")
    print(f"  total={out['lessons']['total']} demo={out['lessons']['demo']}")
    for row in out['lessons']['per_scope']:
        print(f"  scope={row['scope'] or '(none)'} count={row['count']}")
    print("Edges:")
    print(f"  total={out['edges']['total']} active={out['edges']['active']} pending={out['edges']['pending']}")
    print(f"Rejected pairs: {out['rejected_pairs']}")
    print(f"Embeddings: {out['embeddings']}")
    print("Tree-Sitter:")
    print(f"  snapshots={out.get('tree_sitter_status', {}).get('total_snapshots', 0)}")
    print("LSP:")
    print(f"  enriched_snapshots={out.get('lsp_status', {}).get('enriched', 0)}")
    # Optional Issues summary (human-readable)
    issues = out.get('issues') or {}
    if issues:
        print("Issues:")
        print(f"  total={issues.get('total', 0)} open={issues.get('open', 0)}")
        for row in issues.get('by_project', []) or []:
            proj = row.get('project') or '(none)'
            print(f"  project={proj} count={row.get('count', 0)}")
