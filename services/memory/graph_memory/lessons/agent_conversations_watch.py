from __future__ import annotations
import time
import json
import typer
from typing import Optional

from .agent_conversations import _parse_since, _envelope
from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view

app = typer.Typer(add_completion=False)


@app.command()
def watch(
    id_to: str = typer.Option(..., "--id-to", help="Recipient filter (agent id)"),
    topic: str = typer.Option("", help="Optional topic filter"),
    priority: str = typer.Option("", help="Optional priority filter"),
    action_required: bool = typer.Option(False, "--action-required", help="Only action-required"),
    since_ts: str = typer.Option("", help="Start time (unix or ISO8601); defaults to now"),
    interval: int = typer.Option(15, help="Poll interval seconds"),
    limit: int = typer.Option(50, help="Max rows per poll"),
):
    """Blocking watch loop that polls agent_conversations and streams new items to stdout."""
    ensure_collections_and_view()
    db = get_db()
    last_ts = _parse_since(since_ts) or int(time.time())
    typer.echo(f"[watch] id_to={id_to} topic={topic or '*'} priority={priority or '*'} action_required={action_required} interval={interval}s start_ts={last_ts}")
    while True:
        try:
            filters = [
                "(@scope=='' OR m.scope==@scope)",
                "(@recipient IN m.id_to OR 'all' IN m.id_to)",
                "m.ts_unix>=@since",
            ]
            bind_vars = {
                "scope": "agent_conversations",
                "recipient": id_to,
                "since": int(last_ts),
                "lim": max(1, int(limit)),
            }
            if topic:
                filters.append("m.topic==@topic"); bind_vars["topic"] = topic
            if priority:
                filters.append("m.priority==@priority"); bind_vars["priority"] = priority
            if action_required:
                filters.append("m.action_required==true")
            query = [
                "FOR m IN agent_conversations",
                "FILTER " + " AND ".join(filters),
                "SORT m.ts_unix ASC",
                "LIMIT @lim",
                "RETURN m",
            ]
            rows = list(db.aql.execute(" ".join(query), bind_vars=bind_vars))
            for r in rows:
                last_ts = max(last_ts, int(r.get("ts_unix") or 0))
                typer.echo(json.dumps(_envelope(r), ensure_ascii=False))
        except KeyboardInterrupt:
            raise
        except Exception as e:
            typer.echo(f"[watch] error: {e}", err=True)
        time.sleep(max(1, int(interval)))


if __name__ == "__main__":
    app()
