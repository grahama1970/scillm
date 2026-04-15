from __future__ import annotations
import json
import time
import uuid
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import typer
from loguru import logger

from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view
from ..events import log_event


app = typer.Typer(add_completion=False)


def _iso_utc_now() -> str:
    """Return a compact RFC3339/ISO8601 UTC timestamp with Z suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_priority(p: str | None) -> str:
    val = (p or "normal").lower()
    return val if val in ("low", "normal", "high") else "normal"


def _normalize_id_to(id_to: Any) -> List[str]:
    if id_to is None:
        return []
    if isinstance(id_to, str):
        cleaned = [s.strip() for s in id_to.split(",") if s.strip()]
        return cleaned or []
    try:
        out: List[str] = []
        for n in id_to:
            s = str(n).strip()
            if s:
                out.append(s)
        return out
    except Exception as exc:
        logger.error("Failed to normalize id_to: {}", exc)
        return []


def _parse_since(since_ts: str | None) -> Optional[int]:
    if not since_ts:
        return None
    try:
        return int(float(since_ts))
    except Exception as exc:
        logger.error("Failed to parse since_ts as float: {}", exc)
    try:
        return int(datetime.fromisoformat(str(since_ts).replace("Z", "+00:00")).timestamp())
    except Exception as exc:
        logger.error("Failed to parse since_ts as ISO datetime: {}", exc)
        return None


def _envelope(item: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy with stable id + acked_by helper."""
    out = dict(item)
    # Arango returns _key/_id; keep a friendly id alias
    if "_id" in out:
        out.setdefault("id", out.get("_id"))
    out["acked_by"] = sorted(list((out.get("acks") or {}).keys()))
    return out


@app.command("add")
def add_message(
    id_from: str = typer.Option(..., "--id-from", help="Sender agent id/name"),
    id_to: List[str] = typer.Option(..., "--id-to", help="Recipient agent id(s) or 'all'. Repeat for multiple."),
    body: str = typer.Option(..., help="Free text payload"),
    topic: str = typer.Option("", help="Short tag/topic"),
    run_id: str = typer.Option("", help="Optional run identifier"),
    session_id: str = typer.Option("", help="Optional session identifier"),
    priority: str = typer.Option("normal", help="Priority: low|normal|high"),
    action_required: bool = typer.Option(False, "--action-required", help="Mark as requiring action"),
    dedupe_key: str = typer.Option("", help="Optional dedupe key; auto-computed when omitted."),
    scope: str = typer.Option("agent_conversations", help="Logical scope (default: agent_conversations)"),
    json_out: bool = typer.Option(False, "--json", is_flag=True, help="Output JSON envelope"),
    dry_run: bool = typer.Option(False, "--dry-run", is_flag=True, help="Plan only; do not write"),
):
    """Add a message to the agent_conversations collection."""
    ensure_collections_and_view()
    db = get_db()

    ts_unix = int(time.time())
    ts_iso = _iso_utc_now()
    key = uuid.uuid4().hex[:16]
    recipients = _normalize_id_to(id_to)
    norm_priority = _normalize_priority(priority)

    # Validations
    topic_trim = (topic or "").strip()
    if not topic_trim:
        out = {"meta": {"ok": False}, "items": [], "errors": ["topic required"]}
        if json_out:
            typer.echo(json.dumps(out, ensure_ascii=False))
        else:
            typer.echo("topic required", err=True)
        raise typer.Exit(1)
    if len(topic_trim) > 120:
        out = {"meta": {"ok": False}, "items": [], "errors": ["topic too long (max 120)"]}
        if json_out:
            typer.echo(json.dumps(out, ensure_ascii=False))
        else:
            typer.echo("topic too long (max 120)", err=True)
        raise typer.Exit(1)
    if not body:
        out = {"meta": {"ok": False}, "items": [], "errors": ["body required"]}
        if json_out:
            typer.echo(json.dumps(out, ensure_ascii=False))
        else:
            typer.echo("body required", err=True)
        raise typer.Exit(1)
    if len(body) > 4000:
        out = {"meta": {"ok": False}, "items": [], "errors": ["body too long (max 4000 chars)"]}
        if json_out:
            typer.echo(json.dumps(out, ensure_ascii=False))
        else:
            typer.echo("body too long (max 4000 chars)", err=True)
        raise typer.Exit(1)

    doc: Dict[str, Any] = {
        "_key": key,
        "_id": f"agent_conversations/{key}",
        "id": f"agent_conversations/{key}",
        "ts": ts_iso,
        "ts_unix": ts_unix,
        "scope": scope or "agent_conversations",
        "id_from": id_from,
        "id_to": recipients,
        "topic": topic_trim,
        "run_id": run_id or "",
        "session_id": session_id or "",
        "priority": norm_priority,
        "action_required": bool(action_required),
        "body": body,
        "acks": {},
        "expires_at": ts_unix + 30*86400,  # 30-day retention
    }

    if not recipients:
        out = {"meta": {"ok": False}, "items": [], "errors": ["id_to is required"]}
        if json_out:
            typer.echo(json.dumps(out, ensure_ascii=False))
        else:
            typer.echo("id_to is required", err=True)
        raise typer.Exit(1)

    # Dedupe (best-effort)
    if not dedupe_key:
        h = hashlib.sha1()
        h.update(id_from.encode())
        h.update("|".join(sorted(recipients)).encode())
        h.update((topic_trim or "").encode())
        h.update(body[:200].encode())
        h.update(str(len(body)).encode())
        dedupe_key = h.hexdigest()
    doc["dedupe_key"] = dedupe_key

    if not dry_run:
        try:
            existing = list(db.aql.execute(
                "FOR m IN agent_conversations FILTER m.dedupe_key==@dk SORT m.ts_unix DESC LIMIT 1 RETURN m",
                bind_vars={"dk": dedupe_key},
                batch_size=1,
            ))
            if existing:
                out = {"meta": {"ok": True, "deduped": True}, "items": [_envelope(existing[0])], "errors": []}
                if json_out:
                    typer.echo(json.dumps(out, ensure_ascii=False))
                else:
                    typer.echo(existing[0].get("_id"))
                return
        except Exception as exc:
            logger.error("Dedupe check failed: {}", exc)
        res = db.collection("agent_conversations").insert(doc)
        doc["_id"] = res.get("_id", doc["_id"])
        doc["id"] = doc["_id"]
        try:
            log_event(db, "agent_conversation_add", f"{id_from}→{','.join(recipients)}", {"topic": topic_trim or "", "key": doc.get("_key")})
        except Exception as exc:
            logger.error("Failed to log agent_conversation_add event: {}", exc)

    out = {"meta": {"ok": True, "dry_run": bool(dry_run)}, "items": [_envelope(doc)], "errors": []}
    if json_out:
        typer.echo(json.dumps(out, ensure_ascii=False))
    else:
        typer.echo(doc.get("_id"))


@app.command("list")
def list_messages(
    id_to: str = typer.Option(..., "--id-to", help="Recipient filter (agent id)"),
    topic: str = typer.Option("", help="Optional topic filter"),
    since_ts: str = typer.Option("", help="Optional lower bound (ISO8601 or unix seconds)"),
    limit: int = typer.Option(50, help="Max rows, newest first"),
    offset: int = typer.Option(0, help="Offset for pagination"),
    priority: str = typer.Option("", help="Optional priority filter: low|normal|high"),
    action_required: bool = typer.Option(False, "--action-required", help="Filter to action-required only"),
    only_action_required: bool = typer.Option(False, "--only-action-required", help="(deprecated) alias for --action-required"),
    scope: str = typer.Option("agent_conversations", help="Scope filter"),
    json_out: bool = typer.Option(False, "--json", is_flag=True, help="Output JSON envelope"),
):
    """List messages for a recipient (or broadcasts to 'all')."""
    ensure_collections_and_view()
    db = get_db()
    since = _parse_since(since_ts)
    filters = [
        "(@scope=='' OR m.scope==@scope)",
        "(@recipient IN m.id_to OR 'all' IN m.id_to)",
    ]
    bind_vars: Dict[str, Any] = {
        "scope": scope or "",
        "recipient": id_to,
        "lim": max(1, int(limit)),
        "off": max(0, int(offset)),
    }
    if topic:
        filters.append("m.topic==@topic")
        bind_vars["topic"] = topic
    if priority:
        filters.append("m.priority==@priority")
        bind_vars["priority"] = _normalize_priority(priority)
    if since is not None:
        filters.append("m.ts_unix>=@since")
        bind_vars["since"] = int(since)
    if action_required or only_action_required:
        filters.append("m.action_required==true")
    query = [
        "FOR m IN agent_conversations",
        "FILTER " + " AND ".join(filters),
        "LET pr = m.priority == 'high' ? 2 : (m.priority == 'low' ? 0 : 1)",
        "SORT pr DESC, m.ts_unix DESC",
        "LIMIT @off, @lim",
        "RETURN KEEP(m, ['_key','_id','id','ts','ts_unix','scope','id_from','id_to','topic','run_id','session_id','priority','action_required','body','acks'])",
    ]
    rows = list(db.aql.execute(" ".join(query), bind_vars=bind_vars))
    out = {"meta": {"count": len(rows), "scope": scope or ""}, "items": [_envelope(r) for r in rows], "errors": []}
    if json_out:
        typer.echo(json.dumps(out, ensure_ascii=False))
    else:
        for r in out["items"]:
            typer.echo(f"{r.get('ts')} {r.get('id_from')}→{','.join(r.get('id_to') or [])} {r.get('topic') or '-'} {r.get('body')[:60]}")


@app.command("ack")
def ack_message(
    id: str = typer.Option(..., "--id", help="agent_conversations/_key or full _id"),
    agent: str = typer.Option(..., "--agent", help="Agent acknowledging"),
    json_out: bool = typer.Option(False, "--json", is_flag=True, help="Output JSON envelope"),
):
    """Mark a message as acknowledged by an agent (idempotent)."""
    ensure_collections_and_view()
    db = get_db()
    ts_iso = _iso_utc_now()
    ts_unix = int(time.time())
    doc_id = id if "/" in id else f"agent_conversations/{id}"
    try:
        rows = list(
            db.aql.execute(
                """
                LET doc = DOCUMENT(@id)
                FILTER doc!=null
                LET merged = MERGE(doc.acks ? doc.acks : {}, { [@agent]: @ts_iso })
                UPDATE doc WITH { acks: merged, updated_at: @ts_unix } IN agent_conversations
                RETURN NEW
                """,
                bind_vars={"id": doc_id, "agent": agent, "ts_iso": ts_iso, "ts_unix": ts_unix},
            )
        )
        item = rows[0] if rows else None
        ok = bool(item)
        errors: List[str] = [] if ok else ["not_found"]
        if ok:
            try:
                log_event(db, "agent_conversation_ack", doc_id, {"agent": agent})
            except Exception as exc:
                logger.error("Failed to log agent_conversation_ack event: {}", exc)
    except Exception as e:  # pragma: no cover - defensive
        item = None
        ok = False
        errors = [str(e)]
    out = {"meta": {"ok": ok}, "items": [_envelope(item)] if item else [], "errors": errors if not ok else []}
    if json_out:
        typer.echo(json.dumps(out, ensure_ascii=False))
    else:
        msg = "acknowledged" if ok else "not found"
        typer.echo(msg)
