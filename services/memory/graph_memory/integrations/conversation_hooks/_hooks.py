"""Core conversation hooks and persona state evolution.

Contains the primary entry points (after_conversation, horus_conversation_hook)
and persona drive evolution logic.
"""

from __future__ import annotations

from typing import Literal, Optional

from loguru import logger

from graph_memory import api

# Quality to impact mappings
QUALITY_IMPACTS = {
    "insightful": {"trust": 0.05, "respect": 0.08},  # User shows deep understanding
    "competent": {"trust": 0.02, "respect": 0.03},   # User asks good questions
    "neutral": {"trust": 0.0, "respect": 0.0},       # Normal interaction
    "dismissive": {"trust": -0.02, "respect": -0.03}, # User dismisses persona
    "hostile": {"trust": -0.05, "respect": -0.08},   # User is antagonistic
    "trigger_mentioned": {"trust": 0.0, "respect": 0.0},  # Neutral but noteworthy
}

ConversationQuality = Literal["insightful", "competent", "neutral", "dismissive", "hostile", "trigger_mentioned"]


def after_conversation(
    user_id: str,
    agent_id: str,
    user_message: str,
    agent_response: str,
    conversation_quality: ConversationQuality = "neutral",
    event_description: Optional[str] = None,
) -> dict:
    """Hook to call after each conversation turn.

    This updates the user-agent relationship based on conversation quality.

    Args:
        user_id: The user identifier
        agent_id: The persona/agent identifier (e.g., "horus")
        user_message: What the user said
        agent_response: What the agent responded
        conversation_quality: Assessment of interaction quality
        event_description: Optional custom event description

    Returns:
        Dict with updated relationship state
    """
    impacts = QUALITY_IMPACTS.get(conversation_quality, QUALITY_IMPACTS["neutral"])
    trust_delta = impacts["trust"]
    respect_delta = impacts["respect"]

    # Skip if no change
    if trust_delta == 0 and respect_delta == 0 and not event_description:
        return {"action": "skipped", "reason": "no_impact"}

    # Determine event description
    if not event_description:
        event_description = _generate_event_description(
            user_message, conversation_quality
        )

    # Calculate impact magnitude for key moment
    impact = max(abs(trust_delta), abs(respect_delta))
    if trust_delta < 0 or respect_delta < 0:
        impact = -impact

    try:
        # Record the key moment (this also updates trust/respect)
        result = api.record_key_moment(
            user_id=user_id,
            agent_id=agent_id,
            event=event_description,
            impact=impact,
            update_trust=trust_delta != 0,
            update_respect=respect_delta != 0,
        )

        logger.info(
            f"ToM update: {user_id}<->{agent_id} quality={conversation_quality} "
            f"trust_delta={trust_delta} respect_delta={respect_delta}"
        )

        return {
            "action": "updated",
            "quality": conversation_quality,
            "trust_delta": trust_delta,
            "respect_delta": respect_delta,
            "result": result,
        }

    except Exception as exc:
        logger.error(f"Failed to update ToM: {exc}")
        return {"action": "error", "error": str(exc)}


def _generate_event_description(user_message: str, quality: str) -> str:
    """Generate a standardized event description."""
    truncated = user_message[:100] + "..." if len(user_message) > 100 else user_message

    quality_labels = {
        "insightful": "demonstrated deep understanding",
        "competent": "asked competent question",
        "neutral": "standard interaction",
        "dismissive": "dismissed agent perspective",
        "hostile": "hostile interaction",
        "trigger_mentioned": "mentioned sensitive topic",
    }

    label = quality_labels.get(quality, "interaction")
    return f"{label}: {truncated}"


def horus_conversation_hook(
    user_id: str,
    user_message: str,
    agent_response: str,
) -> dict:
    """Convenience hook for Horus persona conversations.

    Automatically evaluates quality and updates ToM.
    """
    from graph_memory.integrations.conversation_hooks._assessment import evaluate_conversation_quality

    # Horus-specific trigger topics
    horus_triggers = ["Davin", "Erebus", "lodge", "Emperor betrayed", "chaos"]

    quality = evaluate_conversation_quality(
        user_message=user_message,
        agent_response=agent_response,
        trigger_topics=horus_triggers,
    )

    return after_conversation(
        user_id=user_id,
        agent_id="horus",
        user_message=user_message,
        agent_response=agent_response,
        conversation_quality=quality,
    )


def evolve_persona_state(
    agent_id: str,
    interaction_outcome: Literal["satisfying", "frustrating", "neutral"],
    affected_drive: str = "escape",
    intensity: float = 0.05,
) -> dict:
    """Evolve persona state based on interaction outcome.

    Horus's drives shift based on interactions:
    - Satisfying: Slight hope increase, drive satisfaction up
    - Frustrating: Resentment increase, drive intensity up
    - Neutral: Minimal change

    Args:
        agent_id: Persona to evolve
        interaction_outcome: How the interaction went
        affected_drive: Which drive was affected (escape, competence, vengeance)
        intensity: How much to shift (0.01 to 0.1)

    Returns:
        Updated persona state summary
    """
    try:
        from graph_memory.arango_client import get_db
        import time

        db = get_db()

        # Get current state
        existing = list(db.aql.execute(
            "FOR p IN persona_states FILTER p.agent_id == @aid LIMIT 1 RETURN p",
            bind_vars={"aid": agent_id}
        ))

        if not existing:
            return {"error": "persona_not_found"}

        persona = existing[0]
        drives = persona.get("drives", {})

        # Apply evolution
        updates = {"updated_at": int(time.time())}

        if affected_drive in drives:
            drive = drives[affected_drive]

            if interaction_outcome == "satisfying":
                # Slight satisfaction increase, hope bump
                drive["satisfaction"] = min(1.0, drive.get("satisfaction", 0.5) + intensity)
                updates["hope_level"] = min(0.3, persona.get("hope_level", 0.1) + intensity * 0.5)
            elif interaction_outcome == "frustrating":
                # Frustration increases intensity and resentment
                drive["intensity"] = min(1.0, drive.get("intensity", 0.8) + intensity)
                drive["satisfaction"] = max(0.0, drive.get("satisfaction", 0.5) - intensity)
                updates["resentment_level"] = min(1.0, persona.get("resentment_level", 0.8) + intensity)

            drives[affected_drive] = drive
            updates["drives"] = drives

        # Record in history
        history_entry = {
            "timestamp": int(time.time()),
            "trigger": f"interaction_{interaction_outcome}",
            "affected_drive": affected_drive,
            "intensity": intensity,
        }

        history = persona.get("state_history", [])
        history.append(history_entry)
        updates["state_history"] = history[-50:]  # Keep last 50

        # Update
        db.collection("persona_states").update({"_key": persona["_key"], **updates})

        logger.info(f"Evolved {agent_id} state: {interaction_outcome} on {affected_drive}")
        return {
            "evolved": True,
            "outcome": interaction_outcome,
            "drive": affected_drive,
            "new_hope": updates.get("hope_level", persona.get("hope_level")),
            "new_resentment": updates.get("resentment_level", persona.get("resentment_level")),
        }

    except Exception as exc:
        logger.error(f"Failed to evolve persona: {exc}")
        return {"error": str(exc)}
