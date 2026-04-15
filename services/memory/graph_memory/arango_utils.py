"""Shared Arango helpers for graph_memory and downstream projects.

API (stable):
- key_control(control_id) -> str (stable URL-safe key)
- key_url(url) -> str
- key_chunk(url, control_id, category) -> str
- now_utc_iso() -> str
- get_arango_client() -> (db, err)
- ensure_collections_and_view(db)  (delegates to local setup_schema if available)
- upsert(db, coll, key, doc)
- upsert_edge(db, coll, _from, _to, attrs=None)
"""
from __future__ import annotations

from loguru import logger

try:
    from dotenv import load_dotenv, find_dotenv  # type: ignore
    load_dotenv(find_dotenv(usecwd=True), override=False)
except Exception as exc:
    logger.error("dotenv loading skipped: {}", exc)

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from arango import ArangoClient  # type: ignore


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def key_control(control_id: str) -> str:
    return _sha1(str(control_id))


def key_url(url: str) -> str:
    return _sha1(str(url))


def key_chunk(url: str, control_id: str, category: str) -> str:
    return _sha1(f"{url}::{control_id}::{category}")


def get_arango_client():
    import os

    url = os.getenv("ARANGO_URL", "http://localhost:8529")
    dbname = os.getenv("ARANGO_DB", "graph")
    user = os.getenv("ARANGO_USER", "root")
    pwd = os.getenv("ARANGO_PASS", "")
    try:
        client = ArangoClient(hosts=url)
        db = client.db(dbname, username=user, password=pwd)
        # ping
        _ = db.properties()
        return db, None
    except Exception as e:  # pragma: no cover
        return None, str(e)


def ensure_collections_and_view(db) -> Dict[str, Any]:
    try:  # prefer local schema helper
        from .setup_schema import ensure_collections_and_view as _ensure

        return _ensure(db)  # type: ignore[call-arg]
    except Exception as exc:
        logger.error("local setup_schema unavailable, using fallback: {}", exc)
    doc_cols = [
        "controls",
        "urls",
        "chunks",
        "qra",
    ]
    edge_cols = [
        "edges_ccat",
        "edges_cc",
        "edges_ccat_llm",
        "edges_cc_llm",
        "edges_qra_ev",
        "edges_qra_ctrl",
    ]
    for c in doc_cols:
        try:
            if not db.has_collection(c):
                db.create_collection(c)
        except Exception as exc:
            logger.error("failed to ensure doc collection {}: {}", c, exc)
    for c in edge_cols:
        try:
            if not db.has_collection(c):
                db.create_collection(c, edge=True)
        except Exception as exc:
            logger.error("failed to ensure edge collection {}: {}", c, exc)
    try:
        if not db.has_view("search_chunks"):
            db.create_view(
                name="search_chunks",
                view_type="arangosearch",
                properties={
                    "links": {
                        "chunks": {
                            "analyzers": ["text_en", "norm_en"],
                            "includeAllFields": False,
                            "fields": {"excerpt": {"analyzers": ["text_en", "norm_en"]}},
                        }
                    }
                },
            )
    except Exception as exc:
        logger.error("failed to ensure search_chunks view: {}", exc)
    return {"ok": True}


def upsert(db, coll: str, key: str, doc: Dict[str, Any]) -> None:
    c = db.collection(coll)
    now = now_utc_iso()
    if c.has(key):
        cur = c.get(key)
        cur.update(doc)
        cur["updated_ts"] = now
        c.update(cur)
    else:
        d = dict(doc)
        d["_key"] = key
        d.setdefault("created_ts", now)
        d["updated_ts"] = now
        c.insert(d)


def _find_edge_key(db, coll: str, _from: str, _to: str) -> Optional[str]:
    try:
        cursor = db.aql.execute(
            f"FOR e IN {coll} FILTER e._from == @from AND e._to == @to LIMIT 1 RETURN e._key",
            bind_vars={"from": _from, "to": _to},
        )
        for k in cursor:
            if isinstance(k, str) and k:
                return k
    except Exception as exc:
        logger.error("edge key lookup failed for {} {}->{}:  {}", coll, _from, _to, exc)
    return None


def upsert_edge(db, coll: str, _from: str, _to: str, attrs: Optional[Dict[str, Any]] = None) -> None:
    key = _find_edge_key(db, coll, _from, _to) or _sha1(f"{coll}::{_from}->{_to}")
    doc = {"_from": _from, "_to": _to}
    if attrs:
        doc.update(attrs)
    upsert(db, coll, key, doc)

