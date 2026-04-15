"""QuerySpec dataclass and validators.

Structured query specification for retrieval routing through /memory's hybrid search.
"""

from typing import ClassVar, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class QuerySpec(BaseModel):
    """Structured query specification for retrieval."""

    action: Literal["QUERY", "CLARIFY", "NO_MATCH", "CONTENT_SAFETY", "UI_COMMAND"] = "QUERY"
    ui_action: Optional[Literal[
        "SELECT_NODE", "ZOOM_IN", "ZOOM_OUT", "VIEW_ALL",
        "SET_PERSPECTIVE", "TOGGLE_PROGRESSIVE",
        "DISMISS_NODE", "FOCUS_CLUSTER", "COLLAPSE_NAMESPACE",
        "SCENE_ACTION", "NONE",
    ]] = None
    scene_action_id: Optional[str] = Field(default=None, description="Scene action: trace_execution, find_attack_surface, compare_nodes")
    target_node_id: Optional[str] = Field(default=None)
    expand_hops: int = Field(default=1)
    perspective: Optional[str] = Field(default=None)
    scope: str = Field(default="sparta")
    lanes: List[str] = Field(default=["bm25", "dense"])
    entities: List[str] = Field(default=[])
    tier1: List[str] = Field(default=[])

    VALID_MIND_TAGS: ClassVar[set[str]] = {"Detect", "Evade", "Exploit", "Harden", "Isolate", "Model", "Persist", "Restore"}
    frameworks: List[str] = Field(default=[])
    keywords: List[str] = Field(default=[])
    min_grounding: float = Field(default=0.7)
    depth: int = Field(default=1, le=2)
    k: int = Field(default=12)
    sort: str = Field(default="rrf")
    diagnostic: Optional[Literal["tag_dist", "entity_dist"]] = None

    @field_validator('depth')
    @classmethod
    def validate_depth(cls, v: int) -> int:
        return max(0, min(v, 2))

    @field_validator('min_grounding')
    @classmethod
    def validate_min_grounding(cls, v: float) -> float:
        return max(0.0, min(v, 1.0))

    @field_validator('keywords')
    @classmethod
    def validate_keywords(cls, v: List[str]) -> List[str]:
        return [kw[:64] for kw in v[:20] if isinstance(kw, str) and kw.strip()]

    @field_validator('entities')
    @classmethod
    def validate_entities(cls, v: List[str]) -> List[str]:
        return [e for e in v[:50] if isinstance(e, str) and e.strip()]

    @field_validator('tier1')
    @classmethod
    def validate_tier1(cls, v: List[str]) -> List[str]:
        valid = cls.VALID_MIND_TAGS
        result = [t for t in v if t in valid]
        rejected = [t for t in v if t not in valid]
        if rejected:
            from loguru import logger
            logger.warning(f"Rejected invalid Mind tags: {rejected}. Valid: {sorted(valid)}")
        return result

    @field_validator('lanes')
    @classmethod
    def validate_lanes(cls, v: List[str]) -> List[str]:
        allow = {"bm25", "dense", "entity", "graph", "taxonomy"}
        return [l for l in v if l in allow]

    @field_validator('sort')
    @classmethod
    def validate_sort(cls, v: str) -> str:
        return v if v in {"rrf", "score", "bm25"} else "rrf"

    @model_validator(mode='after')
    def validate_k_limit(self) -> 'QuerySpec':
        if self.k < 1:
            self.k = 1
        if self.diagnostic:
            self.k = min(self.k, 1000)
        else:
            self.k = min(self.k, 25)
        return self
