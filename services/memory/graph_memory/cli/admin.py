"""Admin commands: sparta-status, operator, assessment."""
from __future__ import annotations

import json
import time
from typing import Optional

import typer

from ._helpers import app, _json_output


@app.command("sparta-status")
def sparta_status(
    run_id: str = typer.Option("", "--run-id", "-r", help="Specific run ID (latest if empty)"),
    json_out: bool = typer.Option(False, "--json", "-j", help="JSON output"),
    history: int = typer.Option(5, "--history", "-n", help="Number of checkpoints to show"),
) -> None:
    """Show live SPARTA pipeline state from ArangoDB (lock-free).

    Reads metrics pushed by Stage 12 heartbeat — no DuckDB lock needed.

    Example:
        memory-agent sparta-status
        memory-agent sparta-status --json
        memory-agent sparta-status --run-id run-recovery-verify --history 10
    """
    from ..sparta_metrics import SpartaMetricsBridge

    bridge = SpartaMetricsBridge()
    state = bridge.get_live_state(run_id) if run_id else bridge.get_latest_live_state()

    if state is None:
        if json_out:
            _json_output({"error": "no_data", "message": "No SPARTA metrics found"})
        else:
            typer.echo("[sparta] No SPARTA metrics found in ArangoDB.", err=True)
            typer.echo("  Metrics are pushed by Stage 12 heartbeat.", err=True)
        raise typer.Exit(1)

    rid = state.get("run_id", run_id)
    checkpoints = bridge.get_convergence_history(rid, limit=history) if rid else []

    if json_out:
        _json_output({
            "live_state": state,
            "checkpoints": checkpoints,
        })
        return

    # Human-readable output
    import datetime
    updated = state.get("updated_at", 0)
    age_s = int(time.time()) - updated if updated else 0
    age_str = f"{age_s}s ago" if age_s < 120 else f"{age_s // 60}m ago"

    typer.echo(f"\n  SPARTA Pipeline: {rid}")
    typer.echo(f"  Status: {state.get('status', '?')}  |  Updated: {age_str}  |  PID: {state.get('pid', '?')}")
    typer.echo(f"  QRAs: {state.get('qra_count', 0):,}  |  Rate: {state.get('qra_rate_per_min', 0):.1f}/min  |  Batch: {state.get('current_batch_id', 0)}")
    typer.echo(f"  Grounding: {state.get('avg_grounding', 0):.2f}  |  Pass: {state.get('pass_pct', 0):.0%}  Warn: {state.get('warn_pct', 0):.0%}  Fail: {state.get('fail_pct', 0):.0%}")

    if checkpoints:
        typer.echo(f"\n  Checkpoints (last {len(checkpoints)}):")
        for ckpt in checkpoints:
            ts = ckpt.get("created_at", 0)
            ts_str = datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "?"
            verdicts = ckpt.get("brandon_verdicts", {})
            v_str = f"P:{verdicts.get('pass', 0)} W:{verdicts.get('warn', 0)} F:{verdicts.get('fail', 0)}" if verdicts else ""
            typer.echo(f"    {ts_str}  QRA: {ckpt.get('qra_count', 0):>6,}  Deficiencies: {ckpt.get('deficiencies', 0):>4}  {v_str}")

    typer.echo("")


# =============================================================================
# OPERATOR COMMANDS - For maintenance, use lessons-* directly
# =============================================================================


@app.command("operator", hidden=True)
def operator_help() -> None:
    """For operator/maintenance commands, use lessons-* CLIs directly.

    Examples:
        lessons-propose --scope myproject --k 12
        lessons-approve --from-title "A" --to-title "B"
        lessons-status --json
        lessons-graph export --scope myproject --json

    These are NOT needed for normal agent workflow.
    """
    typer.echo("Operator commands available via lessons-* CLIs:")
    typer.echo("  lessons-search    - Advanced search options")
    typer.echo("  lessons-propose   - FAISS edge proposals")
    typer.echo("  lessons-approve   - Approve edges")
    typer.echo("  lessons-reject    - Reject edges")
    typer.echo("  lessons-status    - DB status")
    typer.echo("  lessons-graph     - Graph export")
    typer.echo("  lessons-relate    - Relationship tools")
    typer.echo("")
    typer.echo("Run 'lessons-<cmd> --help' for details.")


@app.command("assessment")
def assessment_cmd(
    project: str = typer.Option(..., "--project", help="Project name"),
    issue_json: str = typer.Option(..., "--issue", help="JSON string of the issue identified"),
    research: str = typer.Option("", "--research", help="Dogpile research report"),
    outcome: str = typer.Option("", "--outcome", help="Code review outcome/diff"),
    status: str = typer.Option("success", "--status", help="Run status"),
    date: Optional[str] = typer.Option(None, "--date", help="ISO date (YYYY-MM-DD)"),
) -> None:
    """Record a nightly assessment run results (research + code review)."""
    from ..api import record_assessment
    try:
        issue = json.loads(issue_json)
    except Exception as e:
        typer.echo(f"Invalid issue JSON: {e}", err=True)
        raise typer.Exit(1)

    result = record_assessment(
        project=project,
        issue=issue,
        research=research,
        review_outcome=outcome,
        status=status,
        date=date
    )
    _json_output(result)
