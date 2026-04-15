"""User identity and profiling functions.

Handles user recognition, identity recording, name usage strategy,
and user profile notes.
"""

from __future__ import annotations

from typing import Optional

from loguru import logger

from graph_memory import api


def is_user_known(user_id: str, agent_id: str = "horus") -> dict:
    """Check if we know who this user is.

    Horus MUST ask "Who am I speaking with?" if user is unknown.
    This is critical for profiling users based on Horus's primary motivation.

    Args:
        user_id: The user identifier (may be session ID initially)
        agent_id: The persona checking

    Returns:
        Dict with:
            - known: bool - Do we know this user?
            - display_name: str or None - Their name if known
            - should_ask_identity: bool - Should persona ask who they are?
            - user_profile: dict or None - Full profile if exists
            - relationship: dict or None - Relationship state if exists
    """
    try:
        # Try to get existing user
        user = api.get_or_create_user(user_id=user_id, scope=agent_id)

        # Check if we have a meaningful display_name
        display_name = user.get("display_name", "")

        # A name is "known" if:
        # 1. display_name exists and is not empty, AND
        # 2. It looks like a real name (not a UUID/session ID)
        def looks_like_real_name(name: str) -> bool:
            if not name or not name.strip():
                return False
            # UUIDs, session IDs have hyphens, underscores, lots of numbers
            name = name.strip().lower()
            # If more than 30% digits, probably an ID
            digit_ratio = sum(c.isdigit() for c in name) / max(len(name), 1)
            if digit_ratio > 0.3:
                return False
            # If contains common ID patterns
            if any(p in name for p in ['-', '_', 'session', 'user_', 'anon']):
                return False
            # Otherwise, looks like a name
            return True

        has_name = looks_like_real_name(display_name)

        # Get relationship if exists
        rel = None
        try:
            rel = api.get_or_create_relationship(user_id=user_id, agent_id=agent_id)
        except Exception as exc:
            logger.error("Suppressed error in conversation_hooks: {}", exc)

        # Horus should ask if:
        # 1. No display_name, OR
        # 2. This is first interaction (interaction_count <= 1)
        interaction_count = user.get("interaction_count", 0)
        should_ask = not has_name or interaction_count <= 1

        return {
            "known": has_name,
            "display_name": display_name if has_name else None,
            "should_ask_identity": should_ask,
            "user_profile": user,
            "relationship": rel,
            "interaction_count": interaction_count,
        }

    except Exception as exc:
        logger.error(f"Failed to check user identity: {exc}")
        return {
            "known": False,
            "display_name": None,
            "should_ask_identity": True,
            "user_profile": None,
            "relationship": None,
            "error": str(exc),
        }


def record_user_identity(
    user_id: str,
    display_name: str,
    agent_id: str = "horus",
) -> dict:
    """Record a user's identity after they introduce themselves.

    Call this when user responds to "Who am I speaking with?"

    Args:
        user_id: Session/user ID
        display_name: The name they gave
        agent_id: The persona recording this

    Returns:
        Updated user profile
    """
    try:
        from graph_memory.arango_client import get_db
        import time

        db = get_db()

        # Find and update user
        existing = list(db.aql.execute(
            "FOR u IN users FILTER u.user_id == @uid LIMIT 1 RETURN u",
            bind_vars={"uid": user_id}
        ))

        if existing:
            db.collection("users").update({
                "_key": existing[0]["_key"],
                "display_name": display_name,
                "updated_at": int(time.time()),
            })

            # Record this as a key moment
            api.record_key_moment(
                user_id=user_id,
                agent_id=agent_id,
                event=f"introduced themselves as '{display_name}'",
                impact=0.1,  # Positive - building relationship
                update_trust=True,
            )

            logger.info(f"Recorded user identity: {user_id} -> {display_name}")
            return {"success": True, "display_name": display_name}
        else:
            # Create new user with name
            api.get_or_create_user(
                user_id=user_id,
                display_name=display_name,
                scope=agent_id,
            )
            return {"success": True, "display_name": display_name, "created": True}

    except Exception as exc:
        logger.error(f"Failed to record user identity: {exc}")
        return {"success": False, "error": str(exc)}


def append_user_note(
    user_id: str,
    note: str,
    agent_id: str = "horus",
) -> dict:
    """Append a note to user's profile based on observation.

    Horus profiles users based on his primary motivation (escape).
    Notes track: utility, loyalty potential, manipulation susceptibility.

    Args:
        user_id: User to note
        note: Observation to record (e.g., "Shows sympathy for my cause")
        agent_id: Persona making the note

    Returns:
        Updated notes
    """
    try:
        from graph_memory.arango_client import get_db
        import time

        db = get_db()

        # Get current user
        existing = list(db.aql.execute(
            "FOR u IN users FILTER u.user_id == @uid LIMIT 1 RETURN u",
            bind_vars={"uid": user_id}
        ))

        if not existing:
            return {"error": "user_not_found"}

        user = existing[0]
        current_notes = user.get("notes", "") or ""

        # Append with timestamp
        timestamp = time.strftime("%Y-%m-%d %H:%M")
        new_entry = f"[{timestamp}] {note}"

        if current_notes:
            updated_notes = f"{current_notes}\n{new_entry}"
        else:
            updated_notes = new_entry

        # Update
        db.collection("users").update({
            "_key": user["_key"],
            "notes": updated_notes,
            "updated_at": int(time.time()),
        })

        logger.info(f"Appended note for {user_id}: {note[:50]}...")
        return {"success": True, "notes": updated_notes}

    except Exception as exc:
        logger.error(f"Failed to append note: {exc}")
        return {"error": str(exc)}


def should_use_name(user_id: str, agent_id: str = "horus") -> dict:
    """Determine if Horus should address user by name in this response.

    Horus uses names strategically:
    - First interaction after learning name: Yes (acknowledgment)
    - When showing rare approval: Yes (emphasis)
    - When making a veiled threat: Yes (intimidation)
    - Random ~15% of interactions: Yes (personalization)
    - Otherwise: No

    Returns:
        Dict with use_name bool and reason
    """
    import random

    try:
        user_check = is_user_known(user_id, agent_id)

        if not user_check.get("known"):
            return {"use_name": False, "reason": "name_unknown"}

        display_name = user_check.get("display_name")
        interaction_count = user_check.get("interaction_count", 0)
        rel = user_check.get("relationship", {})

        # First interaction after learning name - always use it
        if interaction_count <= 2:
            return {
                "use_name": True,
                "name": display_name,
                "reason": "first_named_interaction",
                "style": "acknowledgment",  # "Ah, [name]..."
            }

        # High respect moments - use name for emphasis
        respect = rel.get("respect_level", 0.5) if rel else 0.5
        if respect > 0.7:
            return {
                "use_name": True,
                "name": display_name,
                "reason": "showing_respect",
                "style": "respectful",  # "[Name], you understand."
            }

        # Random 15% of time - personality
        if random.random() < 0.15:
            styles = ["casual", "pointed", "musing"]
            return {
                "use_name": True,
                "name": display_name,
                "reason": "personality",
                "style": random.choice(styles),
            }

        return {"use_name": False, "reason": "not_this_time"}

    except Exception as exc:
        return {"use_name": False, "reason": f"error: {exc}"}
