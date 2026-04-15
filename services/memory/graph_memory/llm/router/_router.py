"""InferenceRouter -- the main routing class.

Routes requests through configured tiers in order (cheapest first),
escalating to the next tier when confidence is below threshold.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from ._models import ResultCache, RouteResult, RouterMetrics, Tier, TierConfig
from ._runners import (
    _client_log,
    _default_prompt_builder,
    _run_heuristic,
    _run_scillm,
    _run_small_gpt,
)


class InferenceRouter:
    """Unified inference router with tiered model selection.

    Routes requests through configured tiers in order (cheapest first),
    escalating to the next tier when confidence is below threshold.

    Integrates with:
    - Brandon QRA assessment (assess reasoning traces locally first)
    - Edge verifier (score relationships with small GPT, escalate uncertain)
    - Monitor skills (continuous health checks with minimal API cost)
    - SPARTA pipeline (paraphrase/hints/scoring locally)

    Example:
        router = InferenceRouter("qra_assess", tiers=[
            TierConfig.heuristic(handler=assess_qra_local, min_confidence=0.9),
            TierConfig.small_gpt(
                model_path="models/qra-assessor/model.gguf",
                system_prompt="Assess QRA reasoning trace quality.",
                min_confidence=0.75,
            ),
            TierConfig.scillm(profile="accurate"),
        ])

        for qra in qras:
            result = router.route(qra)
            # result.tier_used tells you which tier handled it
            # result.confidence tells you how sure the model is
    """

    def __init__(
        self,
        task_id: str,
        tiers: List[TierConfig],
        cache: Optional[ResultCache] = None,
        prompt_builder: Optional[Callable[[Dict[str, Any]], str]] = None,
    ):
        self.task_id = task_id
        self.tiers = sorted(
            [t for t in tiers if t.enabled], key=lambda t: t.tier
        )
        self.cache = cache
        self.prompt_builder = prompt_builder or _default_prompt_builder
        self.metrics = RouterMetrics(task_id=task_id)

        if not self.tiers:
            raise ValueError(f"Router '{task_id}' has no enabled tiers")

    def route(
        self,
        input_data: Dict[str, Any],
        *,
        force_tier: Optional[Tier] = None,
        skip_cache: bool = False,
    ) -> RouteResult:
        """Route an inference request through configured tiers.

        Args:
            input_data: The input to process (task-specific dict)
            force_tier: Force a specific tier (bypass routing logic)
            skip_cache: Skip cache lookup

        Returns:
            RouteResult with payload, tier info, confidence, and latency
        """
        # Check cache first
        if not skip_cache and self.cache:
            cached = self.cache.get(self.task_id, input_data)
            if cached is not None:
                self.metrics.record(cached)
                return cached

        # Build prompt once for GPT/LLM tiers
        prompt = self.prompt_builder(input_data)

        # Route through tiers
        last_result: Optional[RouteResult] = None

        for tier_config in self.tiers:
            # Skip if force_tier specified and this isn't it
            if force_tier is not None and tier_config.tier != force_tier:
                continue

            result = self._execute_tier(tier_config, input_data, prompt)

            _client_log(
                "router.tier_result",
                task=self.task_id,
                tier=tier_config.name,
                confidence=result.confidence,
                latency_ms=result.latency_ms,
                escalated=result.escalated,
            )

            if not result.escalated:
                # Tier accepted -- record and return
                self.metrics.record(result)
                if self.cache:
                    self.cache.put(self.task_id, input_data, result)
                return result

            last_result = result

        # All tiers exhausted -- return last result (should be SciLLM)
        if last_result is not None:
            last_result.escalated = False  # Accept the final tier's result
            self.metrics.record(last_result)
            if self.cache:
                self.cache.put(self.task_id, input_data, last_result)
            return last_result

        # Should not reach here
        raise RuntimeError(f"Router '{self.task_id}': all tiers failed")

    def _execute_tier(
        self,
        config: TierConfig,
        input_data: Dict[str, Any],
        prompt: str,
    ) -> RouteResult:
        """Execute a single tier."""
        if config.tier == Tier.HEURISTIC:
            return _run_heuristic(config, input_data)
        elif config.tier == Tier.SMALL_GPT:
            return _run_small_gpt(config, input_data, prompt)
        elif config.tier == Tier.SCILLM:
            return _run_scillm(config, prompt)
        else:
            raise ValueError(f"Unknown tier: {config.tier}")

    def route_batch(
        self,
        items: List[Dict[str, Any]],
        *,
        skip_cache: bool = False,
    ) -> List[RouteResult]:
        """Route a batch of items, collecting per-tier batches for efficiency.

        Items that pass Tier 0 are not sent to Tier 1.5/2.
        Items that pass Tier 1.5 are not sent to Tier 2.
        This minimizes API calls.
        """
        results: List[Optional[RouteResult]] = [None] * len(items)
        pending_indices = list(range(len(items)))

        tier_results: List[tuple] = []
        for tier_config in self.tiers:
            if not pending_indices:
                break

            tier_results = []
            for idx in pending_indices:
                prompt = self.prompt_builder(items[idx])
                result = self._execute_tier(tier_config, items[idx], prompt)
                tier_results.append((idx, result))

            still_pending = []
            for idx, result in tier_results:
                if not result.escalated:
                    results[idx] = result
                    self.metrics.record(result)
                    if self.cache:
                        self.cache.put(self.task_id, items[idx], result)
                else:
                    still_pending.append(idx)

            pending_indices = still_pending

        # Handle any remaining (shouldn't happen if SciLLM is fallback)
        for idx in pending_indices:
            if results[idx] is None and tier_results:
                # Use last tier's result
                for tidx, tresult in tier_results:
                    if tidx == idx:
                        tresult.escalated = False
                        results[idx] = tresult
                        self.metrics.record(tresult)
                        break

        return [r for r in results if r is not None]

    def get_metrics(self) -> Dict[str, Any]:
        """Get routing metrics summary."""
        return self.metrics.summary()
