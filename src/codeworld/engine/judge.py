from __future__ import annotations

"""
Canonical judge rubric for CodeWorld (alpha).

This module provides deterministic, import-free scoring utilities used by the
bridge's judge path. Contestants may supply their own scoring functions to guide
search, but the judge rubric is the only source of truth for ranking.
"""

from typing import Dict, Any


def judge_score(task: str, context: Dict[str, Any], outputs: Dict[str, Any], timings: Dict[str, Any]) -> Dict[str, float]:
    """
    Compute canonical metrics in [0,1].

    - correctness: 1.0 if outputs['result'] == context.get('expected'), else 0.0 (omitted if no expected)
    - speed: 1 - duration_ms / 1000 (clamped)
    - brevity: 1 - (loc / 100) (clamped), if 'loc' present in outputs
    """
    metrics: Dict[str, float] = {}
    expected = context.get("expected")
    if expected is not None:
        metrics["correctness"] = 1.0 if outputs.get("result") == expected else 0.0
    duration_ms = float(timings.get("duration_ms", 0))
    metrics["speed"] = max(0.0, min(1.0, 1.0 - duration_ms / 1000.0))
    loc = outputs.get("loc")
    if isinstance(loc, (int, float)):
        metrics["brevity"] = max(0.0, min(1.0, 1.0 - float(loc) / 100.0))
    return metrics


def aggregate_judge(metrics: Dict[str, float]) -> float:
    """
    Weighted aggregate for judge scoring. Defaults: correctness 0.7, speed 0.2, brevity 0.1.
    Only counts available metrics; weights are re-normalized over present keys.
    """
    weights = {"correctness": 0.7, "speed": 0.2, "brevity": 0.1}
    present = {k: v for k, v in metrics.items() if k in weights}
    if not present:
        return 0.0
    wsum = sum(weights[k] for k in present.keys())
    return sum((weights[k] / wsum) * present[k] for k in present.keys())

