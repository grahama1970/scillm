"""Promote a trained bundle to the current symlink.

Creates/updates `local/seal/models/<scope>/current` -> <bundle> (dir or file).
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import typer

app = typer.Typer(add_completion=False)

@app.command()
def main(
    bundle: Path = typer.Option(..., exists=True, help="Path to trained bundle (dir or file)"),
    scope: str = typer.Option(..., help="Scope (used to locate model dir)"),
    model_dir: Path = typer.Option(Path("local/seal/models"), help="Root model dir"),
    json_out: bool = typer.Option(True, "--json"),
):
    target_dir = model_dir / scope
    target_dir.mkdir(parents=True, exist_ok=True)
    link = target_dir / "current"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(bundle.resolve())
    res = {"meta": {"ok": True, "link": str(link), "bundle": str(bundle.resolve())}, "items": [], "errors": []}
    if json_out:
        print(json.dumps(res))
    else:
        print(res)

if __name__ == "__main__":
    app()
