from __future__ import annotations
import json
import typer
from ..api import explain as api_explain

app = typer.Typer(add_completion=False)

# Force multi-command behavior so tests can invoke with the
# explicit subcommand token ("explain") via CliRunner.
@app.command("_noop", hidden=True)
def _noop():
    """No-op hidden command (ensures group semantics)."""
    pass



@app.command()
def explain(
    key: str = typer.Option(..., help="Lesson key or _id (e.g., lessons/123 or 123)"),
    q: str = typer.Option("", help="Optional query to compute bm25/dense/graph context"),
    scope: str = typer.Option("", help="Optional scope filter for query-dependent scores"),
    as_of: int = typer.Option(0, help="Unix ts for point-in-time graph traversal (0=off)"),
    within_days: int = typer.Option(0, help="Minimum edge verification age window in days (0=off)"),
    json_out: bool = typer.Option(False, "--json", help="Output JSON envelope"),
):
    # Apply temporal filters via env just for this call
    import os
    prev_asof = os.getenv('RECALL_AS_OF')
    prev_within = os.getenv('RECALL_WITHIN_DAYS')
    if int(as_of or 0) > 0:
        os.environ['RECALL_AS_OF'] = str(int(as_of))
    if int(within_days or 0) > 0:
        os.environ['RECALL_WITHIN_DAYS'] = str(int(within_days))
    try:
        out = api_explain(key=key, q=q or None, scope=scope)
    finally:
        if prev_asof is None:
            os.environ.pop('RECALL_AS_OF', None)
        else:
            os.environ['RECALL_AS_OF'] = prev_asof
        if prev_within is None:
            os.environ.pop('RECALL_WITHIN_DAYS', None)
        else:
            os.environ['RECALL_WITHIN_DAYS'] = prev_within
    if json_out:
        print(json.dumps(out, ensure_ascii=False))
    else:
        item = (out.get("items") or [{}])[0]
        typer.echo(f"title: {item.get('title','')}\nwhy: {item.get('why','')}\npath: {item.get('path','')}")

