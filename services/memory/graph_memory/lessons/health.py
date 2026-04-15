from __future__ import annotations
import json
import typer
from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view

app = typer.Typer(add_completion=False)

@app.command("_noop", hidden=True)
def _noop():
    pass


@app.command()
def check(json_out: bool = typer.Option(False, "--json", help="Output JSON envelope")):
    """Lightweight health check for operators.

    Returns a small envelope with ok + arango status and known views.
    """
    ok = False
    items = {}
    errors: list[str] = []
    try:
        ensure_collections_and_view()
        db = get_db()
        views = []
        try:
            views = [v.get("name") for v in db.views()]
        except Exception as e:
            errors.append(str(e))
        ok = True  # DB reachable; views may be empty on fresh setup
        items = {"arango": {"reachable": True, "views": views}}
    except Exception as e:
        errors.append(str(e))
        items = {"arango": {"reachable": False, "views": []}}
        ok = False
    out = {"ok": ok, "items": items, "errors": errors}
    if json_out:
        print(json.dumps(out, ensure_ascii=False))
    else:
        typer.echo(out)
    return out

