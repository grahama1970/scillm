from __future__ import annotations
import typer
from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view
import json
import os

app = typer.Typer(add_completion=False)

@app.command()
def related(
    title: str = typer.Option(...),
    scope: str = typer.Option("tabbed"),
    k: int = typer.Option(10),
    json_out: bool = typer.Option(False, "--json", help="Output JSON envelope"),
):
    # Fake-DB contract mode (no Arango): return canned JSON
    if os.getenv('GM_FAKE_DB') in ('1','true','TRUE') and json_out:
        out = { 'meta': { 'title': title, 'scope': scope, 'k': k }, 'items': [ { 'neighbor': { '_key': 'fake1', 'title': f'(FAKE) related to {title}', 'scope': scope }, 'edge': { 'weight': 0.9, 'approved': True, 'status': 'active' } } ], 'errors': [] }
        typer.echo(json.dumps(out, ensure_ascii=False))
        return
    # Ensure schema availability on first run
    ensure_collections_and_view()
    db = get_db()
    seed = list(db.aql.execute('FOR d IN lessons_v2 FILTER d.title==@t AND d.scope==@s LIMIT 1 RETURN d._id', bind_vars={'t':title,'s':scope}))
    if not seed:
        if json_out:
            out = {'meta': {'title': title, 'scope': scope, 'k': k}, 'items': [], 'errors': ['seed not found']}
            typer.echo(json.dumps(out, ensure_ascii=False))
            return
        typer.echo('seed not found')
        raise typer.Exit(2)
    sid = seed[0]
    aql = '''
    FOR e IN lesson_edges
      FILTER e.type=='related' AND (e._from==@sid OR e._to==@sid)
      LET nid = e._from==@sid ? e._to : e._from
      LET key = SPLIT(nid,'/')[1]
      LET l = DOCUMENT('lessons_v2', key)
      SORT e.weight DESC
      LIMIT @k
      RETURN { neighbor: KEEP(l,['_key','title','scope','tags']), edge: KEEP(e,['weight','approved','status','raw_sim','rationale']) }
    '''
    items = list(db.aql.execute(aql, bind_vars={'sid':sid,'k':max(1,k)}))
    out = { 'meta': {'title': title, 'scope': scope, 'k': k}, 'items': items, 'errors': [] }
    if json_out:
        typer.echo(json.dumps(out, ensure_ascii=False))
    else:
        import pprint
        pprint.pp(out)
