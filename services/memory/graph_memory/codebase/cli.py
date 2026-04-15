"""CLI for lessons-codebase: curate and search commands.

Usage:
    lessons-codebase curate --code-path /path/to/repo
    lessons-codebase search --q "query" --scope code
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from graph_memory.codebase.pipeline.p1_init import run_p1_init
from graph_memory.codebase.pipeline.p2_ingest import run_p2_ingest
from graph_memory.codebase.pipeline.p3_pdf import run_p3_pdf
from graph_memory.codebase.pipeline.p4_lean import run_p4_lean
from graph_memory.codebase.pipeline.p5_finalize import run_p5_finalize
from graph_memory.codebase.pipeline.types import PhaseStatus

app = typer.Typer(
    name="lessons-codebase",
    help="Codebase ingestion and retrieval",
    add_completion=False,
)


@app.command()
def curate(
    code_path: Path = typer.Option(..., "--code-path", help="Path to codebase to ingest"),
    scope: str = typer.Option("code", "--scope", help="Scope for ingested items"),
    debug: bool = typer.Option(False, "--debug", help="Enable debug mode (reduced limits, verbose artifacts)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate without writing to DB"),
    output_json: bool = typer.Option(False, "--json", help="Output JSON envelope"),
):
    """Ingest codebase: symbols, docs, PDFs, and Lean theorems.

    Example:
        lessons-codebase curate --code-path /path/to/repo --json
    """
    # P1: init
    context, p1_result = run_p1_init(
        code_path=code_path,
        scope=scope,
        debug=debug,
        dry_run=dry_run,
    )

    if p1_result.status == PhaseStatus.FAILED_SOFT:
        result = _build_error_envelope(context, p1_result)
        if output_json:
            print(json.dumps(result, indent=2))
        else:
            typer.echo(f"curate: P1 init failed")
            for err in p1_result.errors:
                typer.echo(f"  - {err.get('message', err)}")
        raise typer.Exit(code=1)

    # P2: ingest
    p2_result = run_p2_ingest(context)
    context.phase_results["ingest"] = p2_result

    # P3: pdf_pipeline
    p3_result = run_p3_pdf(context)
    context.phase_results["pdf_pipeline"] = p3_result

    # P4: lean_pipeline
    p4_result = run_p4_lean(context)
    context.phase_results["lean_pipeline"] = p4_result

    # P5: finalize
    p5_result = run_p5_finalize(context)

    # Read the final run_summary
    summary_file = context.artifacts_path / "run_summary.json"
    result = json.loads(summary_file.read_text())

    if output_json:
        print(json.dumps(result, indent=2))
    else:
        meta = result.get("meta", {})
        typer.echo(f"curate: {code_path}")
        typer.echo(f"  run_id: {meta.get('run_id')}")
        typer.echo(f"  status: {meta.get('status')}")
        typer.echo(f"  artifacts: {context.artifacts_path}")
        typer.echo(f"  phases: {meta.get('phase_status')}")
        counts = meta.get("counts", {})
        if counts:
            typer.echo(f"  counts: {counts}")

    # Exit based on overall status
    status = result.get("meta", {}).get("status", "failed")
    if status == "ok":
        raise typer.Exit(code=0)
    elif status == "partial":
        raise typer.Exit(code=0)  # Partial is still success
    else:
        raise typer.Exit(code=1)


def _build_error_envelope(context, phase_result):
    """Build error envelope for failed phase."""
    return {
        "meta": {
            "run_id": context.run_id,
            "scope": context.scope,
            "status": "failed",
            "errors_count": len(phase_result.errors),
        },
        "items": [],
        "errors": phase_result.errors,
    }


@app.command()
def search(
    q: str = typer.Option(..., "--q", help="Search query"),
    scope: str = typer.Option("code", "--scope", help="Scope to search"),
    type_filter: Optional[str] = typer.Option(None, "--type", help="Filter by type: code, theorems, or all"),
    k: int = typer.Option(10, "--k", help="Number of results"),
    output_json: bool = typer.Option(False, "--json", help="Output JSON envelope"),
):
    """Search code symbols and theorems.

    Example:
        lessons-codebase search --q "validate_input" --type code --json
        lessons-codebase search --q "n + 0 = n" --type theorems --json
    """
    # Stub implementation
    result = {
        "meta": {
            "query": q,
            "scope": scope,
            "type_filter": type_filter or "all",
            "k": k,
            "status": "not_implemented",
        },
        "items": [],
        "errors": [
            {
                "code": "NOT_IMPLEMENTED",
                "message": "Search is a stub. Implementation pending curate pipeline.",
            }
        ],
    }

    if output_json:
        print(json.dumps(result, indent=2))
    else:
        typer.echo(f"search: '{q}' in scope={scope}, type={type_filter or 'all'} (stub - not implemented)")

    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
