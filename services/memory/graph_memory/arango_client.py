from __future__ import annotations
import os
import threading
from .env import arango_cfg
from arango import ArangoClient  # type: ignore
from loguru import logger

# Singleton: one ArangoClient per (url, db_name) — reuses HTTP connection pool.
_lock = threading.Lock()
_clients: dict[str, ArangoClient] = {}
_dbs: dict[str, object] = {}
_db_ensured: set[str] = set()


def get_db():
    """
    Connect to ArangoDB using environment variables.

    Returns a cached database handle that reuses the underlying HTTP
    connection pool.  Safe for concurrent use from multiple threads
    (python-arango sessions are thread-safe).

    Required env:
      - ARANGO_URL (e.g., http://127.0.0.1:8529)
      - ARANGO_DB (e.g., memory)
      - ARANGO_USER / ARANGO_PASS
    """
    cfg = arango_cfg()
    url = cfg["url"]
    db_name = cfg["db"]
    user = cfg["user"]
    password = cfg["password"]

    cache_key = f"{url}|{db_name}|{user}"

    # Fast path — already cached (no lock needed for read of immutable ref)
    cached = _dbs.get(cache_key)
    if cached is not None:
        return cached

    with _lock:
        # Double-check inside lock
        cached = _dbs.get(cache_key)
        if cached is not None:
            return cached

        # Reuse or create the HTTP client (one per host)
        client = _clients.get(url)
        if client is None:
            client = ArangoClient(hosts=url)
            _clients[url] = client

        # Ensure database exists (once per process)
        if db_name not in _db_ensured:
            sys_db = client.db("_system", username=user, password=password)
            if not sys_db.has_database(db_name):
                sys_db.create_database(db_name)
            _db_ensured.add(db_name)

        db = client.db(db_name, username=user, password=password)

        # Optional invariant fix (off by default)
        if os.getenv("GM_DB_INVARIANT_FIX") in ("1", "true", "TRUE"):
            try:
                db.aql.execute(
                    """
                    FOR e IN lesson_edges
                      LET lf = DOCUMENT('lessons', SPLIT(e._from,'/')[1])
                      FILTER lf!=null AND lf.scope=='llm_scope' AND e.approved==true AND (e.llm_rationale==null OR e.llm_rationale=='')
                      UPDATE e WITH { llm_rationale: 'auto-approved' } IN lesson_edges
                    """
                )
            except Exception as exc:
                logger.error("Suppressed error in arango_client: {}", exc)

        _dbs[cache_key] = db
        return db


def get_steering_prior(user_id: str) -> Dict[str, Any] | None:
    """Retrieve steering priors for a user from ArangoDB."""
    db = get_db()
    try:
        res = list(db.aql.execute(
            "FOR p IN steering_priors FILTER p.user_id == @uid RETURN p",
            bind_vars={"uid": user_id}
        ))
        return res[0] if res else None
    except Exception as exc:
        logger.error("Suppressed error in arango_client: {}", exc)
        return None


def save_steering_prior(user_id: str, prior_data: Dict[str, Any]) -> bool:
    """Save/Update steering priors for a user in ArangoDB."""
    db = get_db()
    try:
        import time
        ts = int(time.time())
        doc = {**prior_data, "user_id": user_id, "updated_at": ts}
        db.aql.execute(
            "UPSERT { user_id: @uid } INSERT @doc UPDATE @doc IN steering_priors",
            bind_vars={"uid": user_id, "doc": doc}
        )
        return True
    except Exception as exc:
        logger.error("Suppressed error in arango_client: {}", exc)
        return False
