"""Schema setup orchestrator -- delegates to focused submodules.

Submodules:
- _schema_collections: collection creation + ToM indices
- _schema_views: ArangoSearch view definitions (BM25)
- _schema_indexes: index creation for all collections
"""
from __future__ import annotations

from .arango_client import get_db
from ._schema_collections import ensure_collections, ensure_tom_indices
from ._schema_views import ensure_views
from ._schema_indexes import ensure_indexes


def ensure_collections_and_view():
    """Create all collections, views, and indexes (idempotent)."""
    db = get_db()
    ensure_collections(db)
    ensure_tom_indices(db)
    ensure_views(db)
    ensure_indexes(db)
