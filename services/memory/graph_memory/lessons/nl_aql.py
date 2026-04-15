from __future__ import annotations
import json
import typer
from typing import Dict, Any

from ..arango_client import get_db
from ..aql.templates import plan_from_intent, validate_plan, plan_to_json

app = typer.Typer(add_completion=False)

@app.command()
def run(
    intent: str = typer.Option(..., help="Intent: search|edges|issues"),
    q: str = typer.Option("", help="Query text (for search intent)"),
    scope: str = typer.Option("", help="Scope filter"),
    edge_type: str = typer.Option("related", help="Edge type for edges intent"),
    k: int = typer.Option(10, help="Top K (bounded)"),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON envelope"),
):
    params: Dict[str, Any] = {"q": q, "scope": scope, "edge_type": edge_type, "k": k}
    try:
        plan = plan_from_intent(intent, params)
        validate_plan(plan)
    except Exception as e:  # validation or planning error
        err = str(e)
        out = {"meta": {"ok": False, "intent": intent}, "items": [], "errors": [err]}
        if json_out:
            print(json.dumps(out, ensure_ascii=False))
        else:
            print(out)
        raise typer.Exit(1)

    db = get_db()
    try:
        rows = list(db.aql.execute(plan.aql, bind_vars=plan.bind_vars))
        out = {"meta": {"ok": True, "intent": intent, "plan": json.loads(plan_to_json(plan))}, "items": rows, "errors": []}
    except Exception as e:  # execution error
        out = {"meta": {"ok": False, "intent": intent, "plan": json.loads(plan_to_json(plan))}, "items": [], "errors": [str(e)]}
        if json_out:
            print(json.dumps(out, ensure_ascii=False))
        else:
            print(out)
        raise typer.Exit(2)

    if json_out:
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(out)

if __name__ == "__main__":
    app()
