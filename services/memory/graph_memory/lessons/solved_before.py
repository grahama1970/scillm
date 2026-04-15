from __future__ import annotations
import json
import typer
from typing import Dict, Any

from ..arango_client import get_db

app = typer.Typer(add_completion=False)

@app.command()
def run(
    q: str = typer.Option(..., help="Problem description"),
    scope: str = typer.Option("", help="Scope filter"),
    k: int = typer.Option(5, help="Top K"),
    json_out: bool = typer.Option(True, "--json"),
):
    db = get_db()
    # Find lessons matching query via simple view search
    lessons = list(db.aql.execute(
        """
        FOR d IN unified_search
          SEARCH ANALYZER(d.title IN TOKENS(@q, 'text_en'), 'text_en')
          FILTER @scope=='' OR d.scope==@scope
          LIMIT 20
          RETURN d
        """,
        bind_vars={"q": q, "scope": scope or ""},
    ))
    ids = [f"lessons/{d['_key']}" for d in lessons if d.get('_key')]
    solved = []
    if ids:
        edges = list(db.aql.execute(
            """
            FOR e IN lesson_edges
              FILTER e._from IN @ids OR e._to IN @ids
              FILTER e.type IN ['solves','duplicates']
              FILTER @scope=='' OR e.scope==@scope
              LIMIT @k
              RETURN e
            """,
            bind_vars={"ids": ids, "scope": scope or "", "k": k},
        ))
        solved = edges[:k]
    out = {"meta": {"ok": True, "q": q, "scope": scope}, "items": solved, "errors": []}
    if json_out:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(out)

if __name__ == "__main__":
    app()
