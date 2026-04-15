from __future__ import annotations
import json
import typer
from typing import List

from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view

app = typer.Typer(add_completion=False)


@app.command()
def jsonl(
    out: str = typer.Option(".artifacts/events.jsonl", help="Output JSONL file path"),
    kinds: List[str] = typer.Option([], help="Filter by event kinds (repeat --kinds)"),
    scope: str = typer.Option("", help="Optional scope filter inside event data"),
    limit: int = typer.Option(1000, help="Max events"),
):
    """Export memory_events to JSONL for external analysis or training.

    Keeps it read-only and Happy Path compliant.
    """
    ensure_collections_and_view()
    db = get_db()
    aql = """
    FOR e IN memory_events
      FILTER LENGTH(@kinds)==0 OR e.kind IN @kinds
      FILTER @scope=='' OR (HAS(e,'data') && e.data.scope==@scope)
      SORT e.at ASC
      LIMIT @limit
      RETURN e
    """
    rows = list(db.aql.execute(aql, bind_vars={'kinds': kinds or [], 'scope': scope or '', 'limit': int(limit)}))
    import os
    os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(json.dumps({'meta': {'out': out, 'count': len(rows)}, 'items': [], 'errors': []}, ensure_ascii=False))
