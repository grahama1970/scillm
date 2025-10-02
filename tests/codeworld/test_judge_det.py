from __future__ import annotations

from codeworld.engine.judge import judge_score, aggregate_judge


def test_judge_score_and_aggregate():
    task = "strategy_compare"
    context = {"expected": 10}
    outputs = {"result": 10, "loc": 12}
    timings = {"duration_ms": 120}

    metrics = judge_score(task, context, outputs, timings)
    assert 0.0 <= metrics.get("speed", 0.0) <= 1.0
    assert metrics["correctness"] == 1.0
    assert metrics["speed"] == 1.0 - (120 / 1000.0)
    assert metrics.get("brevity") == 1.0 - (12 / 100.0)

    agg = aggregate_judge(metrics)
    # Weighted: 0.7*1.0 + 0.2*(1-0.12) + 0.1*(1-0.12)
    expected_agg = 0.7*1.0 + 0.2*(1.0-0.12) + 0.1*(1.0-0.12)
    assert abs(agg - expected_agg) < 1e-9

