from __future__ import annotations
import re
import typer
from typing import List
from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view

app = typer.Typer(add_completion=False)


def _norm_arxiv_id(s: str) -> str | None:
    if not s:
        return None
    m = re.search(r"([0-9]{4}\.[0-9]{4,5})", s)
    return m.group(1) if m else None


@app.command("build")
def build(scope: str = typer.Option("research")):
    ensure_collections_and_view()
    db = get_db()
    # Map arXiv id -> lesson _id
    lessons = list(db.aql.execute(
        "FOR d IN lessons_v2 FILTER d.scope==@s AND @tag IN d.tags RETURN d",
        bind_vars={'s': scope, 'tag': 'arxiv'}
    ))
    id_map = {}
    for d in lessons:
        title = d.get('title') or ''
        m = re.search(r"ARXIV\[([^\]]+)\]", title)
        if m:
            aid = _norm_arxiv_id(m.group(1))
            if aid:
                id_map[aid] = d.get('_id')
    wrote = 0
    for d in lessons:
        refs: List[str] = d.get('references') or []
        if not refs:
            continue
        frm = d.get('_id')
        for r in refs:
            aid = _norm_arxiv_id(r)
            if not aid:
                continue
            to = id_map.get(aid)
            if not to or to == frm:
                continue
            pid = frm < to and (frm + '|' + to) or (to + '|' + frm)
            db.aql.execute(
                "UPSERT { _from:@f, _to:@t, type:'cites' } "
                "INSERT { _from:@f, _to:@t, type:'cites', source:'arxiv_cites', weight:0.6, confidence:0.6, approved:true, status:'active', created_at:@ts, updated_at:@ts, last_verified_at:@ts, pair_id:@pid } "
                "UPDATE { source:'arxiv_cites', updated_at:@ts, last_verified_at:@ts, weight:0.6, confidence:0.6, approved:true, status:'active', pair_id:@pid } IN lesson_edges",
                bind_vars={'f': frm, 't': to, 'ts': 0, 'pid': pid}
            )
            wrote += 1
    typer.echo(f"cites edges written/updated: {wrote}")
