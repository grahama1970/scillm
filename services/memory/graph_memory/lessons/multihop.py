from __future__ import annotations
import typer
from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view
import json
import os

app = typer.Typer(add_completion=False)

@app.command()
def multihop(
    title: str = typer.Option(...),
    scope: str = typer.Option("tabbed"),
    depth: int = typer.Option(2),
    limit: int = typer.Option(10),
    json_out: bool = typer.Option(False, "--json", help="Output JSON envelope"),
):
    # Fake-DB contract mode (no Arango): return canned JSON
    if os.getenv('GM_FAKE_DB') in ('1','true','TRUE') and json_out:
        items = [ { 'target': { '_key': 'fake2', 'title': f'(FAKE) hop from {title}', 'scope': scope }, 'edges': [ { 'type': 'related', 'weight': 0.8 } ] } ]
        out = { 'meta': {'title': title, 'scope': scope, 'depth': depth, 'limit': limit}, 'items': items, 'errors': [] }
        typer.echo(json.dumps(out, ensure_ascii=False))
        return
    # Ensure schema availability on first run
    ensure_collections_and_view()
    db = get_db()
    seed = list(db.aql.execute('FOR d IN lessons_v2 FILTER d.title==@t AND d.scope==@s LIMIT 1 RETURN d._id', bind_vars={'t':title,'s':scope}))
    if not seed:
        if json_out:
            out = {'meta': {'title': title, 'scope': scope, 'depth': depth, 'limit': limit}, 'items': [], 'errors': ['seed not found']}
            typer.echo(json.dumps(out, ensure_ascii=False))
            return
        typer.echo('seed not found')
        raise typer.Exit(2)
    sid = seed[0]
    depth = max(1, min(4, int(depth)))
    aql = '''
    FOR v, e, p IN 1..@depth ANY @seed lesson_edges
      OPTIONS { bfs: true, uniqueVertices: 'path' }
      FILTER v._id != @seed
      LIMIT @limit
      RETURN { target: v, edges: p.edges }
    '''
    rows = list(db.aql.execute(aql, bind_vars={'seed':sid,'depth':depth,'limit':max(1,limit)}))
    out = { 'meta': {'title': title, 'scope': scope, 'depth': depth, 'limit': limit}, 'items': rows, 'errors': [] }
    if json_out:
        typer.echo(json.dumps(out, ensure_ascii=False))
    else:
        typer.echo(f"Paths: {len(rows)}")
        for i, r in enumerate(rows[:limit], 1):
            tgt = r['target']
            typer.echo(f"{i}. -> {tgt.get('title')} ({tgt.get('scope')})  edges={len(r.get('edges') or [])}")
