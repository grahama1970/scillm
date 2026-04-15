"""Graph Memory Service — FastAPI application package.

Assembled from focused router submodules:
  _core.py        — recall, learn, related, residue, trace, edges, clarify, deflect
  _user.py        — user get/update/history
  _persona.py     — persona, relationship, belief (Theory of Mind)
  _sparta.py      — entity extraction + batch QRA validation (CPU-bound)
  _store.py       — /store convenience endpoint
  _intent.py      — /intent classification (classifier + keyword heuristic)
  _tom.py         — GET /tom/{user_id} consolidated endpoint
  _taxonomy.py    — /taxonomy/* coverage, sample, tag, batch-tag
  _drift.py       — /drift teacher-vs-classifier agreement
  _db_audit.py    — /db/audit infrastructure audit
  _analytics.py   — /analytics/run pipeline
  _queryspec_executor.py — /execute-queryspec deterministic executor
  _entities.py     — /extract-entities entity extraction + /create-evidence-case
  _backfill.py    — /backfill embedding backfill (text + visual)
  _matching.py    — /match/* requirement-to-control matching
  _latency.py     — /latency-stats LLM call latency percentiles for timeout estimation

Removed 2026-03-14 (ZERO callers):
  _datalake.py, _compliance.py, _url_audit.py
  12 deprecated SPARTA query endpoints (use /recall, /list, /analytics/run)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI
from loguru import logger

from ...lessons.recall import warm_dense_model

# Add persona root to sys.path once at module level (needed for persona.bridge imports)
_persona_root = Path("/home/graham/workspace/experiments/memory/persona")
if _persona_root.exists() and str(_persona_root.parent) not in sys.path:
    sys.path.insert(0, str(_persona_root.parent))

# ---------------------------------------------------------------------------
# App creation
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Graph Memory Service",
    version=os.getenv("GRAPH_MEMORY_VERSION", "0.1.0"),
    docs_url="/docs",
    redoc_url="/redoc",
)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def startup_event() -> None:
    """Warm caches and ensure schema/indexes exist."""
    # Ensure ArangoDB indexes (including vector indexes) are created
    try:
        from ...arango_client import get_db
        from ..._schema_indexes import ensure_indexes
        db = get_db()
        ensure_indexes(db)
        logger.info("Schema indexes verified")
    except Exception as exc:
        logger.warning("Schema index setup failed: {}", exc)

    warmed = warm_dense_model()
    if warmed:
        logger.info("Embedding model warmed and cached")
    else:
        logger.warning("Embedding model warm-up skipped (sentence-transformers not available)")


# ---------------------------------------------------------------------------
# Health (kept in __init__ — trivial)
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    db_ok = False
    try:
        from ...arango_client import get_db
        db = get_db()
        db_ok = db is not None
    except Exception as exc:
        logger.error("health check DB connection failed: {}", exc)
        pass
    return {"status": "ok", "ok": True, "memory_db_connected": db_ok}


@app.post("/gc")
def trigger_gc() -> dict:
    """Force garbage collection to reclaim memory (useful with --reload)."""
    import gc
    import resource

    before_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    collected = gc.collect()
    after_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

    return {
        "collected_objects": collected,
        "before_mb": round(before_mb, 1),
        "after_mb": round(after_mb, 1),
        "freed_mb": round(before_mb - after_mb, 1),
    }


# ---------------------------------------------------------------------------
# Include all routers
# ---------------------------------------------------------------------------

from ._core import router as _core_router  # noqa: E402
from ._user import router as _user_router  # noqa: E402
from ._persona import router as _persona_router  # noqa: E402
from ._sparta import router as _sparta_router  # noqa: E402
from ._store import router as _store_router  # noqa: E402
from ._db_audit import router as _db_audit_router  # noqa: E402
from ._drift import router as _drift_router  # noqa: E402
from ._intent import router as _intent_router  # noqa: E402
from ._taxonomy import router as _taxonomy_router  # noqa: E402
from ._tom import router as _tom_router  # noqa: E402
from ._analytics import router as _analytics_router  # noqa: E402
from ._queryspec_executor import router as _queryspec_executor_router  # noqa: E402
from ._entities import router as _entities_router  # noqa: E402
from ._backfill import router as _backfill_router  # noqa: E402
from ._matching import router as _matching_router  # noqa: E402
from ._latency import router as _latency_router  # noqa: E402

app.include_router(_core_router)
app.include_router(_user_router)
app.include_router(_persona_router)
app.include_router(_sparta_router)
app.include_router(_store_router)
app.include_router(_db_audit_router)
app.include_router(_drift_router)
app.include_router(_intent_router)
app.include_router(_taxonomy_router)
app.include_router(_tom_router)
app.include_router(_analytics_router)
app.include_router(_queryspec_executor_router)
app.include_router(_entities_router)
app.include_router(_backfill_router)
app.include_router(_matching_router)
app.include_router(_latency_router)

__all__ = ["app"]
