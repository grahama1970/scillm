"""Persona, relationship, and belief (Theory of Mind) endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ... import api as tom_api
from ._models import (
    PersonaGetRequest,
    PersonaUpdateRequest,
    PersonaTrendRequest,
    RelationshipGetRequest,
    RelationshipUpdateRequest,
    KeyMomentRequest,
    InferBDIRequest,
    UpdateBeliefRequest,
    SetBeliefRequest,
    DecayBeliefRequest,
)

router = APIRouter()


# ---- Persona endpoints -----------------------------------------------------


@router.post("/persona/get")
def persona_get(req: PersonaGetRequest) -> dict:
    """Get or create a persona state."""
    if not req.agent_id.strip():
        raise HTTPException(status_code=400, detail="agent_id is required")
    return tom_api.get_or_create_persona_state(
        agent_id=req.agent_id,
        default_drives=req.default_drives,
        default_mood=req.default_mood,
    )


@router.post("/persona/update")
def persona_update(req: PersonaUpdateRequest) -> dict:
    """Update persona state."""
    if not req.agent_id.strip():
        raise HTTPException(status_code=400, detail="agent_id is required")
    return tom_api.update_persona_state(
        agent_id=req.agent_id,
        drive_updates=req.drive_updates,
        mood=req.mood,
        coping_mechanism_used=req.coping_mechanism_used,
        trigger=req.trigger,
        user_id=req.user_id,
        record_history=req.record_history,
    )


@router.post("/persona/trend")
def persona_trend(req: PersonaTrendRequest) -> dict:
    """Get persona state trend over time."""
    if not req.agent_id.strip():
        raise HTTPException(status_code=400, detail="agent_id is required")
    return tom_api.get_persona_state_trend(
        agent_id=req.agent_id,
        hours=req.hours,
        drive_name=req.drive_name,
    )


# ---- Relationship endpoints ------------------------------------------------


@router.post("/relationship/get")
def relationship_get(req: RelationshipGetRequest) -> dict:
    """Get or create a user-agent relationship."""
    if not req.user_id.strip() or not req.agent_id.strip():
        raise HTTPException(status_code=400, detail="user_id and agent_id are required")
    return tom_api.get_or_create_relationship(
        user_id=req.user_id,
        agent_id=req.agent_id,
    )


@router.post("/relationship/update")
def relationship_update(req: RelationshipUpdateRequest) -> dict:
    """Update relationship with deltas."""
    if not req.user_id.strip() or not req.agent_id.strip():
        raise HTTPException(status_code=400, detail="user_id and agent_id are required")
    return tom_api.update_relationship(
        user_id=req.user_id,
        agent_id=req.agent_id,
        trust_delta=req.trust_delta,
        respect_delta=req.respect_delta,
        familiarity_delta=req.familiarity_delta,
    )


@router.post("/relationship/moment")
def relationship_moment(req: KeyMomentRequest) -> dict:
    """Record a key moment in the relationship."""
    if not req.user_id.strip() or not req.agent_id.strip():
        raise HTTPException(status_code=400, detail="user_id and agent_id are required")
    if not req.event.strip():
        raise HTTPException(status_code=400, detail="event is required")
    return tom_api.record_key_moment(
        user_id=req.user_id,
        agent_id=req.agent_id,
        event=req.event,
        impact=req.impact,
        update_trust=req.update_trust,
        update_respect=req.update_respect,
    )


# ---- Belief state endpoints ------------------------------------------------


@router.post("/belief/infer")
def belief_infer(req: InferBDIRequest) -> dict:
    """Infer user's BDI state from conversation history using LLM."""
    if not req.user_id.strip() or not req.agent_id.strip():
        raise HTTPException(status_code=400, detail="user_id and agent_id are required")
    if not req.conversation_history:
        raise HTTPException(status_code=400, detail="conversation_history is required")
    return tom_api.infer_user_bdi(
        user_id=req.user_id,
        agent_id=req.agent_id,
        conversation_history=req.conversation_history,
        k=req.k,
    )


@router.post("/belief/update")
def belief_update(req: UpdateBeliefRequest) -> dict:
    """Update belief confidence based on observation matching prediction."""
    if not req.user_id.strip() or not req.agent_id.strip():
        raise HTTPException(status_code=400, detail="user_id and agent_id are required")
    return tom_api.update_user_beliefs(
        user_id=req.user_id,
        agent_id=req.agent_id,
        observation=req.observation,
        prediction_matched=req.prediction_matched,
        belief_key=req.belief_key,
    )


@router.post("/belief/set")
def belief_set(req: SetBeliefRequest) -> dict:
    """Set a specific belief about the user."""
    if not req.user_id.strip() or not req.agent_id.strip():
        raise HTTPException(status_code=400, detail="user_id and agent_id are required")
    if not req.belief_key.strip():
        raise HTTPException(status_code=400, detail="belief_key is required")
    return tom_api.set_user_belief(
        user_id=req.user_id,
        agent_id=req.agent_id,
        belief_key=req.belief_key,
        confidence=req.confidence,
        source=req.source,
    )


@router.post("/belief/decay")
def belief_decay(req: DecayBeliefRequest) -> dict:
    """Apply time-based confidence decay to all beliefs."""
    if not req.user_id.strip() or not req.agent_id.strip():
        raise HTTPException(status_code=400, detail="user_id and agent_id are required")
    return tom_api.decay_belief_confidence(
        user_id=req.user_id,
        agent_id=req.agent_id,
        decay_factor=req.decay_factor,
    )
