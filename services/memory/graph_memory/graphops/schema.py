from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Dict, Any, Optional, Tuple


class ActionType(str, Enum):
    PROPOSE_EDGE = "propose_edge"
    DELETE_EDGE = "delete_edge"
    AQL = "aql"
    ANCHOR_BOOST = "anchor_boost"
    INVALIDATE = "invalidate"


@dataclass
class EdgeAction:
    type: ActionType
    from_id: Optional[str] = None
    to_id: Optional[str] = None
    rel_type: Optional[str] = None
    weight: Optional[float] = None
    half_life_days: Optional[float] = None
    edge_id: Optional[str] = None
    valid_to: Optional[int] = None
    rationale: Optional[str] = None


@dataclass
class AQLAction:
    type: ActionType
    query: str
    rationale: Optional[str] = None


@dataclass
class AnchorBoostAction:
    type: ActionType
    anchor_id: str
    delta: float = 0.05


Action = EdgeAction | AQLAction | AnchorBoostAction


@dataclass
class Observation:
    ts: int
    scope: Optional[str]
    q: Optional[str]
    top_bm25: List[str] = field(default_factory=list)
    top_vector: List[str] = field(default_factory=list)
    anchor_id: Optional[str] = None
    graph_stats: Dict[str, Any] = field(default_factory=dict)
    constraints: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Metrics:
    recall_at_k: float
    mrr: float
    ndcg: float
    took_ms: int


@dataclass
class StepResult:
    ok: bool
    observation: Observation
    reward: float
    metrics_before: Metrics
    metrics_after: Metrics
    violations: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class EpisodeConfig:
    scope: Optional[str] = None
    k: int = 10
    q_gold: List[Tuple[str, List[str]]] = field(default_factory=list)
    use_dense: bool = True
    diversify_k: Optional[int] = None
    apply_writes: bool = False  # default to read-only evaluation
    max_latency_ms: int = 1000
    weights: Dict[str, float] = field(default_factory=lambda: {"recall": 0.5, "mrr": 0.3, "ndcg": 0.2})
    penalties: Dict[str, float] = field(default_factory=lambda: {"latency": 0.05, "violation": 1.0, "edge_cost": 0.005})
    as_of: Optional[int] = None
    within_days: Optional[int] = None

