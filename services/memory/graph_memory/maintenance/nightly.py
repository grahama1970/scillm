"""Nightly memory maintenance orchestrator.

Runs the full ingestion pipeline for all enabled projects:
1. Codebase ingestion (treesitter)
2. Document extraction (markdown, PDF)
3. Embedding generation (384d)
4. Edge verification (KNN + LLM)

Usage:
    # Run all enabled projects
    uv run python -m graph_memory.maintenance.nightly run

    # Run specific project
    uv run python -m graph_memory.maintenance.nightly run --project memory

    # Dry run (show what would happen)
    uv run python -m graph_memory.maintenance.nightly run --dry-run

    # Show status
    uv run python -m graph_memory.maintenance.nightly status
"""
from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table

from graph_memory.maintenance.project_config import (
    PROJECTS,
    ProjectConfig,
    get_enabled_projects,
    get_project,
)

app = typer.Typer()
console = Console()

# Log directory
LOG_DIR = Path.home() / ".local" / "state" / "memory"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str, project: str = "global"):
    """Log message to console and file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{project}] {msg}"
    console.print(line)

    log_file = LOG_DIR / f"nightly-{datetime.now().strftime('%Y-%m-%d')}.log"
    with open(log_file, "a") as f:
        f.write(line + "\n")


def run_stage(
    name: str,
    cmd: List[str],
    project: ProjectConfig,
    dry_run: bool = False,
    timeout: int = 1800,  # 30 min default
) -> bool:
    """Run a pipeline stage."""
    log(f"Stage: {name}", project["name"])

    if dry_run:
        log(f"  [DRY RUN] Would run: {' '.join(cmd)}", project["name"])
        return True

    try:
        env = os.environ.copy()
        env["ARANGO_DB"] = project["database"]
        env["PYTHONPATH"] = str(Path(project["path"]) / "src")

        result = subprocess.run(
            cmd,
            cwd=project["path"],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        if result.returncode == 0:
            log(f"  ✓ {name} completed", project["name"])
            return True
        else:
            log(f"  ✗ {name} failed: {result.stderr[:200]}", project["name"])
            return False

    except subprocess.TimeoutExpired:
        log(f"  ✗ {name} timed out after {timeout}s", project["name"])
        return False
    except Exception as e:
        log(f"  ✗ {name} error: {e}", project["name"])
        return False


def ingest_project(project: ProjectConfig, dry_run: bool = False) -> dict:
    """Run full ingestion pipeline for a project."""
    results = {"project": project["name"], "stages": {}}
    start = time.time()

    log(f"Starting ingestion for {project['name']}", project["name"])
    log(f"  Path: {project['path']}", project["name"])
    log(f"  Database: {project['database']}", project["name"])
    log(f"  Scope: {project['scope']}", project["name"])

    # Stage 1: Code ingestion
    if project["ingest_code"]:
        cmd = [
            "uv", "run", "lessons-codebase", "curate",
            project["path"],
            "--scope", project["scope"],
            "--max-files", str(project["max_files"]),
        ]
        results["stages"]["ingest_code"] = run_stage("Code Ingestion", cmd, project, dry_run)
    else:
        results["stages"]["ingest_code"] = None

    # Stage 2: Embedding
    if project["embed"]:
        cmd = [
            "uv", "run", "python", "-m", "graph_memory.maintenance.embed_all",
            "embed", "--batch-size", "64",
        ]
        results["stages"]["embed"] = run_stage("Embedding", cmd, project, dry_run)
    else:
        results["stages"]["embed"] = None

    # Stage 3: Edge verification
    if project["verify_edges"] and project["max_llm_calls"] > 0:
        cmd = [
            str(Path(project["path"]) / ".agents" / "skills" / "edge-verifier" / "run.sh"),
            "--batch",
            "--limit", str(project["max_llm_calls"]),
            "--scope", project["scope"],
        ]
        results["stages"]["verify_edges"] = run_stage("Edge Verification", cmd, project, dry_run, timeout=3600)
    else:
        results["stages"]["verify_edges"] = None

    elapsed = time.time() - start
    results["elapsed_s"] = round(elapsed, 1)
    log(f"Completed {project['name']} in {elapsed:.1f}s", project["name"])

    return results


@app.command()
def run(
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Specific project to run"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen"),
    sequential: bool = typer.Option(True, "--sequential/--parallel", help="Run projects sequentially"),
):
    """Run nightly ingestion pipeline."""
    if project:
        proj = get_project(project)
        if not proj:
            console.print(f"[red]Unknown project: {project}[/red]")
            raise typer.Exit(1)
        projects = [proj]
    else:
        projects = get_enabled_projects()

    if not projects:
        console.print("[yellow]No enabled projects found[/yellow]")
        return

    log(f"Starting nightly run for {len(projects)} project(s)")
    all_results = []

    for proj in projects:
        results = ingest_project(proj, dry_run)
        all_results.append(results)

    # Summary
    log("=" * 60)
    log("SUMMARY")
    for r in all_results:
        stages_ok = sum(1 for v in r["stages"].values() if v is True)
        stages_total = sum(1 for v in r["stages"].values() if v is not None)
        log(f"  {r['project']}: {stages_ok}/{stages_total} stages OK, {r['elapsed_s']}s")


@app.command()
def status():
    """Show project configuration status."""
    table = Table(title="Memory Project Configuration")
    table.add_column("Project", style="cyan")
    table.add_column("Path")
    table.add_column("Database")
    table.add_column("Enabled")
    table.add_column("Code")
    table.add_column("Docs")
    table.add_column("Lean")
    table.add_column("Embed")
    table.add_column("Verify")
    table.add_column("Max LLM")

    for p in PROJECTS:
        table.add_row(
            p["name"],
            p["path"][-30:] if len(p["path"]) > 30 else p["path"],
            p["database"],
            "✓" if p["enabled"] else "✗",
            "✓" if p["ingest_code"] else "✗",
            "✓" if p["ingest_docs"] else "✗",
            "✓" if p["ingest_lean"] else "✗",
            "✓" if p["embed"] else "✗",
            "✓" if p["verify_edges"] else "✗",
            str(p["max_llm_calls"]),
        )

    console.print(table)


if __name__ == "__main__":
    app()
