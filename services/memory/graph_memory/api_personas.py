"""Peer module for persona / Theory-of-Mind user management functions.

Extracted from api.py to keep modules under 800 lines.
All public functions are re-exported by api.py so existing
``from graph_memory.api import X`` imports continue to work.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List

from loguru import logger

from .arango_client import get_db


# ---------------------------------------------------------------------------
# Theory of Mind (ToM) API for Persona Agents
# ---------------------------------------------------------------------------

# ToM Configuration (environment variables)
TOM_DEFAULT_TRUST = float(os.getenv("TOM_DEFAULT_TRUST", "0.5"))
TOM_DEFAULT_RESPECT = float(os.getenv("TOM_DEFAULT_RESPECT", "0.5"))
TOM_HISTORY_TTL_DAYS = int(os.getenv("TOM_HISTORY_TTL_DAYS", "30"))
TOM_VALID_MOODS = os.getenv("TOM_VALID_MOODS", "").split(",") if os.getenv("TOM_VALID_MOODS") else []
TOM_VALID_SKILLS = ["unknown", "novice", "intermediate", "expert"]


def _validate_mood(mood: str | None) -> str | None:
    """Validate mood if TOM_VALID_MOODS is configured."""
    if mood is None:
        return None
    if TOM_VALID_MOODS and mood not in TOM_VALID_MOODS:
        valid_list = ", ".join(TOM_VALID_MOODS)
        raise ValueError(f"Invalid mood '{mood}'. Valid moods: {valid_list}")
    return mood


def _validate_skill(skill: str) -> str:
    """Validate skill level."""
    if skill not in TOM_VALID_SKILLS:
        valid_list = ", ".join(TOM_VALID_SKILLS)
        raise ValueError(f"Invalid skill '{skill}'. Valid skills: {valid_list}")
    return skill


def get_or_create_user(
    user_id: str,
    display_name: str = "",
    scope: str = "",
    initial_skill_level: str = "unknown",
) -> Dict[str, Any]:
    """Task 7: Get or create a user profile.

    Args:
        user_id: Unique identifier for the user
        display_name: Human-readable name
        scope: Project/context scope
        initial_skill_level: novice, intermediate, expert, or unknown

    Returns:
        User profile with is_new flag
    """
    # Validate skill level
    _validate_skill(initial_skill_level)

    from .setup_schema import ensure_collections_and_view

    ensure_collections_and_view()
    db = get_db()
    ts = int(time.time())

    # Try to find existing user
    existing = list(db.aql.execute(
        "FOR u IN users FILTER u.user_id == @uid LIMIT 1 RETURN u",
        bind_vars={"uid": user_id}
    ))

    if existing:
        user = existing[0]
        # Update interaction count
        db.aql.execute(
            "LET d = DOCUMENT(users, @key) UPDATE d WITH { interaction_count: (d.interaction_count || 0) + 1, last_seen_at: @ts } IN users",
            bind_vars={"key": user["_key"], "ts": ts}
        )
        user["is_new"] = False
        user["interaction_count"] = (user.get("interaction_count") or 0) + 1
        return user

    # Create new user
    doc = {
        "user_id": user_id,
        "display_name": display_name or user_id,
        "scope": scope,
        "skill_level": initial_skill_level,
        "topics": [],
        "worthiness_score": 0.5,  # Default neutral
        "interaction_count": 1,
        "notes": "",
        "first_seen_at": ts,
        "last_seen_at": ts,
        "created_at": ts,
        "updated_at": ts,
    }

    result = db.collection("users").insert(doc)
    doc["_key"] = result["_key"]
    doc["_id"] = result["_id"]
    doc["is_new"] = True
    return doc


def update_user_profile(
    user_id: str,
    skill_level: str | None = None,
    worthiness_score: float | None = None,
    add_topics: List[str] | None = None,
    notes: str | None = None,
) -> Dict[str, Any]:
    """Task 8: Update user profile.

    Args:
        user_id: User to update
        skill_level: New skill assessment
        worthiness_score: How worthy of respect (0.0 to 1.0)
        add_topics: Topics to add to user's known topics
        notes: Agent's notes about user

    Returns:
        Updated user profile
    """
    db = get_db()
    ts = int(time.time())

    # Find user
    existing = list(db.aql.execute(
        "FOR u IN users FILTER u.user_id == @uid LIMIT 1 RETURN u",
        bind_vars={"uid": user_id}
    ))

    if not existing:
        return {"error": "user_not_found", "user_id": user_id}

    user = existing[0]
    updates: Dict[str, Any] = {"updated_at": ts, "interaction_count": (user.get("interaction_count") or 0) + 1}

    if skill_level is not None:
        _validate_skill(skill_level)
        updates["skill_level"] = skill_level
    if worthiness_score is not None:
        updates["worthiness_score"] = max(0.0, min(1.0, worthiness_score))
    if add_topics:
        current_topics = user.get("topics") or []
        updates["topics"] = list(set(current_topics + add_topics))
    if notes is not None:
        updates["notes"] = notes

    db.collection("users").update({"_key": user["_key"], **updates})

    # Return updated user
    updated = db.collection("users").get(user["_key"])
    return updated


def get_user_history(
    user_id: str,
    include_episodes: bool = True,
    include_lessons_helped: bool = True,
    limit: int = 20,
) -> Dict[str, Any]:
    """Task 9: Get comprehensive user history.

    Args:
        user_id: User to look up
        include_episodes: Include past interaction episodes
        include_lessons_helped: Include lessons user contributed to

    Returns:
        User profile with history
    """
    db = get_db()

    # Get user profile
    existing = list(db.aql.execute(
        "FOR u IN users FILTER u.user_id == @uid LIMIT 1 RETURN u",
        bind_vars={"uid": user_id}
    ))

    if not existing:
        return {"user_profile": None, "episodes": [], "lessons_helped": [], "error": "user_not_found"}

    user = existing[0]
    result = {"user_profile": user, "episodes": [], "lessons_helped": []}

    if include_episodes:
        try:
            episodes = list(db.aql.execute(
                "FOR e IN episodes FILTER e.user_id == @uid SORT e.created_at DESC LIMIT @lim RETURN e",
                bind_vars={"uid": user_id, "lim": limit}
            ))
            result["episodes"] = episodes
        except Exception as exc:
            logger.error("episodes fetch failed: {}", exc)

    if include_lessons_helped:
        try:
            lessons = list(db.aql.execute(
                "FOR l IN lessons_v2 FILTER l.added_by == @uid SORT l.updated_at DESC LIMIT @lim RETURN KEEP(l, '_key', 'title', 'scope', 'updated_at')",
                bind_vars={"uid": user_id, "lim": limit}
            ))
            result["lessons_helped"] = lessons
        except Exception as exc:
            logger.error("lessons_helped fetch failed: {}", exc)

    return result


def get_or_create_relationship(
    user_id: str,
    agent_id: str,
) -> Dict[str, Any]:
    """Task 10: Get or create a user-agent relationship.

    Args:
        user_id: The user
        agent_id: The agent/persona

    Returns:
        Relationship state with is_new flag
    """
    from .setup_schema import ensure_collections_and_view
    from .api_bdi import get_or_create_persona_state

    ensure_collections_and_view()
    db = get_db()
    ts = int(time.time())

    # Ensure user and persona exist
    get_or_create_user(user_id=user_id)
    get_or_create_persona_state(agent_id=agent_id)

    user_doc = list(db.aql.execute("FOR u IN users FILTER u.user_id == @uid LIMIT 1 RETURN u._id", bind_vars={"uid": user_id}))
    agent_doc = list(db.aql.execute("FOR p IN persona_states FILTER p.agent_id == @aid LIMIT 1 RETURN p._id", bind_vars={"aid": agent_id}))

    if not user_doc or not agent_doc:
        return {"error": "user_or_agent_not_found"}

    from_id = user_doc[0]
    to_id = agent_doc[0]

    # Check for existing relationship
    existing = list(db.aql.execute(
        "FOR r IN user_agent_relationships FILTER r._from == @f AND r._to == @t LIMIT 1 RETURN r",
        bind_vars={"f": from_id, "t": to_id}
    ))

    if existing:
        rel = existing[0]
        rel["is_new"] = False
        return rel

    # Create new relationship
    doc = {
        "_from": from_id,
        "_to": to_id,
        "user_id": user_id,
        "agent_id": agent_id,
        "trust_level": TOM_DEFAULT_TRUST,  # Configurable via TOM_DEFAULT_TRUST
        "respect_level": TOM_DEFAULT_RESPECT,  # Configurable via TOM_DEFAULT_RESPECT
        "familiarity": 0.0,  # New relationship
        "key_moments": [],
        "interaction_count": 0,
        "last_interaction_at": None,
        # Theory of Mind: explicit belief state (reduces invalid actions by 50.7%)
        "user_beliefs": {},  # {"wants_help": 0.8, "is_frustrated": 0.3}
        "belief_confidence": 0.5,  # overall confidence in inferred beliefs
        "last_belief_update": None,
        "created_at": ts,
        "updated_at": ts,
    }

    result = db.collection("user_agent_relationships").insert(doc)
    doc["_key"] = result["_key"]
    doc["_id"] = result["_id"]
    doc["is_new"] = True
    return doc


def update_relationship(
    user_id: str,
    agent_id: str,
    trust_delta: float = 0.0,
    respect_delta: float = 0.0,
    familiarity_delta: float = 0.0,
) -> Dict[str, Any]:
    """Task 11: Update relationship with deltas.

    Args:
        user_id: The user
        agent_id: The agent/persona
        trust_delta: Change in trust (-1.0 to 1.0)
        respect_delta: Change in respect
        familiarity_delta: Change in familiarity

    Returns:
        Updated relationship
    """
    db = get_db()
    ts = int(time.time())

    # Get user and agent IDs
    user_doc = list(db.aql.execute("FOR u IN users FILTER u.user_id == @uid LIMIT 1 RETURN u._id", bind_vars={"uid": user_id}))
    agent_doc = list(db.aql.execute("FOR p IN persona_states FILTER p.agent_id == @aid LIMIT 1 RETURN p._id", bind_vars={"aid": agent_id}))

    if not user_doc or not agent_doc:
        return {"error": "user_or_agent_not_found"}

    from_id = user_doc[0]
    to_id = agent_doc[0]

    # Get existing relationship
    existing = list(db.aql.execute(
        "FOR r IN user_agent_relationships FILTER r._from == @f AND r._to == @t LIMIT 1 RETURN r",
        bind_vars={"f": from_id, "t": to_id}
    ))

    if not existing:
        return {"error": "relationship_not_found"}

    rel = existing[0]

    # Apply deltas with clamping
    new_trust = max(0.0, min(1.0, (rel.get("trust_level") or 0.5) + trust_delta))
    new_respect = max(0.0, min(1.0, (rel.get("respect_level") or 0.5) + respect_delta))
    new_familiarity = max(0.0, min(1.0, (rel.get("familiarity") or 0.0) + familiarity_delta))

    db.collection("user_agent_relationships").update({
        "_key": rel["_key"],
        "trust_level": new_trust,
        "respect_level": new_respect,
        "familiarity": new_familiarity,
        "interaction_count": (rel.get("interaction_count") or 0) + 1,
        "last_interaction_at": ts,
        "updated_at": ts,
    })

    # Return updated
    updated = db.collection("user_agent_relationships").get(rel["_key"])
    return updated


def record_key_moment(
    user_id: str,
    agent_id: str,
    event: str,
    impact: float,
    update_trust: bool = False,
    update_respect: bool = False,
) -> Dict[str, Any]:
    """Task 12: Record a key moment in the relationship.

    Args:
        user_id: The user
        agent_id: The agent/persona
        event: Description of the event
        impact: Impact magnitude (-1.0 to 1.0)
        update_trust: Also update trust based on impact
        update_respect: Also update respect based on impact

    Returns:
        Updated relationship with moment recorded
    """
    db = get_db()
    ts = int(time.time())

    # Get user and agent IDs
    user_doc = list(db.aql.execute("FOR u IN users FILTER u.user_id == @uid LIMIT 1 RETURN u._id", bind_vars={"uid": user_id}))
    agent_doc = list(db.aql.execute("FOR p IN persona_states FILTER p.agent_id == @aid LIMIT 1 RETURN p._id", bind_vars={"aid": agent_id}))

    if not user_doc or not agent_doc:
        return {"error": "user_or_agent_not_found"}

    from_id = user_doc[0]
    to_id = agent_doc[0]

    # Get existing relationship
    existing = list(db.aql.execute(
        "FOR r IN user_agent_relationships FILTER r._from == @f AND r._to == @t LIMIT 1 RETURN r",
        bind_vars={"f": from_id, "t": to_id}
    ))

    if not existing:
        # Create relationship first
        get_or_create_relationship(user_id=user_id, agent_id=agent_id)
        existing = list(db.aql.execute(
            "FOR r IN user_agent_relationships FILTER r._from == @f AND r._to == @t LIMIT 1 RETURN r",
            bind_vars={"f": from_id, "t": to_id}
        ))

    rel = existing[0]
    key_moments = rel.get("key_moments") or []

    # Add the moment
    moment = {
        "event": event,
        "impact": max(-1.0, min(1.0, impact)),
        "timestamp": ts,
    }
    key_moments.append(moment)

    # Keep only last 50 moments
    key_moments = key_moments[-50:]

    updates = {
        "_key": rel["_key"],
        "key_moments": key_moments,
        "interaction_count": (rel.get("interaction_count") or 0) + 1,
        "last_interaction_at": ts,
        "updated_at": ts,
    }

    # Optionally update trust/respect
    if update_trust:
        updates["trust_level"] = max(0.0, min(1.0, (rel.get("trust_level") or 0.5) + impact * 0.1))
    if update_respect:
        updates["respect_level"] = max(0.0, min(1.0, (rel.get("respect_level") or 0.5) + impact * 0.1))

    db.collection("user_agent_relationships").update(updates)

    # Return updated
    updated = db.collection("user_agent_relationships").get(rel["_key"])
    return updated
