"""Quality gates for graph_memory operations.

Provides configurable threshold-based quality evaluation.

Configuration via environment variables:
    GM_GATE_{METRIC}_MIN - Minimum threshold for metric
    GM_GATE_{METRIC}_MAX - Maximum threshold for metric

Usage:
    from graph_memory.quality.gates import QualityGate, evaluate_gate

    # Create a gate
    gate = QualityGate(name="retrieval", thresholds={"mrr": 0.3})
    passed, details = gate.evaluate({"mrr": 0.35, "ndcg": 0.28})

    # Or use convenience function
    passed, details = evaluate_gate("batch", {"success_rate": 0.95})
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

# Default thresholds (can be overridden via env vars)
GATE_DEFAULTS = {
    "mrr": 0.25,
    "ndcg": 0.25,
    "recall_at_5": 0.5,
    "success_rate": 0.8,
    "error_rate_max": 0.1,
    "consecutive_errors_max": 5,
}


@dataclass
class QualityGate:
    """Quality gate with configurable thresholds.

    Thresholds can be set via:
    1. Constructor arguments (highest priority)
    2. Environment variables: GM_GATE_{METRIC}_MIN or GM_GATE_{METRIC}_MAX
    3. Defaults from GATE_DEFAULTS

    Args:
        name: Gate name for identification
        thresholds: Dict of metric -> threshold value

    Example:
        gate = QualityGate(name="retrieval", thresholds={"mrr": 0.3})
        passed, details = gate.evaluate({"mrr": 0.35, "ndcg": 0.28})
    """

    name: str
    thresholds: Dict[str, float] = field(default_factory=dict)
    _initialized: bool = field(default=False, repr=False)

    def __post_init__(self):
        """Load thresholds from env vars and defaults."""
        if self._initialized:
            return

        # Load from env vars and defaults for metrics not explicitly set
        for metric, default in GATE_DEFAULTS.items():
            if metric in self.thresholds:
                continue  # Explicit value takes precedence

            # Check env var
            env_key = f"GM_GATE_{metric.upper()}_MIN"
            if metric.endswith("_max"):
                env_key = f"GM_GATE_{metric.upper()}"
            env_val = os.getenv(env_key)

            if env_val:
                try:
                    self.thresholds[metric] = float(env_val)
                except ValueError:
                    self.thresholds[metric] = default
            else:
                self.thresholds[metric] = default

        self._initialized = True

    def evaluate(self, metrics: Dict[str, float]) -> Tuple[bool, Dict[str, Any]]:
        """Evaluate metrics against thresholds.

        Args:
            metrics: Dict of metric name -> actual value

        Returns:
            Tuple of (passed, details) where details contains per-metric results
        """
        results = {}
        all_passed = True

        for metric, threshold in self.thresholds.items():
            value = metrics.get(metric)
            if value is None:
                continue

            # For _max metrics, check if value is below threshold
            if metric.endswith("_max"):
                passed = value <= threshold
            else:
                passed = value >= threshold

            results[metric] = {
                "value": value,
                "threshold": threshold,
                "passed": passed,
                "direction": "max" if metric.endswith("_max") else "min",
            }

            if not passed:
                all_passed = False

        return all_passed, {
            "gate": self.name,
            "passed": all_passed,
            "metrics": results,
            "thresholds": self.thresholds,
        }

    def check_batch_health(
        self,
        completed: int,
        failed: int,
        consecutive_errors: int = 0,
    ) -> Tuple[bool, str]:
        """Check if a batch operation should continue.

        Args:
            completed: Number of items completed
            failed: Number of items failed
            consecutive_errors: Number of consecutive errors

        Returns:
            Tuple of (should_continue, reason)
        """
        if completed == 0:
            return True, "ok"

        # Check error rate
        error_rate = failed / completed
        max_error_rate = self.thresholds.get("error_rate_max", 0.1)
        if error_rate > max_error_rate:
            return False, f"error_rate={error_rate:.2%} exceeds {max_error_rate:.2%}"

        # Check consecutive errors
        max_consecutive = int(self.thresholds.get("consecutive_errors_max", 5))
        if consecutive_errors >= max_consecutive:
            return False, f"consecutive_errors={consecutive_errors} >= {max_consecutive}"

        # Check success rate
        success_rate = 1 - error_rate
        min_success_rate = self.thresholds.get("success_rate", 0.8)
        if success_rate < min_success_rate:
            return False, f"success_rate={success_rate:.2%} below {min_success_rate:.2%}"

        return True, "ok"


def evaluate_gate(
    name: str,
    metrics: Dict[str, float],
    thresholds: Optional[Dict[str, float]] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """Convenience function to evaluate a one-off gate.

    Args:
        name: Gate name
        metrics: Metrics to evaluate
        thresholds: Optional custom thresholds

    Returns:
        Tuple of (passed, details)
    """
    gate = QualityGate(name=name, thresholds=thresholds or {})
    return gate.evaluate(metrics)


def check_batch_gate(
    completed: int,
    failed: int,
    consecutive_errors: int = 0,
    thresholds: Optional[Dict[str, float]] = None,
) -> Tuple[bool, str]:
    """Convenience function to check batch health.

    Args:
        completed: Items completed
        failed: Items failed
        consecutive_errors: Consecutive errors
        thresholds: Optional custom thresholds

    Returns:
        Tuple of (should_continue, reason)
    """
    gate = QualityGate(name="batch", thresholds=thresholds or {})
    return gate.check_batch_health(completed, failed, consecutive_errors)


# Pre-configured gates for common use cases
RETRIEVAL_GATE = QualityGate(
    name="retrieval",
    thresholds={"mrr": 0.25, "ndcg": 0.25, "recall_at_5": 0.5},
)

BATCH_GATE = QualityGate(
    name="batch",
    thresholds={"success_rate": 0.8, "error_rate_max": 0.1, "consecutive_errors_max": 5},
)

LLM_GATE = QualityGate(
    name="llm",
    thresholds={"success_rate": 0.995, "error_rate_max": 0.05},  # 99.5% for LLM non-determinism
)
