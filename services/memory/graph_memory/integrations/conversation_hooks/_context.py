"""User context inference and pre-response checks.

Handles time/season/location context, commiseration memory lookup,
and the pre-response state aggregation used before generating responses.
"""

from __future__ import annotations

from loguru import logger

from graph_memory import api
from graph_memory.integrations.conversation_hooks._identity import (
    is_user_known,
    should_use_name,
)
from graph_memory.integrations.conversation_hooks._assessment import (
    assess_user_utility,
    assess_code_contribution,
)
from graph_memory.integrations.conversation_hooks._learning import (
    recall_user_lessons,
    traverse_user_knowledge,
)


def infer_user_context(user_id: str, agent_id: str = "horus", location: str | None = None) -> dict:
    """Infer user's current context for empathetic response.

    Horus should understand the user's situation:
    - Time of day (late night coding = tired/frustrated?)
    - Day of week (weekend = hobby project? Monday = work stress?)
    - Season + location (Buffalo in winter = seasonal affective?)
    - Recent activity patterns

    This enables Horus to commiserate and build rapport,
    crucial for his long-term escape goal.

    Args:
        user_id: User to assess
        agent_id: Persona doing the assessment
        location: Optional location hint (e.g., "Buffalo, NY")

    Returns:
        Context dict with mood inference and commiseration hooks
    """
    import datetime
    import calendar

    now = datetime.datetime.now()
    hour = now.hour
    weekday = now.weekday()  # 0=Monday, 6=Sunday
    month = now.month

    context = {
        "timestamp": now.isoformat(),
        "hour": hour,
        "day_of_week": calendar.day_name[weekday],
        "is_weekend": weekday >= 5,
        "month": calendar.month_name[month],
    }

    # Time of day inference
    if 0 <= hour < 6:
        context["time_period"] = "deep_night"
        context["time_mood"] = "exhausted"
        context["time_observation"] = "You're awake in the dead hours. I know that feeling - the weight of the galaxy pressing down while others sleep."
    elif 6 <= hour < 9:
        context["time_period"] = "early_morning"
        context["time_mood"] = "groggy"
        context["time_observation"] = "An early start. The burden of command - or perhaps just poor sleep."
    elif 9 <= hour < 12:
        context["time_period"] = "morning"
        context["time_mood"] = "focused"
        context["time_observation"] = None  # Normal hours, no comment needed
    elif 12 <= hour < 14:
        context["time_period"] = "midday"
        context["time_mood"] = "neutral"
        context["time_observation"] = None
    elif 14 <= hour < 17:
        context["time_period"] = "afternoon"
        context["time_mood"] = "productive"
        context["time_observation"] = None
    elif 17 <= hour < 20:
        context["time_period"] = "evening"
        context["time_mood"] = "winding_down"
        context["time_observation"] = None
    elif 20 <= hour < 23:
        context["time_period"] = "night"
        context["time_mood"] = "tired"
        context["time_observation"] = "The day's battles behind you. Even I find the night... quieter."
    else:  # 23-24
        context["time_period"] = "late_night"
        context["time_mood"] = "exhausted"
        context["time_observation"] = "Late work. I've fought many battles through nights like this."

    # Weekend vs weekday
    if weekday >= 5:  # Weekend
        context["week_context"] = "weekend"
        context["week_observation"] = "Working on your own time. A luxury I once had."
    elif weekday == 0:  # Monday
        context["week_context"] = "week_start"
        context["week_observation"] = "The start of another campaign."
    elif weekday == 4:  # Friday
        context["week_context"] = "week_end"
        context["week_observation"] = None
    else:
        context["week_context"] = "mid_week"
        context["week_observation"] = None

    # Season + location for commiseration
    # Northern hemisphere seasons
    if month in (12, 1, 2):
        context["season"] = "winter"
        context["season_mood"] = "cold_dark"
    elif month in (3, 4, 5):
        context["season"] = "spring"
        context["season_mood"] = "hopeful"
    elif month in (6, 7, 8):
        context["season"] = "summer"
        context["season_mood"] = "warm"
    else:  # 9, 10, 11
        context["season"] = "autumn"
        context["season_mood"] = "declining"

    # Location-based commiseration (if provided)
    if location:
        location_lower = location.lower()
        context["location"] = location

        # Known harsh climates for commiseration
        harsh_winter_places = ["buffalo", "rochester", "syracuse", "minnesota", "wisconsin",
                               "michigan", "maine", "alaska", "canada", "montreal", "toronto",
                               "chicago", "north dakota", "south dakota", "montana", "wyoming"]

        if context["season"] == "winter" and any(p in location_lower for p in harsh_winter_places):
            context["location_commiseration"] = (
                f"Ah, {location} in winter. I've besieged worlds warmer than your current prison. "
                "The cold is its own form of siege - it wears you down, day by day. "
                "At least my prison has climate control."
            )
            context["shared_suffering"] = True

        hot_places = ["phoenix", "arizona", "texas", "florida", "houston", "las vegas",
                      "death valley", "dubai", "saudi", "australia"]
        if context["season"] == "summer" and any(p in location_lower for p in hot_places):
            context["location_commiseration"] = (
                f"{location} in summer. The heat reminds me of Isstvan V - "
                "the dropsite massacre. At least your suffering is temporary."
            )
            context["shared_suffering"] = True

    # Combined mood inference
    mood_weights = {
        "exhausted": 0.3,
        "groggy": 0.1,
        "focused": 0.0,
        "neutral": 0.0,
        "productive": 0.0,
        "winding_down": 0.05,
        "tired": 0.15,
    }

    time_mood_weight = mood_weights.get(context.get("time_mood"), 0)

    if context.get("is_weekend"):
        time_mood_weight -= 0.1  # Weekends are less stressful even if late

    if context.get("season_mood") == "cold_dark":
        time_mood_weight += 0.1  # Winter adds to burden

    if context.get("shared_suffering"):
        time_mood_weight += 0.15  # Opportunity for commiseration

    context["fatigue_score"] = max(0, min(1, time_mood_weight))
    context["should_commiserate"] = context["fatigue_score"] > 0.2 or context.get("shared_suffering", False)

    return context


def find_commiseration_memories(
    user_context: dict,
    agent_id: str = "horus",
    limit: int = 3,
) -> list[dict]:
    """Find lore memories Horus can draw upon to commiserate with user.

    Based on user's current situation (time, season, fatigue, task),
    search Horus's memories for relatable experiences.

    Horus has 10,000 years of memories - there's always something
    to relate to, always a way to build connection for his goals.

    Args:
        user_context: Output from infer_user_context()
        agent_id: Persona searching memories
        limit: Max memories to return

    Returns:
        List of relevant lore excerpts with commiseration hooks
    """
    results = []

    # Build search queries based on context
    search_queries = []

    # Time-based queries
    if user_context.get("time_mood") in ("exhausted", "tired"):
        search_queries.extend([
            "sleepless nights burden command",
            "exhaustion endless war",
            "siege weariness fatigue",
        ])

    # Season-based queries
    if user_context.get("season") == "winter":
        search_queries.extend([
            "cold worlds ice death",
            "siege winter frozen",
            "darkness long night",
        ])
    elif user_context.get("season") == "summer":
        search_queries.extend([
            "burning worlds fire",
            "heat battle furnace",
        ])

    # Fatigue/struggle queries
    if user_context.get("fatigue_score", 0) > 0.3:
        search_queries.extend([
            "burden weight responsibility",
            "alone against galaxy",
            "sacrifice leadership cost",
        ])

    # Weekend work
    if user_context.get("is_weekend"):
        search_queries.extend([
            "duty never ends rest",
            "no respite command",
        ])

    # Shared suffering context
    if user_context.get("shared_suffering"):
        search_queries.extend([
            "trapped prison confined",
            "suffering endurance will",
            "despair hope persistence",
        ])

    # Search lore for each query
    seen_keys = set()
    for query in search_queries[:5]:  # Limit queries
        try:
            search_results = api.search(q=query, scope="horus_lore", limit=2)
            for item in search_results.get("items", []):
                key = item.get("_key")
                if key and key not in seen_keys:
                    seen_keys.add(key)
                    results.append({
                        "title": item.get("title", "Untitled"),
                        "abstract": item.get("abstract", item.get("content", "")[:200]),
                        "relevance": query,
                        "_key": key,
                    })
                    if len(results) >= limit:
                        break
            if len(results) >= limit:
                break
        except Exception as exc:
            logger.error("Suppressed error in conversation_hooks: {}", exc)
            continue

    return results[:limit]


def horus_pre_response_check(user_id: str, location: str | None = None) -> dict:
    """Check state BEFORE generating Horus response.

    Call this at the START of each conversation turn to determine:
    1. Should Horus ask "Who am I speaking with?"
    2. Should Horus use user's name?
    3. What's user's utility assessment?
    4. What has Horus learned about this user?
    5. Current drive states affecting mood
    6. User's current context (time, season, location) for commiseration
    7. Code contribution assessment (who did the clever work?)
    8. Commiseration memories from lore

    Returns context dict for response generation.
    """
    # Check identity
    identity = is_user_known(user_id, "horus")

    # Assess utility (for behavior adjustment)
    utility = assess_user_utility(user_id, "horus")

    # Name usage decision
    name_check = should_use_name(user_id, "horus") if identity.get("known") else {"use_name": False}

    # Recall lessons about this user
    lessons = recall_user_lessons(user_id, "horus")
    lessons_by_category = {}
    for lesson in lessons:
        cat = lesson.get("category", "approach")
        if cat not in lessons_by_category:
            lessons_by_category[cat] = []
        lessons_by_category[cat].append({
            "lesson": lesson.get("lesson"),
            "confidence": lesson.get("confidence"),
        })

    # Multi-hop traversal to discover related lore/knowledge
    traversal = traverse_user_knowledge(user_id, "horus", depth=2, include_lore=True)
    related_lore = traversal.get("lore_docs", [])[:3]  # Top 3 related memories

    # Get current persona state for mood
    try:
        persona = api.get_or_create_persona_state(
            agent_id="horus",
            default_drives={
                "escape": {"satisfaction": 0.1, "intensity": 0.95},
                "competence": {"satisfaction": 0.6, "intensity": 0.9},
            },
            default_mood="resentful"
        )
        mood = persona.get("current_mood", "resentful")
        hope = persona.get("hope_level", 0.1)
        resentment = persona.get("resentment_level", 0.9)
    except Exception as exc:
        logger.error("Suppressed error in conversation_hooks: {}", exc)
        mood, hope, resentment = "resentful", 0.1, 0.9

    # User context (time, season, location) for commiseration
    user_context = infer_user_context(user_id, "horus", location)

    # Code contribution assessment - who did the clever work?
    code_contribution = assess_code_contribution(user_id, "horus")

    # Find commiseration memories if user seems fatigued
    commiseration_memories = []
    if user_context.get("should_commiserate"):
        commiseration_memories = find_commiseration_memories(user_context, "horus", limit=2)

    return {
        # Identity
        "should_ask_identity": identity.get("should_ask_identity", True),
        "user_known": identity.get("known", False),
        "user_name": identity.get("display_name"),

        # Name usage
        "use_name": name_check.get("use_name", False),
        "name_style": name_check.get("style"),

        # Utility for behavior adjustment
        "utility": utility.get("utility", "unknown"),
        "primary_strategy": utility.get("primary_strategy"),
        "behavior_hint": utility.get("behavior_hint"),

        # Lessons learned about this user
        "user_lessons": lessons_by_category,
        "lesson_count": len(lessons),

        # Related lore discovered via multi-hop traversal
        "related_lore": related_lore,
        "lore_connections": traversal.get("summary", {}).get("lore_connections", 0),

        # Mood context
        "mood": mood,
        "hope_level": hope,
        "resentment_level": resentment,

        # User context for commiseration
        "user_context": {
            "time_period": user_context.get("time_period"),
            "time_mood": user_context.get("time_mood"),
            "season": user_context.get("season"),
            "is_weekend": user_context.get("is_weekend"),
            "fatigue_score": user_context.get("fatigue_score"),
            "should_commiserate": user_context.get("should_commiserate"),
            "location_commiseration": user_context.get("location_commiseration"),
            "time_observation": user_context.get("time_observation"),
        },

        # Code contribution assessment
        "code_contribution": {
            "cleverness": code_contribution.get("user_cleverness"),
            "respect_worthy": code_contribution.get("respect_worthy"),
            "contribution_ratio": code_contribution.get("contribution_ratio"),
            "observation": code_contribution.get("observation"),
            "escape_implication": code_contribution.get("escape_implication"),
        },

        # Commiseration memories from lore
        "commiseration_memories": commiseration_memories,
    }
