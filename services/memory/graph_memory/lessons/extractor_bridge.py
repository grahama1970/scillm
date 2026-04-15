from __future__ import annotations
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict

import typer

app = typer.Typer(add_completion=False)

# Force group semantics so tests can invoke with the explicit
# subcommand token ("run") via CliRunner without parse errors.
@app.command("_noop", hidden=True)
def _noop():
    """No-op hidden command."""
    pass



def _default_results_dir() -> Path:
    return Path(os.getenv("EXTRACTOR_RESULTS") or (Path.home() / ".cache" / "graph-memory" / "extractor_results"))


def _extractor_src() -> Path:
    return Path(os.getenv("EXTRACTOR_ROOT", "/opt/extractor/src"))


def _json_out(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


@app.command()
def run(
    pdf: str = typer.Option(..., help="Absolute path to the input PDF"),
    results: str = typer.Option(None, help="Directory to write extractor results (default under extractor/data/results)"),
    mode: str = typer.Option("fast", help="Extraction mode: fast (pipeline-happy) or accurate (full pipeline)", show_default=True),
    dry_run: bool = typer.Option(False, help="Do not execute; print the command envelope"),
):
    """Shell out to the Extractor pipeline (pipeline-happy) with minimal, deterministic defaults.

    This is an operator tool for validation. It does not change agent-faced behavior.
    """
    pdf_path = Path(pdf).expanduser().resolve()
    if not pdf_path.exists():
        _json_out({"ok": False, "error": f"PDF not found: {pdf_path}"})
        raise typer.Exit(code=2)

    out_dir = Path(results).expanduser().resolve() if results else _default_results_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    extractor_src = _extractor_src()
    if not extractor_src.exists() and not dry_run:
        _json_out({"ok": False, "error": f"Extractor src not found: {extractor_src}", "hint": "Set EXTRACTOR_ROOT to your extractor/src"})
        raise typer.Exit(code=2)

    env = os.environ.copy()
    prev = env.get("PYTHONPATH", ""); env["PYTHONPATH"] = (str(extractor_src) + (":" + prev if prev else ""))
    mode = (mode or os.getenv("EXTRACTOR_MODE", "fast")).strip().lower()
    if mode not in ("fast", "accurate"):
        mode = "fast"

    if mode == "fast":
        cmd = [
            "pipeline-happy",
            "--pdf", str(pdf_path),
            "--results", str(out_dir),
        ]
    else:
        # Accurate: call full pipeline runner; rely on Extractor defaults for quality
        cmd = [
            "python", "-m", "extractor.pipeline.run_all", "run",
            "--pdf", str(pdf_path),
            "--results", str(out_dir),
        ]

    if dry_run:
        _json_out({
            "ok": True,
            "dry_run": True,
            "cmd": " ".join(shlex.quote(c) for c in cmd),
            "env": {"PYTHONPATH": env.get("PYTHONPATH", "")},
            "results": str(out_dir),
            "mode": mode,
        })
        return

    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
        ok = (proc.returncode == 0)
        payload: Dict[str, Any] = {
            "ok": ok,
            "returncode": proc.returncode,
            "stdout_tail": "\n".join(proc.stdout.splitlines()[-40:]) if proc.stdout else "",
            "stderr_tail": "\n".join(proc.stderr.splitlines()[-40:]) if proc.stderr else "",
            "results": str(out_dir),
            "mode": mode,
        }
        _json_out(payload)
        raise typer.Exit(code=0 if ok else 1)
    except FileNotFoundError:
        _json_out({"ok": False, "error": "pipeline-happy not found on PATH", "hint": "Install extractor CLI and ensure it's on PATH"})
        raise typer.Exit(code=127)
