from __future__ import annotations
import json
import os
from typing import List, Dict
import typer
from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view
from loguru import logger

app = typer.Typer(add_completion=False)


def _read_list_file(path: str) -> List[str]:
    items: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s:
                items.append(s)
    return items


def _upsert_control(db, scope: str, source: str, external_id: str = "", url: str = "") -> Dict[str, str]:
    # Compute a stable title; prefer external_id when present
    if external_id:
        title = f"CONTROL {source}:{external_id}"
    elif url:
        title = f"CONTROL {source}:{os.path.basename(url)}"
    else:
        title = f"CONTROL {source}:item"
    tags = ["control", f"source:{source}"]
    if external_id:
        tags.append(f"external_id:{external_id}")
    if url:
        tags.append("has_url")
    aql = (
        "UPSERT { title:@t, scope:@s } "
        "INSERT { title:@t, scope:@s, tags:@tags, source:@src, external_id:@eid, external_url:@url, created_at:@ts, updated_at:@ts } "
        "UPDATE { tags:@tags, source:@src, external_id:@eid, external_url:@url, updated_at:@ts } IN lessons_v2 RETURN NEW"
    )
    now = int(__import__("time").time())
    row = list(
        db.aql.execute(
            aql,
            bind_vars={
                "t": title,
                "s": scope or "",
                "tags": tags,
                "src": source,
                "eid": external_id or "",
                "url": url or "",
                "ts": now,
            },
        )
    )
    return row[0] if row else {"title": title}


@app.command("import")
def import_controls(
    source: str = typer.Option(..., help="mitre-attack|nist80053"),
    scope: str = typer.Option("", help="Scope tag to apply (e.g., sparta)"),
    ids_file: str = typer.Option("", help="Path to file with control IDs (one per line) for MITRE"),
    urls_file: str = typer.Option("", help="Path to file with control URLs (one per line) for NIST"),
    ids_json: str = typer.Option("", help="JSON array of IDs (MCP)"),
    urls_json: str = typer.Option("", help="JSON array of URLs (MCP)"),
    json_out: bool = typer.Option(True, "--json"),
):
    """Create/update lesson nodes for specified control IDs/URLs (no crawling)."""
    ensure_collections_and_view()
    db = get_db()
    src = source.strip().lower()
    if src not in {"mitre-attack", "nist80053"}:
        typer.echo("invalid source; expected mitre-attack|nist80053")
        raise typer.Exit(2)
    items: List[str] = []
    if ids_file and os.path.exists(ids_file):
        items.extend(_read_list_file(ids_file))
    if urls_file and os.path.exists(urls_file):
        items.extend(_read_list_file(urls_file))
    if ids_json:
        try:
            items.extend(json.loads(ids_json))
        except Exception as exc:
            logger.error("import_controls JSON parse failed: {exc}", exc=exc)
    if urls_json:
        try:
            items.extend(json.loads(urls_json))
        except Exception as exc:
            logger.error("import_controls JSON parse failed: {exc}", exc=exc)
    if not items:
        typer.echo("no items provided")
        raise typer.Exit(0)
    wrote: List[Dict[str, str]] = []
    for it in items:
        it = (it or "").strip()
        if not it:
            continue
        if src == "mitre-attack":
            wrote.append(_upsert_control(db, scope, src, external_id=it))
        else:
            wrote.append(_upsert_control(db, scope, src, url=it))
    if json_out:
        print(json.dumps({"meta": {"ok": True, "source": src, "count": len(wrote)}, "items": wrote, "errors": []}))
    else:
        typer.echo(f"imported {len(wrote)} controls for {src}")

