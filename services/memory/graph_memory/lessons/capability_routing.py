"""Capability-aware routing for task-oriented recall queries.

When an agent asks "how do I extract tables?", this module detects the
task intent and enriches recall results with a ``skill_route`` section
containing composition suggestions drawn from ``scope="skill_registry"``
lessons stored by skill-lab's ``scan_soup.py --register``.

The full ``/recommend-skill-chain`` cascade (memory → heuristic → classifier →
teacher) is invoked for task-oriented queries.  A recursion guard prevents
circular calls: ``/memory recall`` → ``enrich_with_capabilities`` →
``check_skill_chain`` → ``recommender.recommend`` → ``/memory recall``.

Gate: ``RECALL_CAPABILITY_ROUTING=1`` (default ON).
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, List, Optional
from loguru import logger

# ---------------------------------------------------------------------------
# Recursion guard — prevents /memory recall → recommend → /memory recall loop
# ---------------------------------------------------------------------------

_enrichment_lock = threading.local()


def _is_enriching() -> bool:
    return getattr(_enrichment_lock, "active", False)


def _set_enriching(state: bool) -> None:
    _enrichment_lock.active = state


# ---------------------------------------------------------------------------
# Task-intent detection
# ---------------------------------------------------------------------------

TASK_SIGNALS = [
    "how to", "how do i", "extract", "ingest", "learn", "train",
    "monitor", "review", "compose", "build", "create", "deploy",
    "fix", "troubleshoot", "run", "execute", "process", "convert",
    "scan", "fetch", "search", "download", "analyze", "validate",
]


def detect_task_intent(q: str) -> bool:
    """Return True if *q* looks task-oriented (needs skills, not just knowledge)."""
    q_lower = q.lower()
    return any(signal in q_lower for signal in TASK_SIGNALS)


# ---------------------------------------------------------------------------
# Skill-route enrichment
# ---------------------------------------------------------------------------


def check_skill_chain(q: str, confidence_threshold: float = 0.6) -> Optional[Dict[str, Any]]:
    """Check if a query maps to a known skill chain via /recommend-skill-chain.

    Called by /memory clarify BEFORE generating clarifying questions.
    If a high-confidence skill chain is found, the query is NOT ambiguous —
    it should be routed to the skill handler instead.

    Returns:
        skill_route dict if confidence >= threshold, else None.
    """
    # Recursion guard: if we're already inside enrich_with_capabilities(),
    # the recommender's own /memory recall would re-enter here.
    if _is_enriching():
        return None

    import subprocess
    import sys
    from pathlib import Path

    pi_mono = Path(os.getenv(
        "PI_MONO_ROOT",
        os.path.expanduser("~/workspace/experiments/pi-mono"),
    ))
    recommender_dir = pi_mono / ".pi" / "skills" / "recommend-skill-chain"
    recommender_src = recommender_dir / "src"

    if not recommender_src.exists():
        return None

    # Call the recommender's Python API directly
    try:
        if str(recommender_src) not in sys.path:
            sys.path.insert(0, str(recommender_src))
        from recommender import recommend
        result = recommend(task=q, limit=3)
    except Exception as exc:
        logger.error("check_skill_chain recommend failed: {exc}", exc=exc)
        # Fallback: call via run.sh
        run_sh = recommender_dir / "run.sh"
        if not run_sh.exists():
            return None
        try:
            proc = subprocess.run(
                [str(run_sh), "recommend", q],
                capture_output=True, text=True, timeout=15,
                cwd=str(recommender_dir),
            )
            if proc.returncode != 0:
                return None
            result = json.loads(proc.stdout)
        except Exception as exc:
            logger.error("check_skill_chain JSON parse failed: {exc}", exc=exc)
            return None

    recommendations = result.get("recommendations", [])
    if not recommendations:
        return None

    top = recommendations[0]
    if top.get("confidence", 0) < confidence_threshold:
        return None

    # Reject cold-start heuristic predictions — they're unreliable.
    # Only trust classifier/teacher/memory tiers, or heuristics with
    # a non-cold-start reason.
    tier = top.get("tier", "")
    reason = top.get("reason", "")
    if tier == "heuristic" and "cold_start" in reason:
        return None

    return {
        "chain": top.get("chain", []),
        "confidence": top.get("confidence", 0),
        "tier": top.get("tier", "unknown"),
        "reason": top.get("reason", ""),
        "orchestrator": top.get("orchestrator"),
        "bridge_tags": top.get("bridge_tags", []),
        "elegance": top.get("elegance", "adequate"),
    }


def enrich_with_capabilities(
    items: List[Dict[str, Any]],
    q: str,
    db,
) -> Optional[Dict[str, Any]]:
    """Build a skill routing suggestion from recall results.

    Primary path: calls ``check_skill_chain()`` which invokes the full
    ``/recommend-skill-chain`` cascade (memory → heuristic → classifier →
    teacher).  Supplements with BM25 ``skill_registry`` context for
    richer skill/capability/bond metadata.

    Fallback: BM25-only when the recommender is unavailable.

    Returns a ``skill_route`` dict or ``None`` if no skills match.
    """
    # Set recursion guard BEFORE calling check_skill_chain.
    # The recommender's Phase 1 calls /memory recall, which would
    # re-enter enrich_with_capabilities → check_skill_chain → infinite loop.
    _set_enriching(True)
    try:
        return _enrich_impl(items, q, db)
    finally:
        _set_enriching(False)


def _enrich_impl(
    items: List[Dict[str, Any]],
    q: str,
    db,
) -> Optional[Dict[str, Any]]:
    """Implementation of enrich_with_capabilities (inside recursion guard)."""

    # --- Primary: full /recommend-skill-chain cascade ---
    chain_route = check_skill_chain(q, confidence_threshold=0.4)

    # --- Supplementary: BM25 skill_registry context ---
    skill_items = [
        item for item in items
        if item.get("scope") == "skill_registry"
    ]
    if not skill_items and db is not None:
        from .recall import bm25_rank
        skill_items = bm25_rank(db, q=q, scope="skill_registry", tags=[], k=5)

    # Parse skill_registry solutions into structured buckets
    skills: List[Dict[str, Any]] = []
    capabilities: List[Dict[str, Any]] = []
    compositions: List[Dict[str, Any]] = []
    bonds: List[Dict[str, Any]] = []

    for item in (skill_items or []):
        solution = item.get("solution", "")
        tags = item.get("tags") or []

        try:
            data = json.loads(solution)
        except (json.JSONDecodeError, TypeError):
            data = {}

        if "skill" in tags and data.get("name"):
            skills.append({
                "name": data["name"],
                "provides": data.get("provides", []),
                "composability_score": data.get("composability_score", 0),
                "has_run_sh": data.get("has_run_sh", False),
            })
        elif "capability" in tags and data.get("providers"):
            capabilities.append({
                "capability": data.get("capability", ""),
                "providers": data["providers"],
                "best_provider": data.get("best_provider"),
            })
        elif "composition" in tags or "successful_chain" in tags:
            compositions.append({
                "chain": data.get("chain", []),
                "chain_str": data.get("chain_str", ""),
                "task": data.get("task", ""),
                "latency_ms": data.get("latency_ms"),
            })
        elif "bond" in tags:
            bonds.append({
                "bond_type": data.get("bond_type"),
                "success_rate": data.get("success_rate"),
                "observations": data.get("observations"),
            })

    # If we got a full cascade result, use it as the primary route
    # and attach BM25 context as supplementary metadata.
    if chain_route:
        chain_route["skills"] = skills[:5]
        chain_route["capabilities"] = capabilities[:5]
        chain_route["compositions"] = compositions[:3]
        chain_route["bonds"] = bonds[:5]
        return chain_route

    # Fallback: BM25-only route (no cascade available)
    if not skills and not capabilities and not compositions:
        return None

    suggested_chain = None
    if compositions:
        compositions.sort(key=lambda c: -len(c.get("chain", [])))
        suggested_chain = compositions[0]

    return {
        "skills": skills[:5],
        "capabilities": capabilities[:5],
        "compositions": compositions[:3],
        "bonds": bonds[:5],
        "suggested_chain": suggested_chain,
    }
