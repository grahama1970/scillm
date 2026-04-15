"""Quality module for graph_memory.

Provides quality gates and learning from outcomes.

Usage:
    from graph_memory.quality import QualityGate, LearningTracker, evaluate_gate

    # Quality gate evaluation
    gate = QualityGate(name="retrieval", thresholds={"mrr": 0.3})
    passed, details = gate.evaluate({"mrr": 0.35, "ndcg": 0.28})

    # Pre-configured gates
    from graph_memory.quality import RETRIEVAL_GATE, BATCH_GATE
    passed, details = RETRIEVAL_GATE.evaluate(metrics)

    # Learning from outcomes
    with LearningTracker(scope="extraction") as tracker:
        for item in items:
            result = process(item)
            if result.ok:
                tracker.record_success(item.id, retry_count=result.retries)
            else:
                tracker.record_failure(item.id, error_context=str(result.error))
"""

from .gates import (
    BATCH_GATE,
    LLM_GATE,
    RETRIEVAL_GATE,
    QualityGate,
    check_batch_gate,
    evaluate_gate,
)
from .learning import (
    LearningTracker,
    OutcomeRecord,
    get_average_complexity,
    get_complexity_for_item,
    record_outcome,
)
from .assess import assess_qra, detect_framework

__all__ = [
    # Gates
    "QualityGate",
    "evaluate_gate",
    "check_batch_gate",
    # Pre-configured gates
    "RETRIEVAL_GATE",
    "BATCH_GATE",
    "LLM_GATE",
    # Learning
    "LearningTracker",
    "OutcomeRecord",
    "record_outcome",
    "get_complexity_for_item",
    "get_average_complexity",
    # QRA Assessment
    "assess_qra",
    "detect_framework",
]
