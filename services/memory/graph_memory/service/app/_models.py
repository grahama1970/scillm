"""Shared Pydantic models used across router submodules."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Core endpoints
# ---------------------------------------------------------------------------


class RecallRequest(BaseModel):
    q: str = Field(..., description="Natural language query/problem statement")
    scope: Optional[str] = Field(None, description="Optional memory scope override")
    k: int = Field(5, ge=1, le=200, description="Number of results to return")
    limit: Optional[int] = Field(None, ge=1, le=200, description="Alias for k (backwards compat)")
    threshold: float = Field(0.3, ge=0.0, le=1.0, description="Match threshold")
    collections: Optional[list[str]] = Field(None, description="Filter by source collections")
    tags: Optional[list[str]] = Field(None, description="Filter by tags")
    crosswalk_methods: Optional[list[str]] = Field(
        None,
        description="Filter QRAs by crosswalk method: 'direct' (SPARTA-curated), 'nist_nvd' (Heimdall), 'mitre_chain'"
    )

    def effective_k(self) -> int:
        """Return limit if provided (callers often send 'limit' not 'k'), else k."""
        return self.limit if self.limit is not None else self.k


class ByKeysRequest(BaseModel):
    """Fetch documents by key list. Defaults to _key but supports any allowlisted field
    via key_field (e.g. url_id, control_id) for cross-collection batch lookups."""
    collection: str = Field("lessons", description="Collection to query")
    keys: List[Any] = Field(..., min_length=1, max_length=500, description="Key values to match (str or int)")
    key_field: str = Field("_key", description="Field to match against (default: _key)")
    return_fields: Optional[List[str]] = Field(None, description="Fields to return (None=all)")


class ListRequest(BaseModel):
    """Paginated collection listing with sort and optional field filters.

    Replaces raw AQL LIMIT offset, limit. Filters use exact equality matching
    on allowlisted fields — no operators, no injection risk.
    """
    collection: str = Field("lessons", description="Collection to list")
    limit: int = Field(50, ge=1, le=500, description="Page size")
    offset: int = Field(0, ge=0, description="Offset for pagination")
    sort_field: str = Field("_key", description="Field to sort by")
    sort_order: str = Field("ASC", description="ASC or DESC")
    return_fields: Optional[List[str]] = Field(None, description="Fields to return (None=all)")
    filters: Optional[Dict[str, Any]] = Field(None, description="Exact-match field filters, e.g. {\"source_framework\": \"SPARTA\"}")
    tags: Optional[List[str]] = Field(None, description="Array-contains filter on 'tags' field via POSITION()")


class UpsertRequest(BaseModel):
    """Generic document upsert to allowlisted collections. Uses _key for identity.
    If a document with the same _key exists, it is merged and re-embedded. Otherwise inserted.
    Embedding is ALWAYS recomputed from text fields — no exceptions."""
    collection: str = Field(..., description="Target collection")
    documents: List[Dict[str, Any]] = Field(..., min_length=1, max_length=500, description="Documents to upsert (each must have _key)")


class QueryRequest(BaseModel):
    """Raw AQL execution request for safe read-only queries."""
    aql: str = Field(..., description="Raw AQL query string")
    bind_vars: Optional[Dict[str, Any]] = Field(None, description="AQL bind variables")


class ExploreRequest(BaseModel):
    """Dynamic NL-to-AQL graph traversal request."""
    q: str = Field(..., description="Natural language query to convert to AQL")
    schema_collections: Optional[List[str]] = Field(None, description="Collections to include in schema")


class IntentRequest(BaseModel):
    """NL query mapping to intent/UI actions."""
    q: str = Field(..., description="Natural language query to analyze")
    scope: str = Field("", description="Project scope")
    fast: bool = Field(False, description="Skip LLM, use fast keyword routing")


class LearnRequest(BaseModel):
    problem: str
    solution: str
    scope: Optional[str] = None
    tags: Optional[List[str]] = None
    code_symbol: bool = False


class RelatedRequest(BaseModel):
    title: str
    scope: Optional[str] = None
    k: int = Field(5, ge=1, le=50)


class ResidueRequest(BaseModel):
    limit: int = Field(10, ge=1, le=100, description="Max items to return")


class TraceRequest(BaseModel):
    q: str = Field(..., description="Query text")
    answer: str = Field("", description="Answer to verify claims against")
    scope: str = Field("", description="Scope filter")
    mode: str = Field("fast", description="Speed tier: instant|fast|accurate")
    k: int = Field(10, ge=1, le=50, description="Max retrieval results")
    depth: int = Field(3, ge=1, le=5, description="Graph traversal depth")


class AddEdgeRequest(BaseModel):
    from_title: str = Field(..., description="Title of the source lesson")
    to_title: str = Field(..., description="Title of the target lesson")
    type: str = Field("depends_on", description="Edge type")
    from_scope: Optional[str] = Field(None, description="Scope of source lesson")
    to_scope: Optional[str] = Field(None, description="Scope of target lesson")
    weight: float = Field(0.8, ge=0.0, le=1.0, description="Edge weight")
    rationale: str = Field("Authored", description="Why this edge exists")


class AddEdgesBatchRequest(BaseModel):
    edges: List[AddEdgeRequest] = Field(..., description="Batch of edges to add")


class ClarifyRequest(BaseModel):
    q: str = Field(..., description="User query to analyze for ambiguity")
    persona_id: str = Field("embry", description="Active persona (for voice)")
    scope: str = Field("", description="Project scope")
    context: Optional[str] = Field(None, description="Prior clarification context (re-query)")
    k: int = Field(5, description="Number of candidate results to inspect")


class DeflectRequest(BaseModel):
    q: str = Field(..., description="User query to classify and deflect")
    persona_id: str = Field("embry", description="Active persona")
    user_id: Optional[str] = Field(None, description="User ID for context")
    session_id: Optional[str] = Field(None, description="Session ID for thread tracking")
    intent_action: Optional[str] = Field(None, description="Override intent (QUERY, CLARIFY, OFF_TOPIC, NO_MATCH)")


# ---------------------------------------------------------------------------
# User / Persona / Relationship / Belief (Theory of Mind)
# ---------------------------------------------------------------------------


class UserGetRequest(BaseModel):
    user_id: str = Field(..., description="Unique user identifier")
    display_name: str = Field("", description="Human-readable name (for new users)")
    scope: str = Field("", description="Project/context scope")
    initial_skill_level: str = Field("unknown", description="Initial skill level")


class UserUpdateRequest(BaseModel):
    user_id: str = Field(..., description="User to update")
    skill_level: Optional[str] = Field(None, description="New skill level")
    worthiness_score: Optional[float] = Field(None, ge=0.0, le=1.0, description="Worthiness score")
    add_topics: Optional[List[str]] = Field(None, description="Topics to add")
    notes: Optional[str] = Field(None, description="Agent notes about user")


class UserHistoryRequest(BaseModel):
    user_id: str = Field(..., description="User to look up")
    include_episodes: bool = Field(True, description="Include interaction episodes")
    include_lessons_helped: bool = Field(True, description="Include lessons contributed to")
    limit: int = Field(20, ge=1, le=100, description="Max history items")


class PersonaGetRequest(BaseModel):
    agent_id: str = Field(..., description="Unique persona/agent identifier")
    default_drives: Optional[Dict[str, Dict[str, float]]] = Field(None, description="Default drives for new persona")
    default_mood: str = Field("neutral", description="Default mood for new persona")


class PersonaUpdateRequest(BaseModel):
    agent_id: str = Field(..., description="Persona to update")
    drive_updates: Optional[Dict[str, Dict[str, float]]] = Field(None, description="Drive updates")
    mood: Optional[str] = Field(None, description="New mood state")
    coping_mechanism_used: Optional[str] = Field(None, description="Coping mechanism used")
    trigger: Optional[str] = Field(None, description="What triggered this change")
    user_id: Optional[str] = Field(None, description="User who triggered the change")
    record_history: bool = Field(True, description="Record in history")


class PersonaTrendRequest(BaseModel):
    agent_id: str = Field(..., description="Persona to analyze")
    hours: int = Field(24, ge=1, le=720, description="Hours of history to include")
    drive_name: Optional[str] = Field(None, description="Specific drive to track")


class RelationshipGetRequest(BaseModel):
    user_id: str = Field(..., description="User ID")
    agent_id: str = Field(..., description="Agent/persona ID")


class RelationshipUpdateRequest(BaseModel):
    user_id: str = Field(..., description="User ID")
    agent_id: str = Field(..., description="Agent/persona ID")
    trust_delta: float = Field(0.0, ge=-1.0, le=1.0, description="Trust change")
    respect_delta: float = Field(0.0, ge=-1.0, le=1.0, description="Respect change")
    familiarity_delta: float = Field(0.0, ge=-1.0, le=1.0, description="Familiarity change")


class KeyMomentRequest(BaseModel):
    user_id: str = Field(..., description="User ID")
    agent_id: str = Field(..., description="Agent/persona ID")
    event: str = Field(..., description="Description of the key moment")
    impact: float = Field(..., ge=-1.0, le=1.0, description="Impact magnitude")
    update_trust: bool = Field(False, description="Also update trust")
    update_respect: bool = Field(False, description="Also update respect")


class InferBDIRequest(BaseModel):
    user_id: str = Field(..., description="User ID to model")
    agent_id: str = Field(..., description="Agent doing the modeling")
    conversation_history: list = Field(..., description="List of {role, content} messages")
    k: int = Field(3, ge=1, le=10, description="Top-k BDI combinations")


class UpdateBeliefRequest(BaseModel):
    user_id: str = Field(..., description="User ID")
    agent_id: str = Field(..., description="Agent ID")
    observation: str = Field(..., description="What was observed")
    prediction_matched: bool = Field(..., description="Did observation match prediction?")
    belief_key: str | None = Field(None, description="Specific belief to update")


class SetBeliefRequest(BaseModel):
    user_id: str = Field(..., description="User ID")
    agent_id: str = Field(..., description="Agent ID")
    belief_key: str = Field(..., description="Belief name")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence level")
    source: str = Field("inferred", description="How belief was determined")


class DecayBeliefRequest(BaseModel):
    user_id: str = Field(..., description="User ID")
    agent_id: str = Field(..., description="Agent ID")
    decay_factor: float = Field(0.95, ge=0.0, le=1.0, description="Decay multiplier")
