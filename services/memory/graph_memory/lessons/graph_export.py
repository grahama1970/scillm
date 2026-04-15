from __future__ import annotations
import json
import typer

from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view

# Create a sub-app to ensure group semantics for testing (app export ...)
_cmd_app = typer.Typer(add_completion=False)


@_cmd_app.command("export")
def export(
    scope: str = typer.Option("", help="Optional scope filter"),
    limit: int = typer.Option(500, help="Max lessons"),
    include_edges: bool = typer.Option(True, help="Include lesson_edges"),
    json_out: bool = typer.Option(True, "--json", help="Output JSON envelope"),
):
    ensure_collections_and_view()
    db = get_db()
    nodes = list(db.aql.execute(
        """
        FOR l IN lessons_v2
          FILTER @scope=='' OR l.scope==@scope
          SORT l.updated_at DESC
          LIMIT @limit
          RETURN KEEP(l, ['_key','title','scope','tags','cluster_id','updated_at','is_midterm'])
        """,
        bind_vars={'scope': scope or '', 'limit': max(1, int(limit))}
    ))
    node_keys = {n['_key'] for n in nodes}
    edges = []
    if include_edges and node_keys:
        edges = list(db.aql.execute(
            """
            FOR e IN lesson_edges
              LET from_key = SPLIT(e._from,'/')[1]
              LET to_key = SPLIT(e._to,'/')[1]
              FILTER from_key IN @nodes AND to_key IN @nodes
              RETURN KEEP(e, ['_from','_to','type','weight','confidence','approved','status'])
            """,
            bind_vars={'nodes': list(node_keys)}
        ))
    out = {'meta': {'scope': scope, 'limit': limit}, 'nodes': nodes, 'edges': edges}
    if json_out:
        print(json.dumps(out, ensure_ascii=False))
    else:
        typer.echo(out)
    return out


# Public app exposing the command as a subcommand: `export`
app = typer.Typer(add_completion=False)
app.add_typer(_cmd_app)
