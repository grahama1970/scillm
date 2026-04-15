from __future__ import annotations
import time
import json
import typer
from typing import Optional

from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view
from ..events import log_event
from loguru import logger


app = typer.Typer(add_completion=False)


@app.command("add")
def add_request(
    agent_from: str = typer.Option(..., help="Sender agent id/name"),
    agent_to: str = typer.Option(..., help="Recipient agent id/name"),
    request: str = typer.Option(..., help="Free-text request payload"),
    thread_id: str = typer.Option("", help="Optional thread/group id"),
    json_out: bool = typer.Option(False, "--json", is_flag=True, help="Output JSON envelope"),
):
    ensure_collections_and_view()
    db = get_db()
    ts = int(time.time())
    doc = {
        "agent_from": agent_from,
        "agent_to": agent_to,
        "request": request,
        "status": "open",
        "thread_id": thread_id or "",
        "created_at": ts,
        "updated_at": ts,
        "acknowledged_at": None,
    }
    res = db.collection("agent_requests").insert(doc)
    try:
        log_event(db, 'agent_request_add', f"{agent_from}→{agent_to}", {'thread_id': thread_id or '', 'key': res.get('_key')})
    except Exception as exc:
        logger.error("add_request log_event failed: {exc}", exc=exc)
    out = {"meta": {"ok": True}, "items": [{"_id": res.get("_id"), "_key": res.get("_key")}], "errors": []}
    if json_out:
        typer.echo(json.dumps(out, ensure_ascii=False))


@app.command("list")
def list_requests(
    agent_to: str = typer.Option("", help="Filter by recipient"),
    status: str = typer.Option("open", help="open|acknowledged|any"),
    limit: int = typer.Option(20, help="Max items"),
    json_out: bool = typer.Option(False, "--json", is_flag=True, help="Output JSON envelope"),
):
    ensure_collections_and_view()
    db = get_db()
    aql = [
        "FOR r IN agent_requests",
    ]
    binds = {}
    conds = []
    if agent_to:
        conds.append("r.agent_to==@to"); binds["to"] = agent_to
    if status and status != "any":
        conds.append("r.status==@st"); binds["st"] = status
    if conds:
        aql.append("FILTER " + " AND ".join(conds))
    aql.append("SORT r.created_at DESC LIMIT @lim RETURN r")
    binds["lim"] = max(1, int(limit))
    rows = list(db.aql.execute(" ".join(aql), bind_vars=binds))
    out = {"meta": {"count": len(rows)}, "items": rows, "errors": []}
    if json_out:
        typer.echo(json.dumps(out, ensure_ascii=False))


@app.command("ack")
def ack_request(
    key: str = typer.Option(..., help="agent_requests/_key or full _id"),
    json_out: bool = typer.Option(False, "--json", is_flag=True, help="Output JSON envelope"),
):
    ensure_collections_and_view()
    db = get_db()
    ts = int(time.time())
    # Normalize id
    _id = key if "/" in key else f"agent_requests/{key}"
    try:
        db.aql.execute("LET d=DOCUMENT(@id) UPDATE d WITH { status:'acknowledged', acknowledged_at:@ts, updated_at:@ts } IN agent_requests", bind_vars={"id": _id, "ts": ts})
        try:
            log_event(db, 'agent_request_ack', _id, {'key': _id})
        except Exception as exc:
            logger.error("ack_request log_event failed: {exc}", exc=exc)
        out = {"meta": {"ok": True}, "items": [{"_id": _id, "status": "acknowledged"}], "errors": []}
    except Exception as e:
        out = {"meta": {"ok": False}, "items": [], "errors": [str(e)]}
    if json_out:
        typer.echo(json.dumps(out, ensure_ascii=False))


@app.command("claim-next")
def claim_next(
    agent_to: str = typer.Option(..., help="Recipient id/name"),
    status: str = typer.Option("open", help="Only claim from this status (default: open)"),
    json_out: bool = typer.Option(False, "--json", is_flag=True, help="Output JSON envelope"),
):
    """Atomically claim the oldest open request for a recipient by setting status=acknowledged and returning the doc."""
    ensure_collections_and_view()
    db = get_db()
    ts = int(time.time())
    try:
        rows = list(db.aql.execute(
            """
            FOR r IN agent_requests
              FILTER r.agent_to==@to AND (@st=='any' ? true : r.status==@st)
              SORT r.created_at ASC
              LIMIT 1
              UPDATE r WITH { status: 'acknowledged', acknowledged_at: @ts, updated_at: @ts } IN agent_requests
              RETURN NEW
            """,
            bind_vars={'to': agent_to, 'st': status or 'open', 'ts': ts}
        ))
        item = rows[0] if rows else None
        if item:
            try:
                log_event(db, 'agent_request_claim', f"{agent_to}", {'key': item.get('_key')})
            except Exception as exc:
                logger.error("claim_next log_event failed: {exc}", exc=exc)
        out = { 'meta': { 'ok': bool(item) }, 'items': ([item] if item else []), 'errors': ([] if item else ['none']) }
    except Exception as e:
        out = { 'meta': { 'ok': False }, 'items': [], 'errors': [str(e)] }
    if json_out:
        typer.echo(json.dumps(out, ensure_ascii=False))
