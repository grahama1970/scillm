"""Data models for the inference router.

Tier enum, RouteResult, TierConfig, RouterMetrics, and ResultCache.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from loguru import logger


# ---------------------------------------------------------------------------
# Tier definitions
# ---------------------------------------------------------------------------

class Tier(IntEnum):
    """Inference tiers ordered by cost/latency."""
    HEURISTIC = 0       # Regex, rules, sklearn -- free, us
    SMALL_GPT = 15      # Local GGUF/HF/HTTP -- free, ~200ms
    SCILLM = 20         # Chutes API -- paid, ~2-5s


@dataclass
class RouteResult:
    """Result from a routed inference call."""
    payload: Dict[str, Any]
    tier_used: int
    tier_name: str
    confidence: float
    latency_ms: int
    model: Optional[str] = None
    escalated: bool = False
    escalation_reason: Optional[str] = None
    cache_hit: bool = False

    @property
    def accepted(self) -> bool:
        """Whether the result met the confidence threshold."""
        return not self.escalated or self.tier_used == Tier.SCILLM

    def to_dict(self) -> Dict[str, Any]:
        return {
            "payload": self.payload,
            "tier_used": self.tier_used,
            "tier_name": self.tier_name,
            "confidence": self.confidence,
            "latency_ms": self.latency_ms,
            "model": self.model,
            "escalated": self.escalated,
            "escalation_reason": self.escalation_reason,
            "cache_hit": self.cache_hit,
        }


# ---------------------------------------------------------------------------
# Tier configuration
# ---------------------------------------------------------------------------

@dataclass
class TierConfig:
    """Configuration for a single inference tier."""
    tier: Tier
    name: str
    enabled: bool = True
    min_confidence: float = 0.7
    timeout_ms: int = 10_000

    # Tier 0: heuristic handler (Python callable)
    handler: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None

    # Tier 1.5: small GPT config
    model_path: Optional[str] = None       # GGUF or HF model path
    model_endpoint: Optional[str] = None   # HTTP service URL
    prompt_template: Optional[str] = None  # Prompt template with {input} placeholder
    system_prompt: Optional[str] = None

    # Tier 2: SciLLM config
    model: Optional[str] = None            # Model ID for SciLLM
    profile: Optional[str] = None          # "fast" or "accurate"
    max_tokens: Optional[int] = None

    @classmethod
    def heuristic(
        cls,
        handler: Callable[[Dict[str, Any]], Dict[str, Any]],
        min_confidence: float = 0.85,
        name: str = "heuristic",
    ) -> TierConfig:
        """Create a Tier 0 heuristic config.

        Handler signature:
            def handler(input_data: dict) -> dict:
                # Must return {"result": ..., "confidence": 0.0-1.0}
                # Optional: "skip" key to force escalation
        """
        return cls(
            tier=Tier.HEURISTIC,
            name=name,
            min_confidence=min_confidence,
            handler=handler,
            timeout_ms=1_000,
        )

    @classmethod
    def small_gpt(
        cls,
        model_path: Optional[str] = None,
        model_endpoint: Optional[str] = None,
        prompt_template: Optional[str] = None,
        system_prompt: Optional[str] = None,
        min_confidence: float = 0.7,
        timeout_ms: int = 5_000,
        name: str = "small_gpt",
    ) -> TierConfig:
        """Create a Tier 1.5 small GPT config.

        Supports three backends:
        - GGUF via llama-cpp-python (model_path ends with .gguf)
        - HuggingFace transformers (model_path is HF model ID)
        - HTTP service (model_endpoint is URL)
        """
        return cls(
            tier=Tier.SMALL_GPT,
            name=name,
            min_confidence=min_confidence,
            timeout_ms=timeout_ms,
            model_path=model_path,
            model_endpoint=model_endpoint,
            prompt_template=prompt_template,
            system_prompt=system_prompt,
        )

    @classmethod
    def scillm(
        cls,
        model: Optional[str] = None,
        profile: str = "fast",
        max_tokens: int = 512,
        min_confidence: float = 0.0,
        timeout_ms: int = 30_000,
        name: str = "scillm",
    ) -> TierConfig:
        """Create a Tier 2 SciLLM config (always-accept fallback)."""
        return cls(
            tier=Tier.SCILLM,
            name=name,
            min_confidence=min_confidence,
            timeout_ms=timeout_ms,
            model=model,
            profile=profile,
            max_tokens=max_tokens,
        )


# ---------------------------------------------------------------------------
# Router metrics
# ---------------------------------------------------------------------------

@dataclass
class RouterMetrics:
    """Tracks routing decisions for observability."""
    task_id: str
    total_calls: int = 0
    tier_counts: Dict[int, int] = field(default_factory=dict)
    escalation_count: int = 0
    cache_hits: int = 0
    avg_latency_ms: Dict[int, float] = field(default_factory=dict)
    confidence_sum: Dict[int, float] = field(default_factory=dict)
    errors: int = 0

    def record(self, result: RouteResult) -> None:
        self.total_calls += 1
        tier = result.tier_used
        self.tier_counts[tier] = self.tier_counts.get(tier, 0) + 1
        if result.escalated:
            self.escalation_count += 1
        if result.cache_hit:
            self.cache_hits += 1

        # Running average latency
        prev_avg = self.avg_latency_ms.get(tier, 0.0)
        prev_count = self.tier_counts[tier] - 1
        if prev_count > 0:
            self.avg_latency_ms[tier] = (
                prev_avg * prev_count + result.latency_ms
            ) / self.tier_counts[tier]
        else:
            self.avg_latency_ms[tier] = float(result.latency_ms)

        # Running average confidence
        prev_conf = self.confidence_sum.get(tier, 0.0)
        self.confidence_sum[tier] = prev_conf + result.confidence

    def summary(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "total_calls": self.total_calls,
            "tier_distribution": {
                _tier_label(k): v for k, v in self.tier_counts.items()
            },
            "escalation_rate": (
                self.escalation_count / self.total_calls
                if self.total_calls > 0 else 0.0
            ),
            "cache_hit_rate": (
                self.cache_hits / self.total_calls
                if self.total_calls > 0 else 0.0
            ),
            "avg_latency_ms": {
                _tier_label(k): round(v, 1) for k, v in self.avg_latency_ms.items()
            },
            "avg_confidence": {
                _tier_label(k): round(
                    self.confidence_sum.get(k, 0) / self.tier_counts.get(k, 1), 3
                )
                for k in self.tier_counts
            },
            "errors": self.errors,
        }


def _tier_label(tier: int) -> str:
    try:
        return Tier(tier).name.lower()
    except ValueError:
        return f"tier_{tier}"


# ---------------------------------------------------------------------------
# Result cache (SHA1-keyed, like relations.py llm-score cache)
# ---------------------------------------------------------------------------

class ResultCache:
    """Simple in-memory + optional file-backed cache for inference results.

    Matches the SHA1 caching pattern from lessons/relations.py llm-score.
    """

    def __init__(self, cache_dir: Optional[str] = None, max_memory: int = 10_000):
        self._memory: Dict[str, RouteResult] = {}
        self._max_memory = max_memory
        self._cache_dir = Path(cache_dir) if cache_dir else None
        if self._cache_dir:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    def _key(self, task_id: str, input_data: Dict[str, Any]) -> str:
        raw = json.dumps({"task": task_id, "input": input_data}, sort_keys=True)
        return hashlib.sha1(raw.encode()).hexdigest()

    def get(self, task_id: str, input_data: Dict[str, Any]) -> Optional[RouteResult]:
        key = self._key(task_id, input_data)
        if key in self._memory:
            result = self._memory[key]
            result.cache_hit = True
            return result

        if self._cache_dir:
            cache_file = self._cache_dir / f"{key}.json"
            if cache_file.exists():
                try:
                    data = json.loads(cache_file.read_text())
                    result = RouteResult(**data)
                    result.cache_hit = True
                    self._memory[key] = result
                    return result
                except Exception as exc:
                    logger.error("Suppressed error in router cache get: {}", exc)
        return None

    def put(
        self, task_id: str, input_data: Dict[str, Any], result: RouteResult
    ) -> None:
        key = self._key(task_id, input_data)

        # Evict oldest if at capacity
        if len(self._memory) >= self._max_memory:
            oldest = next(iter(self._memory))
            del self._memory[oldest]

        self._memory[key] = result

        if self._cache_dir:
            cache_file = self._cache_dir / f"{key}.json"
            try:
                cache_file.write_text(json.dumps(result.to_dict()))
            except Exception as exc:
                logger.error("Suppressed error in router cache put: {}", exc)
