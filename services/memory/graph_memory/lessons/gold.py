from __future__ import annotations
import json
import time
from typing import List, Optional
import typer

from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view
from loguru import logger

app = typer.Typer(add_completion=False, no_args_is_help=True)


def _now():
    return int(time.time())


@app.command("add")
def add(
    q: str = typer.Option(..., help="Query text"),
    lesson_id: List[str] = typer.Option([], help="One or more lessons/<key> ids"),
    lesson_ids_json: Optional[str] = typer.Option(None, help="JSON array of ids"),
    scope: str = typer.Option("", help="Optional scope"),
    source: str = typer.Option("manual", help="manual|feedback|promotion|mine"),
    rationale: str = typer.Option("", help="Why this is a good/bad mapping"),
    thread_id: str = typer.Option("", help="Optional thread id"),
    overwrite: bool = typer.Option(True, help="UPSERT existing (scope,q)"),
    json_out: bool = typer.Option(True, "--json"),
):
    ensure_collections_and_view()
    db = get_db()
    col = db.collection("gold_pairs")
    ids = list(lesson_id)
    if lesson_ids_json:
        try:
            ids.extend(json.loads(lesson_ids_json) or [])
        except Exception as exc:
            logger.error("add JSON parse failed: {exc}", exc=exc)
    ids = [str(i) for i in ids if i]
    if not ids:
        raise typer.BadParameter("Provide at least one lesson_id (or lesson_ids_json)")
    ts = _now()
    doc = {
        "q": q,
        "scope": scope or "",
        "lesson_ids": ids,
        "source": source or "manual",
        "rationale": rationale or "",
        "thread_id": thread_id or "",
        "created_at": ts,
        "updated_at": ts,
    }
    if overwrite:
        out = list(db.aql.execute(
            "UPSERT { scope:@s, q:@q } "
            "INSERT @d "
            "UPDATE MERGE(UNSET(@d, ['created_at']), { updated_at:@ts }) IN gold_pairs RETURN NEW",
            bind_vars={"s": doc["scope"], "q": doc["q"], "d": doc, "ts": ts}
        ))
        res = out[0] if out else None
    else:
        res = col.insert(doc)
    out = {"meta": {"ok": True}, "items": [res or doc], "errors": []}
    print(json.dumps(out))


@app.command("ls")
def ls(
    scope: str = typer.Option("", help="Optional scope filter"),
    limit: int = typer.Option(10, help="Max rows"),
    json_out: bool = typer.Option(True, "--json"),
):
    ensure_collections_and_view()
    db = get_db()
    q = (
        "FOR g IN gold_pairs FILTER @s=='' OR g.scope==@s "
        "SORT g.updated_at DESC LIMIT @n RETURN g"
    )
    rows = list(db.aql.execute(q, bind_vars={"s": scope or "", "n": int(limit)}))
    print(json.dumps({"meta": {"scope": scope or "", "limit": int(limit)}, "items": rows, "errors": []}))


def main():
    app()


if __name__ == "__main__":
    main()

