from __future__ import annotations
import time, json, os
from loguru import logger
import typer
from ..arango_client import get_db

app = typer.Typer(add_completion=False)


@app.command("coverage")
def coverage(scope: str = typer.Option("", help="Scope filter"), since: str = typer.Option("1d", help="Window (e.g., 1d, 7d, 24h)"), csv_out: str = typer.Option("", help="Optional CSV output path"), json_out: bool = typer.Option(True, "--json")) -> None:
    """Coverage/efficiency snapshot.

    Returns per-scope counters: refs_total (controls), refs_scored (anchor scores or recent edges), coverage_pct, contradictions (audit categories count), qa_pairs (generated), cache stats.
    """
    db = get_db()

    # Parse window
    mult = 1
    s = since.strip().lower()
    if s.endswith('d'):
        mult = 86400
        n = int(s[:-1] or '1')
    elif s.endswith('h'):
        mult = 3600
        n = int(s[:-1] or '24')
    else:
        n = int(s or '1')
        mult = 86400
    since_ts = int(time.time()) - n*mult

    # Controls in scope
    aql_controls = "FOR l IN lessons_v2 FILTER (@s=='' OR l.scope==@s) AND POSITION(l.tags, 'control', true) RETURN { _id:l._id, title:l.title }"
    controls = list(db.aql.execute(aql_controls, bind_vars={'s': scope or ''}))
    refs_total = len(controls)

    # Scored references (edges touched recently OR anchor_score_cache entries)
    aql_edges = """
    FOR e IN lesson_edges
      FILTER TO_NUMBER(e.updated_at) >= @ts
      LET from_key = SPLIT(e._from,'/')[1]
      LET to_key = SPLIT(e._to,'/')[1]
      LET scope_match = @s=='' OR (
        LENGTH(FOR lf IN lessons_v2 FILTER lf._key == from_key AND lf.scope == @s LIMIT 1 RETURN 1) > 0 OR
        LENGTH(FOR lt IN lessons_v2 FILTER lt._key == to_key AND lt.scope == @s LIMIT 1 RETURN 1) > 0
      )
      FILTER scope_match
      RETURN 1
    """
    recent_edges = len(list(db.aql.execute(aql_edges, bind_vars={'s': scope or '', 'ts': since_ts})))
    anchor_cache_pairs = 0
    try:
        anchor_cache_pairs = len(list(db.collection('anchor_score_cache')))
    except Exception as exc:
        logger.error("Failed to count anchor_score_cache: {}", exc)
        anchor_cache_pairs = 0
    refs_scored = recent_edges

    # QA pairs (best-effort): count last modified file-based pairs isn’t trivial; read optional qa_cache collection
    qa_pairs = 0
    try:
        qa_pairs = len(list(db.collection('qa_cache')))
    except Exception as exc:
        logger.error("Failed to count qa_cache: {}", exc)
        qa_pairs = 0

    # Contradictions (reuse audit categories counts quickly)
    try:
        from .audit import contradictions as _audit
        audit = _audit(scope=scope)
        contradictions = sum(len(v) for v in (audit.get('items') or {}).values())
    except Exception as exc:
        logger.error("Audit contradictions check failed: {}", exc)
        contradictions = 0

    # Coverage mode: edge-based (default) or control-based (unique controls with recent edges)
    coverage_mode = (os.getenv('COVERAGE_MODE') or 'edge').lower()
    if coverage_mode == 'control':
        control_with_recent = set()
        try:
            ctrl_edges = db.aql.execute(
                """
                FOR e IN lesson_edges
                  LET lf = DOCUMENT('lessons', SPLIT(e._from,'/')[1])
                  FILTER (@s=='' OR (lf!=null AND lf.scope==@s))
                  FILTER TO_NUMBER(e.updated_at) >= @ts
                  RETURN lf._id
                """, bind_vars={'s': scope or '', 'ts': since_ts}
            )
            for cid in ctrl_edges:
                if cid:
                    control_with_recent.add(cid)
            coverage_pct = float(len(control_with_recent)) / max(1.0, float(refs_total))
        except Exception as exc:
            logger.error("Control-based coverage computation failed: {}", exc)
            coverage_pct = float(refs_scored) / max(1.0, float(refs_total))
    else:
        coverage_pct = float(refs_scored) / max(1.0, float(refs_total))
    # Per-anchor rows (based on anchor_score_cache entries)
    per_anchor = []
    try:
        rows = list(db.aql.execute("FOR d IN anchor_score_cache COLLECT aid = d.anchor_id AGGREGATE ct = COUNT(d), last_ts = MAX(d.ts) RETURN { anchor_id: aid, scored: ct, last_ts: last_ts }"))
        scored_map = {r['anchor_id']: r['scored'] for r in rows}
        ts_map = {r['anchor_id']: r.get('last_ts') for r in rows}
        for c in controls:
            aid = c.get('_id')
            last_ts = ts_map.get(aid)
            stale_days = None
            if last_ts:
                stale_days = round((time.time() - last_ts)/86400,2)
            per_anchor.append({ 'anchor_id': aid, 'title': c.get('title'), 'scored': int(scored_map.get(aid, 0)), 'last_scored_ts': last_ts, 'staleness_days': stale_days })
        # Derive least-covered and most-contradictions (proxy: scored ascending)
        least = sorted(per_anchor, key=lambda r: r['scored'])[:50]
    except Exception as exc:
        logger.error("Per-anchor coverage aggregation failed: {}", exc)
        per_anchor = []
        least = []
    out = {
        'meta': {'scope': scope, 'since_ts': since_ts},
        'items': {
            'refs_total': refs_total,
            'refs_scored': refs_scored,
            'coverage_pct': round(coverage_pct, 4),
            'contradictions': contradictions,
            'qa_pairs': qa_pairs,
            'cache': {
                'anchor_cache_pairs': anchor_cache_pairs
            },
            'per_anchor': per_anchor,
            'least_covered': least
        },
        'errors': []
    }
    if csv_out:
        try:
            import csv
            import os as _os
            _os.makedirs(_os.path.dirname(csv_out) or '.', exist_ok=True)
            with open(csv_out, 'w', newline='', encoding='utf-8') as f:
                w = csv.writer(f)
                w.writerow(['anchor_id','title','scored','last_scored_ts','staleness_days'])
                for r in per_anchor:
                    w.writerow([r.get('anchor_id'), r.get('title'), r.get('scored'), r.get('last_scored_ts'), r.get('staleness_days')])
        except Exception as exc:
            logger.error("CSV export failed: {}", exc)
    print(json.dumps(out, ensure_ascii=False) if json_out else str(out))
