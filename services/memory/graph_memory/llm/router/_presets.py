"""Pre-built router factories for common tasks.

Each factory creates an InferenceRouter with sensible tier defaults.
"""
from __future__ import annotations

from typing import Callable, Optional

from ._models import ResultCache, TierConfig
from ._router import InferenceRouter


def edge_score_router(
    heuristic_fn: Optional[Callable] = None,
    model_path: Optional[str] = None,
    cache_dir: Optional[str] = None,
) -> InferenceRouter:
    """Pre-built router for edge relationship scoring.

    Used by: lessons/relations.py, edge-verifier skill
    """
    tiers = []

    if heuristic_fn:
        tiers.append(TierConfig.heuristic(
            handler=heuristic_fn,
            min_confidence=0.85,
            name="edge_heuristic",
        ))

    if model_path:
        tiers.append(TierConfig.small_gpt(
            model_path=model_path,
            system_prompt=(
                "Score the relationship between two lessons. "
                "Return JSON: {keep: bool, weight: 0-1, confidence: 0-1, "
                "type: str, rationale: str}"
            ),
            min_confidence=0.7,
            name="edge_gpt",
        ))

    tiers.append(TierConfig.scillm(profile="fast", name="edge_scillm"))

    cache = ResultCache(cache_dir=cache_dir) if cache_dir else None

    return InferenceRouter("edge_score", tiers=tiers, cache=cache)


def qra_assess_router(
    heuristic_fn: Optional[Callable] = None,
    model_path: Optional[str] = None,
    cache_dir: Optional[str] = None,
) -> InferenceRouter:
    """Pre-built router for Brandon's QRA assessment.

    Used by: sparta-review skill, reality-check-sparta
    Tier 0: Deterministic assess_qra() checks (7 checks, ~3us)
    Tier 1.5: Small GPT for reasoning trace quality assessment
    Tier 2: SciLLM for complex/borderline cases
    """
    tiers = []

    if heuristic_fn:
        tiers.append(TierConfig.heuristic(
            handler=heuristic_fn,
            min_confidence=0.9,  # High bar -- only accept clear PASS/FAIL
            name="qra_deterministic",
        ))

    if model_path:
        tiers.append(TierConfig.small_gpt(
            model_path=model_path,
            system_prompt=(
                "You are Brandon Bailey's QRA quality assessor. "
                "Evaluate the reasoning trace for: anchoring, grounding, "
                "space terminology, taxonomy coherence, and reasoning quality. "
                "Return JSON: {grade: PASS|WARN|FAIL, confidence: 0-1, "
                "issues: [str], grounding_check: 0-1, anchoring_ok: bool, "
                "space_terms_ok: bool, reasoning_ok: bool}"
            ),
            min_confidence=0.75,
            name="qra_gpt",
        ))

    tiers.append(TierConfig.scillm(
        profile="accurate",
        name="qra_scillm",
        max_tokens=1024,
    ))

    cache = ResultCache(cache_dir=cache_dir) if cache_dir else None

    return InferenceRouter("qra_assess", tiers=tiers, cache=cache)


def monitor_router(
    heuristic_fn: Optional[Callable] = None,
    model_endpoint: Optional[str] = None,
) -> InferenceRouter:
    """Pre-built router for continuous monitoring skills.

    Used by: monitor-* skills (monitor-memory, monitor-skills, etc.)
    Optimized for high-frequency, low-cost health checks.
    Tier 0: Rule-based health checks
    Tier 1.5: Small GPT via HTTP service (always running)
    Tier 2: SciLLM only for anomaly investigation
    """
    tiers = []

    if heuristic_fn:
        tiers.append(TierConfig.heuristic(
            handler=heuristic_fn,
            min_confidence=0.8,
            name="monitor_rules",
        ))

    if model_endpoint:
        tiers.append(TierConfig.small_gpt(
            model_endpoint=model_endpoint,
            system_prompt=(
                "Assess system health from metrics. "
                "Return JSON: {healthy: bool, confidence: 0-1, "
                "issues: [str], severity: low|medium|high}"
            ),
            min_confidence=0.7,
            timeout_ms=3_000,
            name="monitor_gpt",
        ))

    tiers.append(TierConfig.scillm(
        profile="fast",
        name="monitor_scillm",
        timeout_ms=10_000,
    ))

    return InferenceRouter("monitor", tiers=tiers)


def edge_verify_router(
    heuristic_fn: Optional[Callable] = None,
    model_path: Optional[str] = None,
) -> InferenceRouter:
    """Pre-built router for edge verification.

    Used by: edge-verifier skill
    Tier 0: Similarity-threshold auto-verify (score > 0.8 -> verifies)
    Tier 1.5: Small GPT for medium-confidence edges
    Tier 2: SciLLM for complex/contradictory relationships
    """
    tiers = []

    if heuristic_fn:
        tiers.append(TierConfig.heuristic(
            handler=heuristic_fn,
            min_confidence=0.85,
            name="verify_similarity",
        ))

    if model_path:
        tiers.append(TierConfig.small_gpt(
            model_path=model_path,
            system_prompt=(
                "You are a Knowledge Graph Auditor. "
                "Classify the relationship between source and target. "
                "Return JSON: {weight: 0-1, stance: verifies|contradicts|neutral, "
                "rationale: str, confidence: 0-1}"
            ),
            min_confidence=0.7,
            name="verify_gpt",
        ))

    tiers.append(TierConfig.scillm(profile="fast", name="verify_scillm"))

    return InferenceRouter("edge_verify", tiers=tiers)


def paraphrase_router(
    model_path: Optional[str] = None,
) -> InferenceRouter:
    """Pre-built router for SPARTA AQL paraphrasing.

    Used by: seal/paraphrase_aql.py
    No heuristic tier -- paraphrasing always needs generation.
    Tier 1.5: Small GPT for persona-specific question generation
    Tier 2: SciLLM fallback
    """
    tiers = []

    if model_path:
        tiers.append(TierConfig.small_gpt(
            model_path=model_path,
            system_prompt=(
                "Generate a natural language question from the given AQL query "
                "as the specified persona. Return JSON: "
                "{question: str, confidence: 0-1}"
            ),
            min_confidence=0.6,
            name="paraphrase_gpt",
        ))

    tiers.append(TierConfig.scillm(profile="fast", name="paraphrase_scillm"))

    return InferenceRouter("paraphrase", tiers=tiers)
