"""Hybrid search combining BM25 + vector similarity + graph traversal.

Based on pattern from: https://gist.github.com/grahama1970/3e5127bbd4a4e69f3ae8bcd96fb056a1
"""
from ._embedding import get_query_embedding
from ._bm25 import bm25_search_collection, search_code_symbols
from ._graph import graph_traversal, expand_via_control_ids
from ._lessons import hybrid_search_lessons, hybrid_search_lean_theorems
from ._sparta import hybrid_search_sparta_qra, hybrid_search_skill_descriptions
from ._orchestrator import unified_hybrid_search

# Backward compat: _expand_via_control_ids was the old private name
_expand_via_control_ids = expand_via_control_ids

__all__ = [
    "get_query_embedding",
    "bm25_search_collection",
    "search_code_symbols",
    "graph_traversal",
    "expand_via_control_ids",
    "hybrid_search_lessons",
    "hybrid_search_lean_theorems",
    "hybrid_search_sparta_qra",
    "hybrid_search_skill_descriptions",
    "unified_hybrid_search",
]
