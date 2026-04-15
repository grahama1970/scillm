from __future__ import annotations
import json
import typer
from typing import Dict, Any

from ..arango_client import get_db
from loguru import logger

app = typer.Typer(add_completion=False)

@app.command()
def analyze(
    thread_id: str = typer.Option(..., help="Thread ID or run/session id to analyze"),
    write_episode: bool = typer.Option(False, "--write-episode", help="Log a summary episode"),
    scope: str = typer.Option("", help="Scope tag for episode (optional)"),
    json_out: bool = typer.Option(True, "--json", help="Emit JSON envelope"),
):
    db = get_db()
    # Pull memory_events by thread
    events = list(db.aql.execute(
        """
        FOR e IN memory_events
          FILTER e.thread_id == @tid
          SORT e.at ASC
          RETURN KEEP(e, 'kind','tool','input','output','at')
        """,
        bind_vars={"tid": thread_id},
    ))
    summary = {
        "thread_id": thread_id,
        "event_count": len(events),
        "tools": sorted({(e.get('tool') or 'unknown') for e in events}),
    }
    if write_episode:
        try:
            db.collection("episodes").insert({
                "title": f"journey:{thread_id}",
                "scope": scope or "journey",
                "text": json.dumps(summary),
            })
        except Exception as exc:
            logger.error("analyze caught error: {exc}", exc=exc)
    out = {"meta": {"ok": True, "thread_id": thread_id}, "items": [summary], "errors": []}
    if json_out:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(out)

if __name__ == "__main__":
    app()
