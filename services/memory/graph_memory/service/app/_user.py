"""User endpoints: get, update, history."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ... import api as tom_api
from ._models import UserGetRequest, UserUpdateRequest, UserHistoryRequest

router = APIRouter()


@router.post("/user/get")
def user_get(req: UserGetRequest) -> dict:
    """Get or create a user profile."""
    if not req.user_id.strip():
        raise HTTPException(status_code=400, detail="user_id is required")
    return tom_api.get_or_create_user(
        user_id=req.user_id,
        display_name=req.display_name,
        scope=req.scope,
        initial_skill_level=req.initial_skill_level,
    )


@router.post("/user/update")
def user_update(req: UserUpdateRequest) -> dict:
    """Update user profile."""
    if not req.user_id.strip():
        raise HTTPException(status_code=400, detail="user_id is required")
    return tom_api.update_user_profile(
        user_id=req.user_id,
        skill_level=req.skill_level,
        worthiness_score=req.worthiness_score,
        add_topics=req.add_topics,
        notes=req.notes,
    )


@router.post("/user/history")
def user_history(req: UserHistoryRequest) -> dict:
    """Get comprehensive user history."""
    if not req.user_id.strip():
        raise HTTPException(status_code=400, detail="user_id is required")
    return tom_api.get_user_history(
        user_id=req.user_id,
        include_episodes=req.include_episodes,
        include_lessons_helped=req.include_lessons_helped,
        limit=req.limit,
    )
