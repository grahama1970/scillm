from __future__ import annotations
from loguru import logger

import hashlib
import json
import time
from pathlib import Path
from typing import Iterable, List, Tuple

import typer

from ..arango_client import get_db
from ..arango_utils import upsert
from ..setup_schema import ensure_collections_and_view


app = typer.Typer(add_completion=False)


def _iter_docs(root: Path, exts: Tuple[str, ...]) -> Iterable[Path]:
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        if path.suffix.lower() in exts and not any(part.startswith(".") for part in path.parts):
            yield path


def _segment_markdown(text: str) -> List[str]:
    sections: List[str] = []
    buf: List[str] = []
    for line in text.splitlines():
        if line.startswith("#") and buf:
            sections.append("\n".join(buf).strip())
            buf = []
        buf.append(line)
    if buf:
        sections.append("\n".join(buf).strip())
    return [s for s in sections if s]


def _summarize(text: str, limit: int = 400) -> str:
    t = " ".join(text.split())
    return t[:limit] + ("…" if len(t) > limit else "")


@app.command()
def ingest(
    root: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
    scope: str = typer.Option("", help="Workspace scope (defaults to workspace:<root name>)"),
    exts: str = typer.Option(".md,.mdx,.rst,.txt", help="Comma-separated extensions to ingest"),
    include_from: Path = typer.Option(None, help="Optional file with newline-separated paths to include"),
    dry_run: bool = typer.Option(False, help="Plan only; do not write to Arango"),
):
    """Ingest workspace docs into lessons for hybrid search/graph flows."""

    ensure_collections_and_view()
    db = get_db()
    ts = int(time.time())

    workspace = root.name
    scope_val = scope or f"workspace:{workspace}"
    ext_tuple = tuple(e.strip().lower() for e in exts.split(',') if e.strip())

    allow_set = None
    if include_from:
        allow_set = {Path(line.strip()).resolve() for line in include_from.read_text().splitlines() if line.strip()}

    docs = []
    for p in _iter_docs(root, ext_tuple):
        if allow_set is not None and p.resolve() not in allow_set:
            continue
        docs.append(p)
    wrote = 0
    planned = []

    for path in docs:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as exc:
            logger.error("Suppressed error in ingest: {}", exc)
            continue
        sections = _segment_markdown(text)
        rel = path.relative_to(root)
        key = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()
        doc = {
            "title": path.stem,
            "scope": scope_val,
            "problem": _summarize(text),
            "playbook": "Workspace doc ingestion",
            "chunks": sections,
            "workspace": workspace,
            "source_path": str(rel),
            "source_root": str(root),
            "tags": ["workspace", workspace],
            "status": "active",
            "updated_at": ts,
            "created_at": ts,
        }
        planned.append({"path": str(rel), "scope": scope_val, "key": key})
        if not dry_run:
            upsert(db, "lessons", key, doc)
            wrote += 1

    out = {
        "meta": {
            "root": str(root),
            "workspace": workspace,
            "scope": scope_val,
            "dry_run": dry_run,
            "files": len(docs),
            "written": wrote,
        },
        "items": planned,
        "errors": [],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()
