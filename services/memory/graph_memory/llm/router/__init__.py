"""Unified inference router for tiered model selection.

Generalizes the proof_assessment.py 3-tier cascade into a reusable
routing layer that any call site can use:

    Tier 0: Heuristic (regex, rules, sklearn) -- free, microseconds
    Tier 1.5: Small GPT (GGUF, HF, HTTP service) -- free, ~200ms
    Tier 2: SciLLM API (Chutes/DeepSeek) -- ~$0.12/1K, ~2-5s

Usage:
    from graph_memory.llm.router import InferenceRouter, TierConfig

    router = InferenceRouter("edge_score", tiers=[
        TierConfig.heuristic(handler=my_heuristic_fn, min_confidence=0.85),
        TierConfig.small_gpt(model_path="models/edge-scorer/model.gguf"),
        TierConfig.scillm(),
    ])

    result = router.route({"question": "...", "answer": "..."})
    print(result.tier_used, result.confidence, result.payload)
"""

# Models and data types
from ._models import (
    ResultCache,
    RouteResult,
    RouterMetrics,
    Tier,
    TierConfig,
    _tier_label,
)

# Main router class
from ._router import InferenceRouter

# Pre-built router factories
from ._presets import (
    edge_score_router,
    edge_verify_router,
    monitor_router,
    paraphrase_router,
    qra_assess_router,
)

# Helpers exposed for downstream use (e.g., teacher_student.py)
from ._runners import _extract_json

__all__ = [
    # Models
    "Tier",
    "RouteResult",
    "TierConfig",
    "RouterMetrics",
    "ResultCache",
    "_tier_label",
    # Router
    "InferenceRouter",
    # Presets
    "edge_score_router",
    "edge_verify_router",
    "monitor_router",
    "paraphrase_router",
    "qra_assess_router",
    # Helpers
    "_extract_json",
]
