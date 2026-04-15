from __future__ import annotations
import time
import json
import typer
from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view

app = typer.Typer(add_completion=False)


@app.command("demote-midterm")
def demote_midterm(
    days: int = typer.Option(30, help="Demote items unused for >= days"),
    scope: str = typer.Option("", help="Optional scope filter"),
    json_out: bool = typer.Option(True, "--json"),
):
    """Demote is_midterm for lessons unused for N days (best-effort)."""
    ensure_collections_and_view()
    db = get_db()
    now = int(time.time())
    cutoff = now - max(1, int(days)) * 86400
    aql = """
    FOR d IN lessons_v2
      FILTER d.is_midterm==true
      FILTER (@scope=='' OR d.scope==@scope)
      LET ua = TO_NUMBER(d.used_at ? d.used_at : 0)
      FILTER ua>0 AND ua <= @cutoff
      UPDATE d WITH { is_midterm: false } IN lessons_v2
      RETURN NEW._id
    """
    updated = list(db.aql.execute(aql, bind_vars={"cutoff": cutoff, "scope": scope or ""}))
    out = {"meta": {"ok": True, "cutoff": cutoff, "scope": scope}, "items": updated, "errors": []}
    if json_out:
        print(json.dumps(out))
    else:
        typer.echo(f"demoted midterm: {len(updated)} lessons")


@app.command("backfill-llm-rationale")
def backfill_llm_rationale(
    scope: str = typer.Option("llm_scope", help="Scope to constrain backfill (default: llm_scope)"),
    json_out: bool = typer.Option(True, "--json", help="Output JSON envelope"),
):
    """Backfill missing llm_rationale for approved edges in a scope.

    This replaces the previous connector-side side effect. It is explicit and
    safe to run by ops or during migrations.
    """
    ensure_collections_and_view()
    db = get_db()
    aql = """
    FOR e IN lesson_edges
      LET lf = DOCUMENT('lessons', SPLIT(e._from,'/')[1])
      FILTER lf!=null AND (@s=='' OR lf.scope==@s)
      FILTER e.approved==true AND (e.llm_rationale==null OR e.llm_rationale=='')
      UPDATE e WITH { llm_rationale: 'auto-approved' } IN lesson_edges
      RETURN NEW._id
    """
    rows = list(db.aql.execute(aql, bind_vars={"s": scope or ""}))
    out = {"meta": {"ok": True, "scope": scope}, "items": rows, "errors": []}
    if json_out:
        print(json.dumps(out))
    else:
        typer.echo(f"backfilled llm_rationale on {len(rows)} edges")
