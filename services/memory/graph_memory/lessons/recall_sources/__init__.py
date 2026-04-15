"""recall_sources package -- split from monolithic recall_sources.py.

Public API (preserved):
    RecallSource, builtin_sources, load_supplemental_sources,
    sanitize_fields, query_source, gather_supplemental_hits,
    apply_formatter, FORMATTERS
"""

from ._declarations import RecallSource, builtin_sources  # noqa: F401
from ._formatters import FORMATTERS, apply_formatter  # noqa: F401
from ._query import (  # noqa: F401
    gather_supplemental_hits,
    load_supplemental_sources,
    query_source,
    sanitize_fields,
)

__all__ = [
    "RecallSource",
    "builtin_sources",
    "load_supplemental_sources",
    "sanitize_fields",
    "query_source",
    "gather_supplemental_hits",
    "apply_formatter",
    "FORMATTERS",
]
