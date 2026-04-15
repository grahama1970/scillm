"""Theory of Mind and persona management: user, persona, relationship, lore sub-apps."""
from __future__ import annotations
from loguru import logger

import json
from typing import Optional, List

import typer

from ._helpers import app, _json_output

# =============================================================================
# USER COMMANDS - User profile management
# =============================================================================

user_app = typer.Typer(help="User profile management for persona agents.")
app.add_typer(user_app, name="user")


@user_app.command("get")
def user_get(
    user_id: str = typer.Argument(..., help="User ID to get or create"),
    display_name: str = typer.Option("", "--name", help="Display name (for new users)"),
    scope: str = typer.Option("", "--scope", help="Scope for the user"),
    skill: str = typer.Option("unknown", "--skill", help="Initial skill level (novice, intermediate, expert, unknown)"),
) -> None:
    """Get or create a user profile."""
    from .. import api

    result = api.get_or_create_user(
        user_id=user_id,
        display_name=display_name,
        scope=scope,
        initial_skill_level=skill,
    )
    _json_output(result)


@user_app.command("update")
def user_update(
    user_id: str = typer.Argument(..., help="User ID to update"),
    skill: str = typer.Option(None, "--skill", help="New skill level"),
    worthiness: float = typer.Option(None, "--worthiness", help="Worthiness score (0.0-1.0)"),
    topic: Optional[List[str]] = typer.Option(None, "--topic", help="Topics to add"),
    notes: str = typer.Option(None, "--notes", help="Agent notes about user"),
) -> None:
    """Update user profile."""
    from .. import api

    result = api.update_user_profile(
        user_id=user_id,
        skill_level=skill,
        worthiness_score=worthiness,
        add_topics=topic,
        notes=notes,
    )
    _json_output(result)


@user_app.command("history")
def user_history(
    user_id: str = typer.Argument(..., help="User ID to look up"),
    limit: int = typer.Option(20, "--limit", help="Max history items"),
    episodes: bool = typer.Option(True, "--episodes/--no-episodes", help="Include episodes"),
    lessons: bool = typer.Option(True, "--lessons/--no-lessons", help="Include lessons helped"),
) -> None:
    """Get comprehensive user history."""
    from .. import api

    result = api.get_user_history(
        user_id=user_id,
        include_episodes=episodes,
        include_lessons_helped=lessons,
        limit=limit,
    )
    _json_output(result)


# =============================================================================
# PERSONA COMMANDS - Persona state management
# =============================================================================

persona_app = typer.Typer(help="Persona state management for AI agents.")
app.add_typer(persona_app, name="persona")


@persona_app.command("get")
def persona_get(
    agent_id: str = typer.Argument(..., help="Agent/persona ID"),
    mood: str = typer.Option("neutral", "--mood", help="Default mood (for new personas)"),
) -> None:
    """Get or create a persona state."""
    from .. import api

    result = api.get_or_create_persona_state(
        agent_id=agent_id,
        default_mood=mood,
    )
    _json_output(result)


@persona_app.command("update")
def persona_update(
    agent_id: str = typer.Argument(..., help="Agent/persona ID"),
    mood: str = typer.Option(None, "--mood", help="New mood state"),
    drive: Optional[List[str]] = typer.Option(None, "--drive", help="Drive update (format: name:satisfaction or name:satisfaction:intensity)"),
    coping: str = typer.Option(None, "--coping", help="Coping mechanism used"),
    trigger: str = typer.Option(None, "--trigger", help="What triggered this change"),
    user_id: str = typer.Option(None, "--user", help="User who triggered the change"),
    history: bool = typer.Option(True, "--history/--no-history", help="Record in history"),
) -> None:
    """Update persona state."""
    from .. import api

    # Parse drive updates
    drive_updates = None
    if drive:
        drive_updates = {}
        for d in drive:
            parts = d.split(":")
            if len(parts) >= 2:
                name = parts[0]
                satisfaction = float(parts[1])
                intensity = float(parts[2]) if len(parts) > 2 else None
                drive_updates[name] = {"satisfaction": satisfaction}
                if intensity is not None:
                    drive_updates[name]["intensity"] = intensity

    result = api.update_persona_state(
        agent_id=agent_id,
        mood=mood,
        drive_updates=drive_updates,
        coping_mechanism_used=coping,
        trigger=trigger,
        user_id=user_id,
        record_history=history,
    )
    _json_output(result)


@persona_app.command("trend")
def persona_trend(
    agent_id: str = typer.Argument(..., help="Agent/persona ID"),
    hours: int = typer.Option(24, "--hours", help="Hours of history to include"),
    drive: str = typer.Option(None, "--drive", help="Specific drive to track"),
) -> None:
    """Get persona state trend over time."""
    from .. import api

    result = api.get_persona_state_trend(
        agent_id=agent_id,
        hours=hours,
        drive_name=drive,
    )
    _json_output(result)


# =============================================================================
# RELATIONSHIP COMMANDS - User-agent relationship management
# =============================================================================

relationship_app = typer.Typer(help="User-agent relationship management.")
app.add_typer(relationship_app, name="relationship")


@relationship_app.command("get")
def relationship_get(
    user_id: str = typer.Argument(..., help="User ID"),
    agent_id: str = typer.Argument(..., help="Agent/persona ID"),
) -> None:
    """Get or create a user-agent relationship."""
    from .. import api

    result = api.get_or_create_relationship(
        user_id=user_id,
        agent_id=agent_id,
    )
    _json_output(result)


@relationship_app.command("update")
def relationship_update(
    user_id: str = typer.Argument(..., help="User ID"),
    agent_id: str = typer.Argument(..., help="Agent/persona ID"),
    trust: str = typer.Option(None, "--trust", help="Trust delta (e.g., +0.1 or -0.05)"),
    respect: str = typer.Option(None, "--respect", help="Respect delta"),
    familiarity: str = typer.Option(None, "--familiarity", help="Familiarity delta"),
) -> None:
    """Update relationship with deltas."""
    from .. import api

    trust_delta = 0.0
    respect_delta = 0.0
    familiarity_delta = 0.0

    if trust:
        trust_delta = float(trust.replace("+", ""))
    if respect:
        respect_delta = float(respect.replace("+", ""))
    if familiarity:
        familiarity_delta = float(familiarity.replace("+", ""))

    result = api.update_relationship(
        user_id=user_id,
        agent_id=agent_id,
        trust_delta=trust_delta,
        respect_delta=respect_delta,
        familiarity_delta=familiarity_delta,
    )
    _json_output(result)


@relationship_app.command("moment")
def relationship_moment(
    user_id: str = typer.Argument(..., help="User ID"),
    agent_id: str = typer.Argument(..., help="Agent/persona ID"),
    event: str = typer.Option(..., "--event", help="Description of the key moment"),
    impact: float = typer.Option(..., "--impact", help="Impact magnitude (-1.0 to 1.0)"),
    update_trust: bool = typer.Option(False, "--update-trust", help="Also update trust"),
    update_respect: bool = typer.Option(False, "--update-respect", help="Also update respect"),
) -> None:
    """Record a key moment in the relationship."""
    from .. import api

    result = api.record_key_moment(
        user_id=user_id,
        agent_id=agent_id,
        event=event,
        impact=impact,
        update_trust=update_trust,
        update_respect=update_respect,
    )
    _json_output(result)


# =============================================================================
# LORE COMMANDS - Persona knowledge graph retrieval
# =============================================================================

lore_app = typer.Typer(help="Persona lore retrieval for character agents.")
app.add_typer(lore_app, name="lore")


@lore_app.command("query")
def lore_query(
    agent_id: str = typer.Argument(..., help="Persona agent ID (e.g., 'horus')"),
    query: str = typer.Option(..., "--q", "-q", help="Query to search lore"),
    k: int = typer.Option(5, "--k", help="Number of results"),
    graph: bool = typer.Option(True, "--graph/--no-graph", help="Include graph-traversed related docs"),
    content_type: str = typer.Option("", "--type", help="Filter by content type (e.g., 'canon')"),
    bm25_weight: float = typer.Option(0.4, "--bm25", help="BM25 text search weight"),
    semantic_weight: float = typer.Option(0.6, "--semantic", help="Semantic search weight"),
    subconscious: bool = typer.Option(False, "--subconscious", help="Format as subconscious prompt section"),
    user_id: str = typer.Option("", "--user", help="User ID for Theory of Mind context"),
) -> None:
    """Query persona lore with hybrid search.

    Uses BM25 + semantic + graph traversal to find relevant lore.
    Returns items with mood, trauma triggers, and character names.

    Example:
        memory-agent lore query horus --q "Tell me about Erebus"
        memory-agent lore query horus --q "The siege of Terra" --subconscious
    """
    from ..lore import query_lore, format_subconscious

    result = query_lore(
        agent_id=agent_id,
        query=query,
        top_k=k,
        include_graph=graph,
        content_type=content_type or None,
        bm25_weight=bm25_weight,
        semantic_weight=semantic_weight,
    )

    if subconscious:
        output = format_subconscious(result, user_id=user_id or None)
        print(output)
    else:
        _json_output(result.to_dict())


@lore_app.command("status")
def lore_status(
    agent_id: str = typer.Argument(..., help="Persona agent ID (e.g., 'horus')"),
) -> None:
    """Check lore database status for a persona.

    Shows collection sizes, embedding coverage, and edge counts.

    Example:
        memory-agent lore status horus
    """
    from ..arango_client import get_db

    db = get_db()
    prefix = f"{agent_id}_lore"

    status = {
        "agent_id": agent_id,
        "collections": {},
        "views": {},
        "graphs": {},
    }

    # Check collections
    for suffix in ["docs", "chunks", "edges"]:
        coll_name = f"{prefix}_{suffix}"
        if db.has_collection(coll_name):
            coll = db.collection(coll_name)
            count = coll.count()
            status["collections"][coll_name] = {"count": count}

            # Check embedding coverage for docs
            if suffix == "docs":
                try:
                    with_emb = list(db.aql.execute(
                        f"RETURN LENGTH(FOR d IN {coll_name} FILTER d.embedding != null RETURN 1)"
                    ))[0]
                    with_abstract = list(db.aql.execute(
                        f"RETURN LENGTH(FOR d IN {coll_name} FILTER d.abstract != null RETURN 1)"
                    ))[0]
                    status["collections"][coll_name]["with_embedding"] = with_emb
                    status["collections"][coll_name]["with_abstract"] = with_abstract
                except Exception as exc:
                    logger.error("Suppressed error in tom: {}", exc)
        else:
            status["collections"][coll_name] = None

    # Check views
    for suffix in ["docs_search", "chunks_search"]:
        view_name = f"{prefix}_{suffix}"
        try:
            view = db.view(view_name)
            status["views"][view_name] = {"exists": True, "type": view.get("type", "unknown")}
        except Exception as exc:
            logger.error("Suppressed error in tom: {}", exc)
            status["views"][view_name] = None

    # Check graphs
    graph_name = f"{prefix}_graph"
    try:
        graph = db.graph(graph_name)
        status["graphs"][graph_name] = {
            "exists": True,
            "vertex_collections": list(graph.vertex_collections()),
            "edge_definitions": [ed["edge_collection"] for ed in graph.edge_definitions()],
        }
    except Exception as exc:
        logger.error("Suppressed error in tom: {}", exc)
        status["graphs"][graph_name] = None

    _json_output(status)


# =============================================================================
# THEORY OF MIND - imported from tom_advanced.py
# =============================================================================

from .tom_advanced import tom_app  # noqa: E402

app.add_typer(tom_app, name="tom")
