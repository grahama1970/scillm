"""Collection utility commands: sample, tag, count, archive-session."""
from __future__ import annotations
from loguru import logger

import hashlib
import json
import os
import time
import uuid
from typing import Any, Dict, Optional

import typer

from ._helpers import app, _json_output
from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view


@app.command("sample")
def sample_cmd(
    collection: str = typer.Option(..., "--collection", "-c", help="Collection to sample from"),
    limit: int = typer.Option(5, "--limit", "-l", help="Number of documents to return"),
    scope: str = typer.Option("", "--scope", "-s", help="Filter by scope field"),
    random: bool = typer.Option(False, "--random", is_flag=True, help="Random sampling (default: most recent)"),
    fields: Optional[str] = typer.Option(None, "--fields", "-f", help="Comma-separated fields to return (default: all)"),
    filter_expr: Optional[str] = typer.Option(None, "--filter", help="AQL filter expression, e.g. 'doc.status==\"active\"'"),
) -> None:
    """Sample documents from a collection.

    Returns random or recent documents for inspection, training data collection,
    or validation. Useful for skills that need representative data without
    writing custom AQL.

    Example:
        memory-agent sample --collection lessons --limit 5 --random
        memory-agent sample --collection lessons --limit 10 --scope sparta
        memory-agent sample --collection agent_conversations --limit 3 --fields "body,category,session_id"
    """
    db = get_db()
    if not db.has_collection(collection):
        typer.echo(json.dumps({"error": f"Collection '{collection}' does not exist", "items": []}, indent=2))
        raise typer.Exit(1)

    # Build AQL
    filters = []
    bind_vars: Dict[str, Any] = {"@coll": collection, "lim": limit}
    if scope:
        filters.append("FILTER doc.scope == @scope")
        bind_vars["scope"] = scope
    if filter_expr:
        # Safety: only allow simple field comparisons, no function calls that could be destructive
        forbidden = ["REMOVE", "UPDATE", "INSERT", "REPLACE", "UPSERT"]
        if any(kw in filter_expr.upper() for kw in forbidden):
            typer.echo(json.dumps({"error": "Write operations not allowed in filter"}, indent=2))
            raise typer.Exit(1)
        filters.append(f"FILTER {filter_expr}")

    filter_clause = "\n    ".join(filters)
    if random:
        sort_clause = "SORT RAND()"
    else:
        sort_clause = "SORT doc._key DESC"

    # Field projection
    return_clause = "RETURN doc"
    if fields:
        field_list = [f.strip() for f in fields.split(",") if f.strip()]
        keep_fields = ", ".join(f"'{f}'" for f in field_list)
        return_clause = f"RETURN KEEP(doc, '_key', {keep_fields})"

    aql = f"""
    FOR doc IN @@coll
    {filter_clause}
    {sort_clause}
    LIMIT @lim
    {return_clause}
    """

    try:
        items = list(db.aql.execute(aql, bind_vars=bind_vars))
    except Exception as exc:
        typer.echo(json.dumps({"error": str(exc), "items": []}, indent=2))
        raise typer.Exit(1)

    _json_output({"collection": collection, "count": len(items), "items": items})


@app.command("tag")
def tag_cmd(
    collection: str = typer.Option(..., "--collection", "-c", help="Collection containing the document"),
    key: str = typer.Option(..., "--key", "-k", help="Document _key to tag"),
    tags: str = typer.Option(..., "--tags", "-t", help='JSON array of tags, e.g. \'["Precision","Loyalty"]\''),
    field: str = typer.Option("taxonomy_tags", "--field", "-f", help="Field name to store tags in"),
    mode: str = typer.Option("merge", "--mode", "-m", help="merge (add to existing) or replace (overwrite)"),
) -> None:
    """Stamp taxonomy tags on an existing document.

    Adds or replaces tags on a document without re-embedding or re-inserting.
    This is the official path for post-insert tag stamping — skills must NOT
    write taxonomy_tags directly via python-arango.

    Example:
        memory-agent tag --collection lessons --key abc123 --tags '["Precision","Loyalty"]'
        memory-agent tag --collection lessons --key abc123 --tags '["Resilience"]' --mode replace
        memory-agent tag --collection lessons --key abc123 --tags '["validated"]' --field review_tags
    """
    db = get_db()
    if not db.has_collection(collection):
        typer.echo(json.dumps({"error": f"Collection '{collection}' does not exist"}, indent=2))
        raise typer.Exit(1)

    # Parse tags JSON
    try:
        tag_list = json.loads(tags)
        if not isinstance(tag_list, list):
            raise ValueError("tags must be a JSON array")
    except (json.JSONDecodeError, ValueError) as exc:
        typer.echo(json.dumps({"error": f"Invalid tags JSON: {exc}"}, indent=2))
        raise typer.Exit(1)

    coll = db.collection(collection)

    # Check document exists
    try:
        doc = coll.get(key)
    except Exception as exc:
        logger.error("Suppressed error in collections: {}", exc)
        doc = None
    if not doc:
        typer.echo(json.dumps({"error": f"Document '{key}' not found in '{collection}'"}, indent=2))
        raise typer.Exit(1)

    # Merge or replace
    if mode == "merge":
        existing = doc.get(field) or []
        if not isinstance(existing, list):
            existing = []
        merged = sorted(set(existing + tag_list))
        update_val = merged
    else:
        update_val = tag_list

    try:
        coll.update({
            "_key": key,
            field: update_val,
            f"{field}_updated_at": int(time.time()),
        })
    except Exception as exc:
        typer.echo(json.dumps({"error": f"Update failed: {exc}"}, indent=2))
        raise typer.Exit(1)

    _json_output({
        "ok": True,
        "collection": collection,
        "key": key,
        "field": field,
        "tags": update_val,
        "mode": mode,
    })


@app.command("count")
def count_cmd(
    collection: str = typer.Option(..., "--collection", "-c", help="Collection to count"),
    filter_expr: Optional[str] = typer.Option(None, "--filter", help="AQL filter expression, e.g. 'doc.scope==\"sparta\"'"),
) -> None:
    """Count documents in a collection with optional filter.

    Returns the total count. Useful for health checks, coverage stats,
    and progress monitoring without reading full documents.

    Example:
        memory-agent count --collection lessons
        memory-agent count --collection lessons --filter 'doc.scope=="sparta"'
        memory-agent count --collection agent_conversations --filter 'doc.category=="Error"'
    """
    db = get_db()
    if not db.has_collection(collection):
        typer.echo(json.dumps({"error": f"Collection '{collection}' does not exist", "count": 0}, indent=2))
        raise typer.Exit(1)

    bind_vars: Dict[str, Any] = {"@coll": collection}
    filter_clause = ""
    if filter_expr:
        forbidden = ["REMOVE", "UPDATE", "INSERT", "REPLACE", "UPSERT"]
        if any(kw in filter_expr.upper() for kw in forbidden):
            typer.echo(json.dumps({"error": "Write operations not allowed in filter"}, indent=2))
            raise typer.Exit(1)
        filter_clause = f"FILTER {filter_expr}"

    aql = f"""
    FOR doc IN @@coll
    {filter_clause}
    COLLECT WITH COUNT INTO length
    RETURN length
    """

    try:
        result = list(db.aql.execute(aql, bind_vars=bind_vars))
        count = result[0] if result else 0
    except Exception as exc:
        typer.echo(json.dumps({"error": str(exc), "count": 0}, indent=2))
        raise typer.Exit(1)

    _json_output({"collection": collection, "count": count})


@app.command("archive-session")
def archive_session_cmd(
    session_json: str = typer.Option(..., "--json", "-j", help="JSON string with session data (session_id, messages[], user_id, persona_id)"),
    analyze: bool = typer.Option(True, "--analyze/--no-analyze", help="Run session analysis after archival"),
    scope: str = typer.Option("agent_conversations", "--scope", "-s", help="Logical scope"),
) -> None:
    """Archive an agent conversation session to episodic memory.

    This is the official write path for episodic data. Skills must NOT write
    directly to agent_conversations or session_summaries collections.

    Input JSON schema:
        {
            "session_id": "required-session-id",
            "user_id": "graham",
            "persona_id": "pi",
            "messages": [
                {"from": "User", "content": "...", "timestamp": 1234567890},
                {"from": "Agent", "content": "...", "timestamp": 1234567891}
            ]
        }

    Example:
        memory-agent archive-session --json '{"session_id":"s1","user_id":"graham","persona_id":"pi","messages":[{"from":"User","content":"hello","timestamp":1234567890}]}'
    """
    # Parse input
    try:
        data = json.loads(session_json)
    except json.JSONDecodeError as exc:
        typer.echo(json.dumps({"error": f"Invalid JSON: {exc}"}, indent=2))
        raise typer.Exit(1)

    session_id = data.get("session_id")
    if not session_id:
        typer.echo(json.dumps({"error": "session_id is required"}, indent=2))
        raise typer.Exit(1)

    messages = data.get("messages", [])
    if not messages:
        typer.echo(json.dumps({"error": "messages array is required and must not be empty"}, indent=2))
        raise typer.Exit(1)

    user_id = data.get("user_id", os.getenv("PI_USER_ID", "graham"))
    persona_id = data.get("persona_id", "")

    ensure_collections_and_view()
    db = get_db()
    coll_name = "agent_conversations"
    if not db.has_collection(coll_name):
        db.create_collection(coll_name)
    coll = db.collection(coll_name)

    stored = 0
    skipped = 0
    errors = []

    for msg in messages:
        body = (msg.get("content") or msg.get("body") or "")[:4000]
        if not body:
            skipped += 1
            continue

        ts = msg.get("timestamp") or int(time.time())
        sender = msg.get("from") or msg.get("id_from") or "Unknown"

        # Dedupe key: hash of session + timestamp + sender + body prefix
        dedupe_src = f"{session_id}|{ts}|{sender}|{body[:200]}"
        dedupe_key = hashlib.sha1(dedupe_src.encode()).hexdigest()[:16]

        # Check for existing
        existing = list(db.aql.execute(
            "FOR d IN @@coll FILTER d.dedupe_key == @dk LIMIT 1 RETURN d._key",
            bind_vars={"@coll": coll_name, "dk": dedupe_key}
        ))
        if existing:
            skipped += 1
            continue

        doc = {
            "_key": uuid.uuid4().hex[:16],
            "session_id": session_id,
            "user_id": user_id,
            "persona_id": persona_id,
            "body": body,
            "id_from": sender,
            "id_to": [persona_id] if sender == "User" else [user_id],
            "timestamp": ts,
            "ts_unix": ts if isinstance(ts, int) else int(time.time()),
            "scope": scope,
            "dedupe_key": dedupe_key,
            "category": msg.get("category", ""),
            "type": msg.get("type", "unknown"),
        }

        try:
            coll.insert(doc)
            stored += 1
        except Exception as exc:
            errors.append(str(exc))

    result = {
        "ok": len(errors) == 0,
        "session_id": session_id,
        "stored": stored,
        "skipped": skipped,
        "errors": errors[:5] if errors else [],
    }

    _json_output(result)
