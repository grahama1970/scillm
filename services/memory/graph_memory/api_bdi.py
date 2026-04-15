"""Peer module for BDI / persona state functions.

Extracted from api.py to keep modules under 800 lines.
All public functions are re-exported by api.py so existing
``from graph_memory.api import X`` imports continue to work.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from loguru import logger

from .arango_client import get_db
from .api_personas import _validate_mood


def get_or_create_persona_state(
    agent_id: str,
    default_drives: Dict[str, Dict[str, float]] | None = None,
    default_mood: str = "neutral",
) -> Dict[str, Any]:
    """Task 13: Get or create persona state.

    Args:
        agent_id: Unique identifier for the persona/agent
        default_drives: Initial drives if creating new (e.g., {"escape": {"satisfaction": 0.1, "intensity": 0.9}})
        default_mood: Initial mood if creating new

    Returns:
        Persona state with is_new flag
    """
    from .setup_schema import ensure_collections_and_view

    ensure_collections_and_view()
    db = get_db()
    ts = int(time.time())

    # Try to find existing
    existing = list(db.aql.execute(
        "FOR p IN persona_states FILTER p.agent_id == @aid LIMIT 1 RETURN p",
        bind_vars={"aid": agent_id}
    ))

    if existing:
        state = existing[0]
        state["is_new"] = False
        return state

    # Create new persona state with full schema
    doc = {
        "agent_id": agent_id,
        "drives": default_drives or {
            "escape": {"satisfaction": 0.0, "intensity": 0.5},
            "competence": {"satisfaction": 0.5, "intensity": 0.5},
        },
        "coping_mechanisms": {},  # Track usage: {"dark_humor": 3, "grandiose_claims": 1}
        "defense_mechanisms": {
            "projection_frequency": 0,
            "grandiosity_triggers": [],
            "splitting_examples": [],
            "denial_topics": [],
        },
        "self_perception": {
            "self_hatred_level": 0.0,
            "shame_triggers": [],
            "compensatory_behaviors": [],
        },
        "humor_mode": "genuine_warmth",  # genuine_warmth, tactical_charm, cruel_mockery, gallows_humor
        "arc_phase_baseline": "pre_corruption",
        "pre_corruption_remnants": [],
        "current_mood": default_mood,
        "hope_level": 0.5,
        "resentment_level": 0.0,
        "created_at": ts,
        "updated_at": ts,
    }

    result = db.collection("persona_states").insert(doc)
    doc["_key"] = result["_key"]
    doc["_id"] = result["_id"]
    doc["is_new"] = True
    return doc


def update_persona_state(
    agent_id: str,
    drive_updates: Dict[str, Dict[str, float]] | None = None,
    mood: str | None = None,
    coping_mechanism_used: str | None = None,
    trigger: str | None = None,
    user_id: str | None = None,
    record_history: bool = True,
) -> Dict[str, Any]:
    """Task 14: Update persona state.

    Args:
        agent_id: The persona to update
        drive_updates: Updates to drives (e.g., {"escape": {"satisfaction": 0.2}})
        mood: New mood state
        coping_mechanism_used: Record usage of a coping mechanism
        trigger: What triggered this state change
        user_id: User who triggered the change (if any)
        record_history: Whether to record this in history

    Returns:
        Updated persona state
    """
    # Validate mood if configured
    _validate_mood(mood)

    db = get_db()
    ts = int(time.time())

    # Get existing state
    existing = list(db.aql.execute(
        "FOR p IN persona_states FILTER p.agent_id == @aid LIMIT 1 RETURN p",
        bind_vars={"aid": agent_id}
    ))

    if not existing:
        # Create first
        get_or_create_persona_state(agent_id=agent_id)
        existing = list(db.aql.execute(
            "FOR p IN persona_states FILTER p.agent_id == @aid LIMIT 1 RETURN p",
            bind_vars={"aid": agent_id}
        ))

    state = existing[0]
    updates: Dict[str, Any] = {"updated_at": ts}

    # Update drives
    if drive_updates:
        drives = state.get("drives") or {}
        for drive_name, drive_update in drive_updates.items():
            if drive_name not in drives:
                drives[drive_name] = {"satisfaction": 0.0, "intensity": 0.5}
            for key, value in drive_update.items():
                drives[drive_name][key] = max(0.0, min(1.0, value))
        updates["drives"] = drives

    # Update mood
    if mood is not None:
        updates["current_mood"] = mood

    # Track coping mechanism usage
    if coping_mechanism_used:
        coping = state.get("coping_mechanisms") or {}
        coping[coping_mechanism_used] = (coping.get(coping_mechanism_used) or 0) + 1
        updates["coping_mechanisms"] = coping

    db.collection("persona_states").update({"_key": state["_key"], **updates})

    # Record history if requested
    if record_history:
        history_doc = {
            "agent_id": agent_id,
            "timestamp": ts,
            "trigger": trigger or "",
            "triggered_by_user": user_id or "",
            "mood_before": state.get("current_mood"),
            "mood_after": mood or state.get("current_mood"),
            "drives_snapshot": updates.get("drives") or state.get("drives"),
            "coping_used": coping_mechanism_used or "",
        }
        try:
            db.collection("persona_state_history").insert(history_doc)
        except Exception as exc:
            logger.error("persona_state_history insert failed: {}", exc)

    # Return updated state
    updated = db.collection("persona_states").get(state["_key"])
    return updated


def get_persona_state_trend(
    agent_id: str,
    hours: int = 24,
    drive_name: str | None = None,
) -> Dict[str, Any]:
    """Task 15: Get persona state trend over time.

    Args:
        agent_id: The persona
        hours: How many hours back to look
        drive_name: Specific drive to track (optional)

    Returns:
        Trend data with history snapshots
    """
    db = get_db()
    ts = int(time.time())
    since = ts - (hours * 3600)

    # Get history
    history = list(db.aql.execute(
        "FOR h IN persona_state_history FILTER h.agent_id == @aid AND h.timestamp >= @since SORT h.timestamp ASC RETURN h",
        bind_vars={"aid": agent_id, "since": since}
    ))

    # Get current state
    current = list(db.aql.execute(
        "FOR p IN persona_states FILTER p.agent_id == @aid LIMIT 1 RETURN p",
        bind_vars={"aid": agent_id}
    ))

    result = {
        "agent_id": agent_id,
        "hours": hours,
        "current_state": current[0] if current else None,
        "trend_data": history,
    }

    # Add drive-specific trend if requested
    if drive_name and history:
        drive_trend = []
        for h in history:
            drives = h.get("drives_snapshot") or {}
            drive_data = drives.get(drive_name)
            if drive_data:
                drive_trend.append({
                    "timestamp": h.get("timestamp"),
                    "satisfaction": drive_data.get("satisfaction"),
                    "intensity": drive_data.get("intensity"),
                })
        result["drive_trend"] = drive_trend

    return result


# ---------------------------------------------------------------------------
# Theory of Mind: Belief State Tracking
# Research: Explicit belief representation reduces invalid actions by 50.7%
# ---------------------------------------------------------------------------


def infer_user_bdi(
    user_id: str,
    agent_id: str,
    conversation_history: List[Dict[str, str]],
    k: int = 3,
) -> Dict[str, Any]:
    """Infer user's BDI state from conversation history using zero-shot prompting.

    Based on ToM-agent research: use LLM to generate top-k BDI combinations
    from dialogue history without annotation or additional training.

    Args:
        user_id: The user to model
        agent_id: The agent doing the modeling
        conversation_history: List of {"role": "user"|"assistant", "content": "..."}
        k: Number of BDI combinations to generate

    Returns:
        {
            "beliefs": {"key": {"state": "text", "confidence": float}},
            "desires": {"key": {"state": "text", "confidence": float}},
            "intentions": {"key": {"state": "text", "confidence": float}},
            "overall_confidence": 0.7,
            "reasoning": "..."
        }
    """
    if not conversation_history:
        return {
            "beliefs": {},
            "desires": {},
            "intentions": {},
            "overall_confidence": 0.0,
            "reasoning": "No conversation history provided",
        }

    # Format conversation for LLM
    conv_text = "\n".join([
        f"{m.get('role', 'user').upper()}: {m.get('content', '')}"
        for m in conversation_history[-10:]  # Last 10 messages
    ])

    prompt = f"""Analyze this conversation and infer the USER's mental state (Theory of Mind).

CONVERSATION:
{conv_text}

Based on this conversation, infer the user's BDI state.
IMPORTANT: Disentangle the "state" (what they believe/want/intend) from your "confidence" in that inference.

1. BELIEFS: What does the user believe to be true?
2. DESIRES: What does the user want to achieve?
3. INTENTIONS: What is the user planning to do next?

Return ONLY valid JSON:
{{
  "beliefs": {{
    "belief_key": {{"state": "description", "confidence": 0.0_to_1.0}}
  }},
  "desires": {{
    "desire_key": {{"state": "description", "confidence": 0.0_to_1.0}}
  }},
  "intentions": {{
    "intention_key": {{"state": "description", "confidence": 0.0_to_1.0}}
  }},
  "overall_confidence": 0.0_to_1.0,
  "reasoning": "brief explanation of the evidence for these mental states"
}}"""

    # Try to use scillm for inference
    try:
        import sys as _sys
        from pathlib import Path
        skills_dir = Path(__file__).parent.parent.parent.parent / ".pi" / "skills"
        scillm_path = skills_dir / "scillm"
        if scillm_path.exists() and str(scillm_path) not in _sys.path:
            _sys.path.insert(0, str(scillm_path))

        from batch import quick_completion
        import json as _json

        response = quick_completion(
            prompt=prompt,
            max_tokens=512,
            temperature=0.3,
            timeout=30,
        )

        # Parse JSON response
        result = _json.loads(response.strip())
        result["user_id"] = user_id
        result["agent_id"] = agent_id
        return result

    except ImportError:
        # Fallback: simple heuristic based on keywords
        text_lower = conv_text.lower()
        beliefs: Dict[str, Any] = {}
        desires: Dict[str, Any] = {}
        intentions: Dict[str, Any] = {}

        # Simple keyword-based inference
        if "help" in text_lower or "how do i" in text_lower:
            beliefs["needs_assistance"] = {"state": "User needs assistance", "confidence": 0.8}
            desires["get_help"] = {"state": "User wants to receive help", "confidence": 0.9}
        if "error" in text_lower or "fail" in text_lower or "broken" in text_lower:
            beliefs["experiencing_problem"] = {"state": "User acts as if system is broken", "confidence": 0.8}
            desires["fix_issue"] = {"state": "User wants to fix the issue", "confidence": 0.9}
        if "thanks" in text_lower or "works" in text_lower:
            beliefs["problem_resolved"] = {"state": "User believes problem is resolved", "confidence": 0.7}
        if "?" in conv_text:
            intentions["ask_question"] = {"state": "User intends to ask a question", "confidence": 0.7}

        return {
            "beliefs": beliefs,
            "desires": desires,
            "intentions": intentions,
            "overall_confidence": 0.4,  # Lower confidence for heuristic
            "reasoning": "Heuristic inference (scillm not available)",
            "user_id": user_id,
            "agent_id": agent_id,
        }
    except Exception as exc:
        return {
            "beliefs": {},
            "desires": {},
            "intentions": {},
            "overall_confidence": 0.0,
            "reasoning": f"Inference failed: {exc}",
            "user_id": user_id,
            "agent_id": agent_id,
        }


def update_user_beliefs(
    user_id: str,
    agent_id: str,
    observation: str,
    prediction_matched: bool,
    belief_key: str | None = None,
) -> Dict[str, Any]:
    """Update belief confidence based on observation matching prediction.

    Based on ToM research confidence update rules:
    - If observation confirms belief: confidence = min(confidence * 1.2, 1.0)
    - If observation contradicts belief: confidence = confidence * 0.7

    Args:
        user_id: The user
        agent_id: The agent
        observation: What was observed (for logging)
        prediction_matched: Did reality match our prediction?
        belief_key: Specific belief to update (or None to update overall confidence)

    Returns:
        Updated relationship with new belief confidence
    """
    ts = int(time.time())
    db = get_db()

    # Get existing relationship
    existing = list(db.aql.execute(
        """FOR r IN user_agent_relationships
           FILTER r.user_id == @uid AND r.agent_id == @aid
           LIMIT 1 RETURN r""",
        bind_vars={"uid": user_id, "aid": agent_id}
    ))

    if not existing:
        return {"error": "relationship_not_found"}

    rel = existing[0]
    user_beliefs = rel.get("user_beliefs") or {}
    overall_confidence = rel.get("belief_confidence") or 0.5

    # Apply confidence update
    if prediction_matched:
        # Confirmed: increase confidence
        multiplier = 1.2
    else:
        # Contradicted: decrease confidence
        multiplier = 0.7

    if belief_key and belief_key in user_beliefs:
        # Update specific belief
        old_conf = user_beliefs[belief_key]
        user_beliefs[belief_key] = max(0.0, min(1.0, old_conf * multiplier))
    else:
        # Update overall confidence
        overall_confidence = max(0.0, min(1.0, overall_confidence * multiplier))

    # Store the update
    updates = {
        "_key": rel["_key"],
        "user_beliefs": user_beliefs,
        "belief_confidence": overall_confidence,
        "last_belief_update": ts,
        "updated_at": ts,
    }
    db.collection("user_agent_relationships").update(updates)

    # Return updated relationship
    updated = db.collection("user_agent_relationships").get(rel["_key"])
    updated["observation"] = observation
    updated["prediction_matched"] = prediction_matched
    return updated


def set_user_belief(
    user_id: str,
    agent_id: str,
    belief_key: str,
    confidence: float,
    source: str = "inferred",
) -> Dict[str, Any]:
    """Set a specific belief about the user.

    Args:
        user_id: The user
        agent_id: The agent
        belief_key: The belief name (e.g., "wants_help", "is_frustrated")
        confidence: Confidence level 0.0-1.0
        source: How belief was determined ("inferred", "explicit", "observed")

    Returns:
        Updated relationship
    """
    from .api_personas import get_or_create_relationship

    ts = int(time.time())
    db = get_db()

    # Get or create relationship
    rel = get_or_create_relationship(user_id, agent_id)

    user_beliefs = rel.get("user_beliefs") or {}
    user_beliefs[belief_key] = max(0.0, min(1.0, confidence))

    updates = {
        "_key": rel["_key"],
        "user_beliefs": user_beliefs,
        "last_belief_update": ts,
        "updated_at": ts,
    }
    db.collection("user_agent_relationships").update(updates)

    updated = db.collection("user_agent_relationships").get(rel["_key"])
    updated["belief_set"] = belief_key
    updated["source"] = source
    return updated


def decay_belief_confidence(
    user_id: str,
    agent_id: str,
    decay_factor: float = 0.95,
) -> Dict[str, Any]:
    """Apply time-based confidence decay to all beliefs.

    Based on research: confidence decays by 0.95 per turn without observation.

    Args:
        user_id: The user
        agent_id: The agent
        decay_factor: Multiplier for decay (default 0.95)

    Returns:
        Updated relationship with decayed confidences
    """
    ts = int(time.time())
    db = get_db()

    existing = list(db.aql.execute(
        """FOR r IN user_agent_relationships
           FILTER r.user_id == @uid AND r.agent_id == @aid
           LIMIT 1 RETURN r""",
        bind_vars={"uid": user_id, "aid": agent_id}
    ))

    if not existing:
        return {"error": "relationship_not_found"}

    rel = existing[0]
    user_beliefs = rel.get("user_beliefs") or {}
    overall_confidence = rel.get("belief_confidence") or 0.5

    # Decay all belief confidences
    for key in user_beliefs:
        user_beliefs[key] = max(0.0, user_beliefs[key] * decay_factor)

    # Decay overall confidence
    overall_confidence = max(0.0, overall_confidence * decay_factor)

    updates = {
        "_key": rel["_key"],
        "user_beliefs": user_beliefs,
        "belief_confidence": overall_confidence,
        "updated_at": ts,
    }
    db.collection("user_agent_relationships").update(updates)

    return db.collection("user_agent_relationships").get(rel["_key"])
