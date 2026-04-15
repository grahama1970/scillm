from __future__ import annotations
import json
import os
import typer
from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view
from loguru import logger

app = typer.Typer(add_completion=False)


@app.command()
def purge(
    title: str = typer.Option("", help="Lesson title (alternative to --id)"),
    scope: str = typer.Option("", help="Scope for title lookup"),
    id: str = typer.Option("", help="Full lesson id (e.g., lessons/12345)"),
    confirm: str = typer.Option("", help='Type DELETE to confirm'),
    json_out: bool = typer.Option(False, "--json"),
):
    # Env guard (operator-only)
    if os.getenv('HARD_PURGE_ENABLE') not in ('1','true','TRUE','YES'):
        raise typer.Exit(code=2)
    if confirm != 'DELETE':
        raise typer.Exit(code=2)
    ensure_collections_and_view()
    db = get_db()
    lid = id.strip()
    if not lid:
        if not title:
            raise typer.Exit(code=2)
        rows = list(db.aql.execute(
            "FOR l IN lessons_v2 FILTER l.title==@t AND (@s=='' OR l.scope==@s) LIMIT 1 RETURN l",
            bind_vars={'t': title, 's': scope or ''}
        ))
        if not rows:
            out = {'meta': {}, 'items': [], 'errors': ['not_found']}
            if json_out:
                print(json.dumps(out, ensure_ascii=False))
            raise typer.Exit(code=1)
        lid = rows[0]['_id']
    # Remove edges
    db.aql.execute("FOR e IN lesson_edges FILTER e._from==@id OR e._to==@id REMOVE e IN lesson_edges", bind_vars={'id': lid})
    # Remove lesson texts
    db.aql.execute("FOR t IN lesson_texts FILTER t.lesson_id==@id REMOVE t IN lesson_texts", bind_vars={'id': lid})
    # Remove embeddings (best-effort if schema stores lesson_id)
    try:
        db.aql.execute("FOR e IN lesson_embeddings FILTER e.lesson_id==@id REMOVE e IN lesson_embeddings", bind_vars={'id': lid})
    except Exception as exc:
        logger.error("purge AQL execute failed: {exc}", exc=exc)
    # Remove lesson doc
    try:
        db.aql.execute("REMOVE @id IN lessons_v2 OPTIONS { ignoreErrors: true }", bind_vars={'id': lid})
    except Exception as exc:
        logger.error("purge AQL execute failed: {exc}", exc=exc)
    out = {'meta': {}, 'items': [{'purged': lid}], 'errors': []}
    if json_out:
        print(json.dumps(out, ensure_ascii=False))

