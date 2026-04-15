"""Consolidated Intent Mapper -- single intent system for /memory.

Absorbed from /sparta-intent. This is the ONLY intent mapping layer.
Routes retrieval through /memory's hybrid search, NOT standalone AQL.

Usage:
    from graph_memory.intent import IntentMapper

    mapper = IntentMapper()
    result = mapper.infer("How do I detect RF jamming attacks?")
    # {"action": "QUERY", "entities": [...], "tier1": [...], ...}

    # For retrieval, use hybrid search:
    from graph_memory.hybrid_search import hybrid_search_sparta_qra
    results = hybrid_search_sparta_qra(db, query, k=12)
"""

from ._4way import classify_4way, is_available as is_4way_available
from ._grounding import check_self_correction, recall_grounded_intent
from ._keywords import (
    AMBIGUITY_CONFIDENCE_THRESHOLD,
    INTENT_CONFIDENCE_THRESHOLD,
)
from ._mapper import (
    IntentMapper,
    ThreadTracker,
    extract_entities,
    extract_keywords,
    extract_tier1_tags,
    get_mapper,
)
from ._spec import QuerySpec
from ._t05 import T05Classifier

__all__ = [
    "AMBIGUITY_CONFIDENCE_THRESHOLD",
    "INTENT_CONFIDENCE_THRESHOLD",
    "IntentMapper",
    "QuerySpec",
    "T05Classifier",
    "ThreadTracker",
    "check_self_correction",
    "classify_4way",
    "extract_entities",
    "extract_keywords",
    "extract_tier1_tags",
    "get_mapper",
    "is_4way_available",
    "recall_grounded_intent",
]
