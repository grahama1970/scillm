from __future__ import annotations
import time
import uuid
from typing import Any, Dict, List, Optional
from loguru import logger

from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view


def _episodes_col():
    db = get_db()
    try:
        return db.collection("episodes")
    except Exception as exc:
        logger.error("Suppressed error in episodes_store: {}", exc)
        ensure_collections_and_view()
        return db.collection("episodes")


def _steps_col():
    db = get_db()
    try:
        return db.collection("episode_steps")
    except Exception as exc:
        logger.error("Suppressed error in episodes_store: {}", exc)
        ensure_collections_and_view()
        return db.collection("episode_steps")


def add_episode(
    *,
    scope: Optional[str],
    meta: Optional[Dict[str, Any]] = None,
) -> str:
    """Create a minimal episode for GraphWorld runs; returns full id (episodes/<key>)."""
    ensure_collections_and_view()
    col = _episodes_col()
    key = uuid.uuid4().hex[:12]
    ts = int(time.time())
    doc = {
        "_key": key,
        "status": "running",
        "scope": scope or "",
        "created_at": ts,
        "ended_at": None,
        "meta": meta or {},
        "title": "graphops_episode",
    }
    col.insert(doc)
    return f"episodes/{key}"


def end_episode(episode_id: str, summary: Optional[Dict[str, Any]] = None) -> None:
    col = _episodes_col()
    key = episode_id.split("/", 1)[-1]
    try:
        col.update({"_key": key, "status": "done", "ended_at": int(time.time()), "summary": summary or {}})
    except Exception as exc:
        logger.error("Suppressed error in episodes_store: {}", exc)
        # best-effort; do not raise from logging
        pass


def log_step(
    *,
    episode_id: str,
    step_idx: int,
    observation: Dict[str, Any],
    action: Dict[str, Any],
    result: Dict[str, Any],
    tool_meta: Optional[Dict[str, Any]] = None,
) -> str:
    ensure_collections_and_view()
    col = _steps_col()
    ts = int(time.time())
    key = f"{episode_id.split('/',1)[-1]}_{step_idx:04d}"
    doc = {
        "_key": key,
        "episode_id": episode_id,
        "step_idx": int(step_idx),
        "observation": observation,
        "action": action,
        "result": result,
        "tool_meta": tool_meta or {},
        "ts": ts,
    }
    col.insert(doc)
    return f"episode_steps/{key}"


def get_episode_steps(episode_id: str) -> List[Dict[str, Any]]:
    db = get_db()
    query = (
        "FOR s IN episode_steps FILTER s.episode_id==@id SORT s.step_idx ASC RETURN s"
    )
    return list(db.aql.execute(query, bind_vars={"id": episode_id}))

