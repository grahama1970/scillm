from __future__ import annotations
import time
import re
from typing import List, Dict, Any, Tuple
import typer

from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view

app = typer.Typer(add_completion=False)


def _toks(s: str) -> List[str]:
    return [t.lower() for t in re.findall(r"[A-Za-z0-9_\-]+", s or "") if len(t) > 2]


def _edge(db, frm: str, to: str, ty: str, w: float, prov: str):
    ts = int(time.time())
    pid = frm < to and (frm + '|' + to) or (to + '|' + frm)
    db.aql.execute(
        "UPSERT { _from:@f, _to:@t, type:@ty } "
        "INSERT { _from:@f, _to:@t, type:@ty, source:@prov, weight:@w, confidence:@w, approved:true, status:'active', created_at:@ts, updated_at:@ts, last_verified_at:@ts, pair_id:@pid } "
        "UPDATE { source:@prov, weight:@w, confidence:@w, approved:true, status:'active', updated_at:@ts, last_verified_at:@ts, pair_id:@pid } IN lesson_edges",
        bind_vars={'f': frm, 't': to, 'ty': ty, 'w': max(0.0, min(1.0, float(w))), 'prov': prov, 'ts': ts, 'pid': pid}
    )


@app.command()
def build(scope: str = typer.Option("research"), limit: int = typer.Option(200), min_overlap: int = typer.Option(3)):
    """Build simple arXiv paper graph edges from cross-listing, authors, and keyword overlap."""
    ensure_collections_and_view()
    db = get_db()
    # Fetch latest research lessons tagged arxiv
    rows = list(db.aql.execute(
        """
        FOR d IN lessons_v2
          FILTER d.scope==@s AND @tag IN d.tags
          SORT d.updated_at DESC
          LIMIT @n
          RETURN KEEP(d, ['_key','title','tags','authors','scope'])
        """,
        bind_vars={'s': scope, 'n': max(2, int(limit)), 'tag': 'arxiv'}
    ))
    ids = [f"lessons/{r['_key']}" for r in rows]
    cats = [set([t.lower() for t in (r.get('tags') or []) if t.lower()!='arxiv']) for r in rows]
    auths = [set(_toks(' '.join([(a.get('name') or '') + ' ' + (a.get('affiliation') or '') for a in (r.get('authors') or [])]))) for r in rows]
    keys = [_toks(r.get('title') or '') for r in rows]
    wrote = 0
    for i in range(len(rows)):
        for j in range(i+1, len(rows)):
            frm, to = ids[i], ids[j]
            # co-category edge
            inter_c = cats[i] & cats[j]
            if inter_c:
                _edge(db, frm, to, 'co_category', min(1.0, 0.3 + 0.1*len(inter_c)), 'arxiv_graph')
                wrote += 1
            # same author edge
            inter_a = auths[i] & auths[j]
            if inter_a:
                _edge(db, frm, to, 'same_author', min(1.0, 0.5 + 0.05*len(inter_a)), 'arxiv_graph')
                wrote += 1
            # keyword overlap edge (from title tokens)
            inter_k = set(keys[i]) & set(keys[j])
            if len(inter_k) >= max(1, int(min_overlap)):
                _edge(db, frm, to, 'keyword_overlap', min(1.0, 0.4 + 0.05*len(inter_k)), 'arxiv_graph')
                wrote += 1
    typer.echo(f"arxiv graph edges written/updated: {wrote}")
