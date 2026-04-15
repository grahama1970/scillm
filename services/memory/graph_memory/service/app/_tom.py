"""Consolidated Theory of Mind endpoint.

Inputs:
    user_id (path) — the user to look up.
    agent_id (query, default "horus") — the agent whose persona/relationship to fetch.

Outputs:
    JSON ``{"user": {...}, "persona_state": {...}, "relationship": {...}}``.
    Sub-objects are empty dicts when no data exists (never 404).

Failure modes:
    400 — blank or oversized user_id.
    503 — ArangoDB unreachable (raised by ``get_db()``).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from loguru import logger

from ... import api as tom_api

router = APIRouter()


@router.get("/tom/{user_id}")
def get_tom(user_id: str, agent_id: str = "horus") -> dict[str, Any]:
    """Consolidated Theory of Mind: user + persona + relationship."""
    if not user_id or not user_id.strip() or len(user_id) > 100:
        raise HTTPException(status_code=400, detail="Invalid user_id")

    # -- user profile -------------------------------------------------------
    user: dict[str, Any] = {}
    try:
        user = tom_api.get_or_create_user(user_id=user_id)
    except Exception as exc:
        logger.warning("ToM: user lookup failed for {}: {}", user_id, exc)

    # -- persona state ------------------------------------------------------
    persona_state: dict[str, Any] = {}
    try:
        persona_state = tom_api.get_or_create_persona_state(agent_id=agent_id)
    except Exception as exc:
        logger.warning("ToM: persona_state lookup failed for {}: {}", agent_id, exc)

    # -- relationship -------------------------------------------------------
    relationship: dict[str, Any] = {}
    try:
        relationship = tom_api.get_or_create_relationship(
            user_id=user_id, agent_id=agent_id,
        )
    except Exception as exc:
        logger.warning(
            "ToM: relationship lookup failed for {}/{}: {}",
            user_id, agent_id, exc,
        )

    return {
        "user": user,
        "persona_state": persona_state,
        "relationship": relationship,
    }
