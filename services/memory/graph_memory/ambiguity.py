"""Ambiguity analysis for query results.

Absorbed from /sparta-intent. Provides:
- AmbiguityOracle: statistical ambiguity detection on result distributions
- ResultStats: score/tag/entity distribution analysis
- Clarifier: targeted clarification question generation

These are pure analysis functions with no retrieval dependencies.
"""

import math
from collections import Counter
from typing import Any, Dict, List, NamedTuple, Optional


class AmbiguityVerdict(NamedTuple):
    is_ambiguous: bool
    triggers: List[str]


class AmbiguityOracle:
    """Deterministic oracle that decides if a query result is ambiguous
    based on statistical signals.
    """

    MARGIN_THRESHOLD = 0.08
    ENTROPY_THRESHOLD = 1.35
    TAG_DISAGREEMENT_THRESHOLD = 0.45
    ENTITY_DISAGREEMENT_THRESHOLD = 0.55

    @classmethod
    def evaluate(
        cls,
        stats: Dict[str, Any],
        thresholds: Optional[Dict[str, float]] = None,
        richness_score: float = 0.0,
    ) -> AmbiguityVerdict:
        triggers = []

        mt = (thresholds or {}).get("margin", cls.MARGIN_THRESHOLD)
        et = (thresholds or {}).get("entropy", cls.ENTROPY_THRESHOLD)
        tt = (thresholds or {}).get("tag_disagreement", cls.TAG_DISAGREEMENT_THRESHOLD)
        at = (thresholds or {}).get("entity_disagreement", cls.ENTITY_DISAGREEMENT_THRESHOLD)

        if stats.get("count", 0) < 5:
            return AmbiguityVerdict(is_ambiguous=False, triggers=["insufficient_sample"])

        if stats.get("margin", 1.0) < mt:
            triggers.append(f"margin_low_{stats.get('margin')}")

        if stats.get("entropy", 0.0) > et:
            triggers.append(f"entropy_high_{stats.get('entropy')}")

        if stats.get("tag_disagreement", 0.0) > tt:
            triggers.append(f"tag_fracture_{stats.get('tag_disagreement')}")

        if stats.get("entity_disagreement", 0.0) > at:
            triggers.append(f"entity_fracture_{stats.get('entity_disagreement')}")

        # Adjust trigger threshold based on coverage richness:
        # Rich coverage (>=0.6) → need MORE triggers to declare ambiguous
        # Sparse coverage (<0.2) → fewer triggers suffice
        required_triggers = 2
        if richness_score >= 0.6:
            required_triggers = 3  # Need MORE evidence to declare ambiguity
        elif richness_score < 0.2:
            required_triggers = 1  # Sparse = easier to declare ambiguous

        is_ambiguous = len(triggers) >= required_triggers

        return AmbiguityVerdict(is_ambiguous=is_ambiguous, triggers=triggers)


class ResultStats:
    """Computes statistical signals from query results for the Ambiguity Oracle."""

    @staticmethod
    def compute(results: List[Dict[str, Any]], k: int) -> Dict[str, Any]:
        if not results:
            return {
                "margin": 0.0,
                "entropy": 0.0,
                "tag_dist": {},
                "entity_dist": {},
                "count": 0,
            }

        scores = [r.get("score", 0.0) for r in results]

        if len(scores) >= 2:
            margin = scores[0] - scores[1]
        elif len(scores) == 1:
            margin = 1.0
        else:
            margin = 0.0

        total_score = sum(scores)
        entropy = 0.0
        if total_score > 0:
            probs = [s / total_score for s in scores]
            for p in probs:
                if p > 0:
                    entropy -= p * math.log(p, 2)

        from .lessons.store import get_mind_tags
        all_tier1 = []
        for r in results:
            tags = get_mind_tags(r) or []
            if isinstance(tags, list):
                all_tier1.extend(tags)

        all_entities = []
        for r in results:
            cid = r.get("control_id")
            if cid:
                all_entities.append(cid)
            tid = r.get("tactic_id")
            if tid:
                all_entities.append(tid)

        tag_counts = dict(Counter(all_tier1).most_common(5))
        entity_counts = dict(Counter(all_entities).most_common(5))

        tag_disagreement = 0.0
        if all_tier1:
            top_tag_count = Counter(all_tier1).most_common(1)[0][1]
            tag_disagreement = 1.0 - (top_tag_count / len(all_tier1))

        entity_disagreement = 0.0
        if all_entities:
            top_entity_count = Counter(all_entities).most_common(1)[0][1]
            entity_disagreement = 1.0 - (top_entity_count / len(all_entities))

        return {
            "margin": round(margin, 4),
            "entropy": round(entropy, 4),
            "tag_dist": tag_counts,
            "entity_dist": entity_counts,
            "tag_disagreement": round(tag_disagreement, 3),
            "entity_disagreement": round(entity_disagreement, 3),
            "count": len(results),
        }


class Clarifier:
    """Generates targeted clarification questions from result statistics."""

    @staticmethod
    def generate_question(stats: Dict[str, Any]) -> str:
        """Select best clarification strategy based on stats fracture.
        Returns a question string (max 100 chars).
        """
        if stats.get("tag_disagreement", 0) > 0.4:
            counts = stats.get("tag_dist", {})
            if isinstance(counts, dict) and len(counts) >= 2:
                sorted_items = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
                top_tags = [k for k, _ in sorted_items[:2]]
                if len(top_tags) == 2:
                    q = f"Are you asking about {top_tags[0]} or {top_tags[1]}?"
                    return q[:100]
            return "Are you asking about detecting, preventing, or recovering from attacks?"[:100]

        if stats.get("entity_disagreement", 0) > 0.5:
            counts = stats.get("entity_dist", {})
            if isinstance(counts, dict) and len(counts) >= 2:
                sorted_items = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
                top_ids = [k for k, _ in sorted_items[:2]]
                if len(top_ids) == 2:
                    q = f"Which control specifically: {top_ids[0]} or {top_ids[1]}?"
                    return q[:100]
            return "Which specific control or technique are you interested in?"[:100]

        if stats.get("entropy", 0) > 1.2:
            return "Could you specify if this is for spacecraft or ground systems?"[:100]

        return "Can you be more specific about the context?"[:100]
