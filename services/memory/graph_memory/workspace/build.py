from __future__ import annotations

import json
from pathlib import Path
import typer

from .detect import detect_workspace
from .ingest import ingest as ingest_cmd
from ..lessons import proposer


app = typer.Typer(add_completion=False)


@app.command()
def build(
    root: Path = typer.Option(None, help="Workspace root (defaults to auto-detect from CWD)"),
    scope: str = typer.Option("", help="Override scope (default workspace:<root>)"),
    exts: str = typer.Option(".md,.mdx,.rst,.txt", help="Extensions to ingest"),
    skip_propose: bool = typer.Option(False, help="Skip FAISS proposer step"),
    k: int = typer.Option(12, help="Proposer K"),
    sim_thresh: float = typer.Option(0.55, help="Proposer sim threshold"),
    min_top: int = typer.Option(3, help="Proposer min-top"),
    dry_run: bool = typer.Option(False, help="Plan only; no writes"),
):
    """One-shot: detect workspace → ingest docs → propose edges (optional)."""

    detection = detect_workspace(root or Path("."))
    ws_root = detection.root
    scope_val = scope or detection.scope

    # Ingest docs (reuses ingest CLI logic); passes through dry_run
    ingest_cmd(
        root=ws_root,
        scope=scope_val,
        exts=exts,
        dry_run=dry_run,
    )

    meta = {"workspace": str(ws_root), "scope": scope_val, "propose": not skip_propose and not dry_run}

    if not skip_propose and not dry_run:
        proposer.propose(
            k=k,
            sim_thresh=sim_thresh,
            min_top=min_top,
            scope=scope_val,
            ids_file="",
            dry_run=False,
            updated_since=0,
            write_limit=0,
        )
    else:
        meta["propose_skipped"] = True

    print(json.dumps({"meta": meta, "errors": []}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()

