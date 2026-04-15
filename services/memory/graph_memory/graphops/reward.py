from __future__ import annotations
from typing import List, Dict
import math


def recall_at_k(preds: List[str], gold: List[str]) -> float:
    if not preds or not gold:
        return 0.0
    hits = sum(1 for p in preds if p in gold)
    denom = min(len(gold), len(preds)) or 1
    return hits / float(denom)


def mrr(preds: List[str], gold: List[str]) -> float:
    if not preds or not gold:
        return 0.0
    ranks = []
    for g in gold:
        try:
            ranks.append(preds.index(g) + 1)
        except ValueError:
            continue
    if not ranks:
        return 0.0
    return sum(1.0 / r for r in ranks) / float(len(gold))


def ndcg(preds: List[str], gold: List[str]) -> float:
    if not preds or not gold:
        return 0.0
    dcg = 0.0
    for i, p in enumerate(preds):
        rel = 1.0 if p in gold else 0.0
        if rel:
            dcg += rel / math.log2(i + 2)
    ideal = 0.0
    for i in range(min(len(gold), len(preds))):
        ideal += 1.0 / math.log2(i + 2)
    return (dcg / ideal) if ideal > 0 else 0.0


def aggregate_reward(
    before: Dict[str, float],
    after: Dict[str, float],
    weights: Dict[str, float],
    penalties: Dict[str, float],
    latency_ms: int,
    violations: int,
    edge_cost_units: float,
) -> float:
    d_recall = after.get("recall_at_k", 0.0) - before.get("recall_at_k", 0.0)
    d_mrr = after.get("mrr", 0.0) - before.get("mrr", 0.0)
    d_ndcg = after.get("ndcg", 0.0) - before.get("ndcg", 0.0)
    score = (
        weights.get("recall", 0.5) * d_recall
        + weights.get("mrr", 0.3) * d_mrr
        + weights.get("ndcg", 0.2) * d_ndcg
    )
    score -= penalties.get("latency", 0.05) * (latency_ms / 1000.0)
    score -= penalties.get("violation", 1.0) * float(violations)
    score -= penalties.get("edge_cost", 0.005) * float(edge_cost_units)
    return score

