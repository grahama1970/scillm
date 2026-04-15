"""Peer module for agent conversation messaging functions.

Extracted from api.py to keep modules under 800 lines.
All public functions are re-exported by api.py so existing
``from graph_memory.api import X`` imports continue to work.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List

from loguru import logger

from .arango_client import get_db
from .events import log_event
from .lessons.agent_conversations import _normalize_id_to as _conv_normalize_id_to


# ---------------------------------------------------------------------------
# Agent conversations (cross-agent message bus)
# ---------------------------------------------------------------------------


def add_message(
    id_from: str,
    id_to: List[str] | str,
    body: str,
    topic: str = "",
    run_id: str = "",
    session_id: str = "",
    priority: str = "normal",
    action_required: bool | None = None,
    dedupe_key: str | None = None,
    scope: str = "agent_conversations",
) -> Dict[str, Any]:
    """Persist a cross-agent message and return the stored record."""
    from .setup_schema import ensure_collections_and_view
    import uuid
    from datetime import datetime, timezone
    import hashlib

    ensure_collections_and_view()
    db = get_db()
    ts_unix = int(time.time())
    ts_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    recipients = _conv_normalize_id_to(id_to)
    if not recipients:
        return {"meta": {"ok": False}, "items": [], "errors": ["id_to is required"]}
    topic_trim = (topic or "").strip()
    if not topic_trim:
        return {"meta": {"ok": False}, "items": [], "errors": ["topic required"]}
    if len(topic_trim) > 120:
        return {"meta": {"ok": False}, "items": [], "errors": ["topic too long (max 120)"]}
    if not body:
        return {"meta": {"ok": False}, "items": [], "errors": ["body required"]}
    if len(body) > 4000:
        return {"meta": {"ok": False}, "items": [], "errors": ["body too long (max 4000 chars)"]}
    key = uuid.uuid4().hex[:16]
    if not dedupe_key:
        h = hashlib.sha1()
        h.update(id_from.encode())
        h.update("|".join(sorted(recipients)).encode())
        h.update(topic_trim.encode())
        h.update(body[:200].encode())
        h.update(str(len(body)).encode())
        dedupe_key = h.hexdigest()
    # Auto-taxonomy extraction (Federated Taxonomy)
    bridge_attributes: List[str] = []
    try:
        from .lessons.store import extract_bridges_fast
        bridge_attributes = extract_bridges_fast(f"{topic_trim} {body}")
    except Exception as exc:
        logger.error("taxonomy extraction for conversation failed: {}", exc)

    all_tags: List[str] = []  # agent_conversations have no user tags; bridges go in bridge_attributes

    doc = {
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
        "priority": (priority or "normal").lower() if priority else "normal",
        "action_required": bool(action_required) if action_required is not None else False,
        "body": body,
        "tags": all_tags,
        "bridge_attributes": bridge_attributes,
        "acks": {},
        "dedupe_key": dedupe_key,
        "acked_by": [],
        "expires_at": ts_unix + 30 * 86400,
    }
    # Best-effort dedupe (return existing if same dedupe_key)
    try:
        existing = list(db.aql.execute(
            "FOR m IN agent_conversations FILTER m.dedupe_key==@dk SORT m.ts_unix DESC LIMIT 1 RETURN m",
            bind_vars={"dk": dedupe_key},
            batch_size=1,
        ))
        if existing:
            for r in existing:
                r.setdefault("id", r.get("_id"))
                r.setdefault("acked_by", sorted(list((r.get("acks") or {}).keys())))
            return {"meta": {"ok": True, "deduped": True}, "items": existing, "errors": []}
    except Exception as exc:
        logger.error("conversation dedup check failed: {}", exc)
    res = db.collection("agent_conversations").insert(doc)
    doc["_id"] = res.get("_id", doc["_id"])
    doc["id"] = doc["_id"]
    try:
        log_event(db, "agent_conversation_add", f"{id_from}→{','.join(recipients)}", {"topic": topic or "", "key": doc.get('_key')})
    except Exception as exc:
        logger.error("log_event for conversation_add failed: {}", exc)
    return {"meta": {"ok": True}, "items": [doc], "errors": []}


def list_messages(
    id_to: str,
    topic: str | None = None,
    since_ts: int | str | None = None,
    limit: int = 50,
    offset: int = 0,
    priority: str | None = None,
    action_required: bool | None = None,
    scope: str = "agent_conversations",
) -> Dict[str, Any]:
    """List messages for a recipient (or broadcasts to 'all'), newest first."""
    from datetime import datetime
    from .setup_schema import ensure_collections_and_view

    ensure_collections_and_view()
    db = get_db()
    since_val: int | None = None
    if since_ts is not None and since_ts != "":
        try:
            since_val = int(float(since_ts))
        except Exception as exc:
            logger.error("since_ts numeric parse failed: {}", exc)
            try:
                since_val = int(datetime.fromisoformat(str(since_ts).replace("Z", "+00:00")).timestamp())
            except Exception as exc2:
                logger.error("since_ts ISO parse failed: {}", exc2)
                since_val = None
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
        bind_vars["priority"] = (priority or "").lower()
    if since_val is not None:
        filters.append("m.ts_unix>=@since")
        bind_vars["since"] = int(since_val)
    if action_required is True:
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
    for r in rows:
        r.setdefault("id", r.get("_id"))
        r.setdefault("acks", r.get("acks") or {})
        r["acked_by"] = sorted(list((r.get("acks") or {}).keys()))
    return {"meta": {"count": len(rows)}, "items": rows, "errors": []}


def ack_message(id: str, agent: str) -> Dict[str, Any]:
    """Mark a conversation message as acknowledged by an agent."""
    from datetime import datetime, timezone
    from .setup_schema import ensure_collections_and_view

    ensure_collections_and_view()
    db = get_db()
    ts_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
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
        if ok:
            try:
                log_event(db, "agent_conversation_ack", doc_id, {"agent": agent})
            except Exception as exc:
                logger.error("log_event for conversation_ack failed: {}", exc)
            item["acked_by"] = sorted(list((item.get("acks") or {}).keys()))
        return {"meta": {"ok": ok}, "items": ([item] if item else []), "errors": ([] if ok else ["not_found"])}
    except Exception as exc:
        return {"meta": {"ok": False}, "items": [], "errors": [str(exc)]}
