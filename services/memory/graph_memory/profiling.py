"""Performance profiling for /memory operations.

Provides a lightweight profiling context manager and CLI command
to identify bottlenecks in ArangoDB queries, embedding calls,
hybrid search, and entity extraction.

Usage as context manager:
    from graph_memory.profiling import Profiler

    with Profiler() as p:
        p.step("bm25_search")
        results = db.aql.execute(...)
        p.step("embedding")
        vec = get_query_embedding(text)
        p.step("rerank")
        ...
    print(p.report())

Usage as CLI:
    python -m graph_memory.profiling "firmware tampering countermeasures"
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StepTiming:
    name: str
    start_ms: float
    end_ms: float = 0.0

    @property
    def duration_ms(self) -> float:
        return self.end_ms - self.start_ms


class Profiler:
    """Lightweight step profiler for /memory operations."""

    def __init__(self, label: str = ""):
        self.label = label
        self.steps: list[StepTiming] = []
        self._current: StepTiming | None = None
        self._start_ms: float = 0.0
        self._end_ms: float = 0.0

    def __enter__(self):
        self._start_ms = time.perf_counter() * 1000
        return self

    def __exit__(self, *exc):
        self._close_current()
        self._end_ms = time.perf_counter() * 1000
        return False

    def step(self, name: str):
        """Mark the start of a new profiling step."""
        self._close_current()
        self._current = StepTiming(name=name, start_ms=time.perf_counter() * 1000)

    def _close_current(self):
        if self._current is not None:
            self._current.end_ms = time.perf_counter() * 1000
            self.steps.append(self._current)
            self._current = None

    @property
    def total_ms(self) -> float:
        return self._end_ms - self._start_ms

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "total_ms": round(self.total_ms, 1),
            "steps": [
                {"name": s.name, "ms": round(s.duration_ms, 1)}
                for s in self.steps
            ],
        }

    def report(self, budget_ms: float | None = None) -> str:
        """Format a human-readable profiling report."""
        lines = []
        if self.label:
            lines.append(f"Profile: {self.label}")
        total = self.total_ms
        for s in self.steps:
            pct = (s.duration_ms / total * 100) if total > 0 else 0
            bar = "#" * max(1, int(pct / 2))
            lines.append(f"  {s.name:30s} {s.duration_ms:7.1f}ms  {pct:5.1f}%  {bar}")
        verdict = "OK" if budget_ms is None or total <= budget_ms else "SLOW"
        budget_str = f" (budget: {budget_ms:.0f}ms)" if budget_ms else ""
        lines.append(f"  {'TOTAL':30s} {total:7.1f}ms  {verdict}{budget_str}")
        return "\n".join(lines)


def profile_search(query: str, k: int = 20) -> Profiler:
    """Profile a full /memory search call with per-step breakdown."""
    from .arango_client import get_db
    from .hybrid_search import hybrid_search_sparta_qra, get_query_embedding

    db = get_db()
    p = Profiler(label=f"search: {query[:60]}")
    p.__enter__()

    p.step("db_connect")
    db.version()  # warm connection

    p.step("bm25_sparta_qra")
    bm25_results = list(db.aql.execute(
        """FOR doc IN sparta_unified_search
             SEARCH ANALYZER(doc.question IN TOKENS(@q, 'text_en'), 'text_en')
             SORT BM25(doc) DESC LIMIT @k
             RETURN doc._key""",
        bind_vars={"q": query, "k": k},
    ))

    p.step("embedding")
    vec = get_query_embedding(query)

    p.step("hybrid_search_sparta_qra")
    hybrid_results = hybrid_search_sparta_qra(db, query, k=k)

    p.step("bm25_lessons")
    lesson_results = list(db.aql.execute(
        """FOR doc IN lessons_search
             SEARCH ANALYZER(doc.title IN TOKENS(@q, 'text_en'), 'text_en')
             SORT BM25(doc) DESC LIMIT @k
             RETURN doc._key""",
        bind_vars={"q": query, "k": k},
    ))

    p.step("entity_extraction")
    from .entity_extraction import extract_entities
    entities = extract_entities(query, include_taxonomy=False)

    p.step("domain_vocabulary")
    from .entity_helpers import _get_domain_vocabulary
    import graph_memory.entity_helpers as eh
    eh._domain_vocabulary_cache = None
    vocab = _get_domain_vocabulary(db)

    p.step("spellcheck")
    from .entity_extraction import spellcheck_question
    misspellings = spellcheck_question(query, db=db)

    p.__exit__(None, None, None)
    return p


def profile_full_recall(query: str, k: int = 20) -> Profiler:
    """Profile the full api.search() path with per-step breakdown."""
    from .arango_client import get_db
    from .api import search

    db = get_db()
    p = Profiler(label=f"full_recall: {query[:60]}")
    p.__enter__()

    p.step("db_connect")
    db.version()

    p.step("api_search")
    result = search(q=query, k=k)

    p.step("api_search_sparta_qra")
    result2 = search(q=query, k=k, collections=["sparta_qra"])

    p.__exit__(None, None, None)
    return p


if __name__ == "__main__":
    import sys
    import json

    query = sys.argv[1] if len(sys.argv) > 1 else "What SPARTA countermeasures protect firmware from tampering?"
    fmt = sys.argv[2] if len(sys.argv) > 2 else "text"

    print(f"Profiling: {query}\n")

    p = profile_search(query)
    if fmt == "json":
        print(json.dumps(p.as_dict(), indent=2))
    else:
        print(p.report(budget_ms=500))

    print()
    p2 = profile_full_recall(query)
    if fmt == "json":
        print(json.dumps(p2.as_dict(), indent=2))
    else:
        print(p2.report(budget_ms=2000))
