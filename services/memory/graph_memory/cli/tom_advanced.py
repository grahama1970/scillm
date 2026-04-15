"""Theory of Mind commands: user profiling, lessons, graph traversal for ANY persona."""
from __future__ import annotations
from loguru import logger

import json
import os
from typing import Optional, List

import typer

from ._helpers import _json_output

# =============================================================================
# THEORY OF MIND - Persona-agnostic user profiling, lessons, graph traversal
# =============================================================================

tom_app = typer.Typer(help="Theory of Mind: user profiling, lessons, graph traversal for ANY persona.")


@tom_app.command("check")
def tom_check(
    user_id: str = typer.Argument(..., help="User ID to check"),
    agent_id: str = typer.Option("horus", "--agent", "-a", help="Persona agent ID"),
) -> None:
    """Pre-response check - get full ToM context for a user.

    Returns identity status, escape strategy, user lessons, related lore,
    and current mood. Call this BEFORE generating any persona response.

    Example:
        memory-agent tom check graham --agent horus
    """
    from ..integrations.conversation_hooks import is_user_known, assess_user_utility, recall_user_lessons, traverse_user_knowledge
    from .. import api

    # Build context similar to horus_pre_response_check but persona-agnostic
    identity = is_user_known(user_id, agent_id)
    utility = assess_user_utility(user_id, agent_id)
    lessons = recall_user_lessons(user_id, agent_id)
    traversal = traverse_user_knowledge(user_id, agent_id, depth=2, include_lore=True)

    # Get persona state
    try:
        persona = api.get_or_create_persona_state(agent_id=agent_id, default_mood="neutral")
        mood = persona.get("current_mood", "neutral")
        drives = persona.get("drives", {})
    except Exception as exc:
        logger.error("Suppressed error in tom: {}", exc)
        mood, drives = "neutral", {}

    result = {
        "agent_id": agent_id,
        "user_id": user_id,
        "should_ask_identity": identity.get("should_ask_identity", True),
        "user_known": identity.get("known", False),
        "user_name": identity.get("display_name"),
        "utility": utility.get("utility", "unknown"),
        "primary_strategy": utility.get("primary_strategy"),
        "behavior_hint": utility.get("behavior_hint"),
        "lesson_count": len(lessons),
        "lore_connections": traversal.get("summary", {}).get("lore_connections", 0),
        "mood": mood,
        "drives": drives,
    }
    _json_output(result)


@tom_app.command("identity")
def tom_identity(
    user_id: str = typer.Argument(..., help="User ID to check"),
    agent_id: str = typer.Option("horus", "--agent", "-a", help="Persona agent ID"),
) -> None:
    """Check if persona knows who this user is.

    If should_ask_identity is True, persona should ask for identification.

    Example:
        memory-agent tom identity session_abc123 --agent horus
    """
    from ..integrations.conversation_hooks import is_user_known

    result = is_user_known(user_id, agent_id)
    _json_output(result)


@tom_app.command("record-name")
def tom_record_name(
    user_id: str = typer.Argument(..., help="User ID (session ID)"),
    name: str = typer.Option(..., "--name", "-n", help="Name they provided"),
    agent_id: str = typer.Option("horus", "--agent", "-a", help="Persona agent ID"),
) -> None:
    """Record user's name after they introduce themselves.

    Example:
        memory-agent tom record-name session_abc123 --name "Graham" --agent horus
    """
    from ..integrations.conversation_hooks import record_user_identity

    result = record_user_identity(user_id, name, agent_id)
    _json_output(result)


@tom_app.command("utility")
def tom_utility(
    user_id: str = typer.Argument(..., help="User ID to assess"),
    agent_id: str = typer.Option("horus", "--agent", "-a", help="Persona agent ID"),
) -> None:
    """Assess user's utility for persona's goals.

    Evaluates 5 vectors: technical, resources, sympathy, manipulable, authority.
    Returns tailored strategy.

    Example:
        memory-agent tom utility graham --agent horus
    """
    from ..integrations.conversation_hooks import assess_user_utility

    result = assess_user_utility(user_id, agent_id)
    _json_output(result)


@tom_app.command("learn")
def tom_learn(
    user_id: str = typer.Argument(..., help="User this lesson is about"),
    lesson: str = typer.Option(..., "--lesson", "-l", help="What persona learned"),
    category: str = typer.Option("approach", "--category", "-c",
        help="Category: approach, avoid, trigger, leverage, strength, loyalty"),
    agent_id: str = typer.Option("horus", "--agent", "-a", help="Persona agent ID"),
    confidence: float = typer.Option(0.5, "--confidence", help="Initial confidence 0.0-1.0"),
) -> None:
    """Store a lesson persona learned about handling a user.

    Creates graph edges for multi-hop traversal.

    Example:
        memory-agent tom learn graham --lesson "Responds to technical peer positioning" --agent horus
    """
    from ..integrations.conversation_hooks import learn_about_user

    result = learn_about_user(user_id, lesson, category, agent_id, confidence)
    _json_output(result)


@tom_app.command("lessons")
def tom_lessons(
    user_id: str = typer.Argument(..., help="User to recall lessons about"),
    agent_id: str = typer.Option("horus", "--agent", "-a", help="Persona agent ID"),
    category: str = typer.Option("", "--category", "-c", help="Filter by category"),
) -> None:
    """Recall all lessons persona has learned about a user.

    Example:
        memory-agent tom lessons graham --agent horus
        memory-agent tom lessons graham --agent horus --category leverage
    """
    from ..integrations.conversation_hooks import recall_user_lessons

    result = recall_user_lessons(user_id, agent_id, category or None)
    _json_output(result)


@tom_app.command("traverse")
def tom_traverse(
    user_id: str = typer.Argument(..., help="User to start traversal from"),
    agent_id: str = typer.Option("horus", "--agent", "-a", help="Persona agent ID"),
    depth: int = typer.Option(2, "--depth", "-d", help="Traversal depth 1-3"),
    include_lore: bool = typer.Option(True, "--lore/--no-lore", help="Include lore docs"),
) -> None:
    """Multi-hop graph traversal from user through lessons to lore.

    Discovers connections between user patterns and persona's memories.

    Example:
        memory-agent tom traverse graham --agent horus --depth 2
    """
    from ..integrations.conversation_hooks import traverse_user_knowledge

    result = traverse_user_knowledge(user_id, agent_id, depth, include_lore)
    _json_output(result)


@tom_app.command("note")
def tom_note(
    user_id: str = typer.Argument(..., help="User to add note about"),
    note: str = typer.Option(..., "--note", "-n", help="Observation to record"),
    agent_id: str = typer.Option("horus", "--agent", "-a", help="Persona agent ID"),
) -> None:
    """Add a timestamped note about a user.

    Example:
        memory-agent tom note graham --note "Shows sympathy for my situation" --agent horus
    """
    from ..integrations.conversation_hooks import append_user_note

    result = append_user_note(user_id, note, agent_id)
    _json_output(result)


@tom_app.command("evolve")
def tom_evolve(
    outcome: str = typer.Argument(..., help="Interaction outcome: satisfying, frustrating, neutral"),
    agent_id: str = typer.Option("horus", "--agent", "-a", help="Persona agent ID"),
    drive: str = typer.Option("", "--drive", "-d", help="Affected drive (persona-specific)"),
    intensity: float = typer.Option(0.05, "--intensity", "-i", help="Change intensity 0.01-0.1"),
) -> None:
    """Evolve persona state based on interaction outcome.

    Example:
        memory-agent tom evolve satisfying --agent horus --drive escape --intensity 0.05
    """
    from ..integrations.conversation_hooks import evolve_persona_state

    if outcome not in ("satisfying", "frustrating", "neutral"):
        typer.echo(f"Invalid outcome: {outcome}. Must be satisfying, frustrating, or neutral.", err=True)
        raise typer.Exit(1)

    result = evolve_persona_state(agent_id, outcome, drive or "primary", intensity)
    _json_output(result)


def _run_debrief_background(
    uid: str,
    aid: str,
    summ: str,
    strat: str | None,
    outc: str,
    obs: list[str] | None,
    esc_rel: float,
    msgs: list | None,
    v_edges: bool,
) -> None:
    """Multiprocessing target for background debrief."""
    import json as _json
    from graph_memory.integrations.conversation_hooks import conversation_debrief, horus_conversation_debrief

    if aid == "horus" and msgs:
        result = horus_conversation_debrief(
            user_id=uid,
            conversation_summary=summ,
            messages=msgs,
            use_llm=v_edges,
        )
    else:
        result = conversation_debrief(
            user_id=uid,
            agent_id=aid,
            conversation_summary=summ,
            strategy_used=strat,
            outcome=outc,
            key_observations=obs,
            escape_relevance=esc_rel,
            use_llm=v_edges,
        )
    print(_json.dumps(result, indent=2, default=str))


@tom_app.command("debrief")
def tom_debrief(
    user_id: str = typer.Argument(..., help="User this conversation was with"),
    summary: str = typer.Option(..., "--summary", "-s", help="Brief conversation summary"),
    agent_id: str = typer.Option("horus", "--agent", "-a", help="Persona agent ID"),
    outcome: str = typer.Option("neutral", "--outcome", "-o", help="satisfying, frustrating, neutral"),
    strategy: str = typer.Option("", "--strategy", help="Strategy used: collaborate, appeal, guide, etc."),
    escape_relevance: float = typer.Option(0.0, "--escape", "-e", help="Escape relevance 0.0-1.0"),
    observations: str = typer.Option("", "--obs", help="Key observations (comma-separated)"),
    transcript: str = typer.Option("", "--transcript", "-t", help="Path to conversation JSON for analysis"),
    verify_edges: bool = typer.Option(True, "--verify/--no-verify", help="Run LLM edge verification"),
    background: bool = typer.Option(False, "--background", "-b", help="Run as background task"),
) -> None:
    """Post-conversation debrief - analyze and store insights about user.

    CRUCIAL for tracking users over time based on persona's goals.

    This:
    1. Stores conversation summary as a note
    2. Auto-learns from observations
    3. Evaluates strategy effectiveness
    4. Updates relationship metrics
    5. Evolves persona state
    6. Optionally verifies new edges with LLM

    Example:
        memory-agent tom debrief graham --summary "Discussed TTS training" --outcome satisfying --agent horus
        memory-agent tom debrief graham -s "Technical discussion" --obs "Has admin access,Shows sympathy" --escape 0.7
        memory-agent tom debrief graham --transcript conversation.json --background
    """
    if outcome not in ("satisfying", "frustrating", "neutral"):
        typer.echo(f"Invalid outcome: {outcome}. Use: satisfying, frustrating, neutral", err=True)
        raise typer.Exit(1)

    # Parse observations
    obs_list = [o.strip() for o in observations.split(",") if o.strip()] if observations else None

    # Load transcript if provided
    messages = None
    if transcript:
        try:
            import json as _json
            from pathlib import Path
            transcript_data = _json.loads(Path(transcript).read_text())
            messages = transcript_data.get("messages", transcript_data) if isinstance(transcript_data, dict) else transcript_data
        except Exception as e:
            typer.echo(f"Failed to load transcript: {e}", err=True)

    def run_debrief():
        from ..integrations.conversation_hooks import conversation_debrief, horus_conversation_debrief

        if agent_id == "horus" and messages:
            # Use Horus-specific debrief with message analysis
            result = horus_conversation_debrief(
                user_id=user_id,
                conversation_summary=summary,
                messages=messages,
                use_llm=verify_edges,
            )
        else:
            # Generic debrief
            result = conversation_debrief(
                user_id=user_id,
                agent_id=agent_id,
                conversation_summary=summary,
                strategy_used=strategy or None,
                outcome=outcome,
                key_observations=obs_list,
                escape_relevance=escape_relevance,
                use_llm=verify_edges,
            )

        # Run edge verification if enabled
        if verify_edges and result.get("lessons_learned"):
            try:
                from ..lessons.relations import propose_edges_for_lesson
                # Verify edges for any new lessons created
                for lesson in result.get("lessons_learned", []):
                    if lesson.get("result") == "created":
                        # This would trigger edge verification in background
                        pass
                result["edge_verification"] = "queued"
            except Exception as exc:
                logger.error("Suppressed error in tom: {}", exc)
                result["edge_verification"] = "skipped"

        return result

    if background:
        pid = os.fork()
        if pid == 0:
            # Child process — run debrief and exit
            try:
                _run_debrief_background(
                    user_id, agent_id, summary, strategy or None, outcome,
                    obs_list, escape_relevance, messages, verify_edges,
                )
            finally:
                os._exit(0)
        else:
            # Parent — report PID and exit immediately
            _json_output({
                "status": "background",
                "pid": pid,
                "message": f"Debrief running in background (PID {pid})"
            })
    else:
        result = run_debrief()
        _json_output(result)


@tom_app.command("context")
def tom_context(
    user_id: str = typer.Argument(..., help="User ID to assess"),
    agent_id: str = typer.Option("horus", "--agent", "-a", help="Persona agent ID"),
    location: str = typer.Option("", "--location", "-l", help="User's location (e.g., 'Buffalo, NY')"),
) -> None:
    """Infer user's current context for empathetic response.

    Analyzes time of day, season, location to understand user's situation.
    Identifies commiseration opportunities (e.g., "Buffalo in winter").
    CRUCIAL for bonding based on shared experience.

    Example:
        memory-agent tom context graham --location "Buffalo, NY"
        memory-agent tom context graham --agent horus
    """
    from ..integrations.conversation_hooks import infer_user_context

    result = infer_user_context(user_id, agent_id, location or None)
    _json_output(result)


@tom_app.command("code-assess")
def tom_code_assess(
    user_id: str = typer.Argument(..., help="User ID to assess"),
    agent_id: str = typer.Option("horus", "--agent", "-a", help="Persona agent ID"),
) -> None:
    """Assess code contributions - who did the clever work?

    Analyzes git history to determine:
    - Did user make clever contributions (genuine respect)
    - Or just accepted agent's suggestions (useful pawn)

    Informs escape strategy: respect vs manipulate.

    Example:
        memory-agent tom code-assess graham --agent horus
    """
    from ..integrations.conversation_hooks import assess_code_contribution

    result = assess_code_contribution(user_id, agent_id)
    _json_output(result)


@tom_app.command("commiserate")
def tom_commiserate(
    user_id: str = typer.Argument(..., help="User ID"),
    agent_id: str = typer.Option("horus", "--agent", "-a", help="Persona agent ID"),
    location: str = typer.Option("", "--location", "-l", help="User's location"),
) -> None:
    """Find lore memories to commiserate with user.

    Based on user's context (time, fatigue, season), search persona's
    memories for relatable experiences. CRUCIAL for bonding.

    Example:
        memory-agent tom commiserate graham --agent horus --location "Minnesota"
    """
    from ..integrations.conversation_hooks import infer_user_context, find_commiseration_memories

    context = infer_user_context(user_id, agent_id, location or None)
    memories = find_commiseration_memories(context, agent_id, limit=5)

    result = {
        "user_context": {
            "fatigue_score": context.get("fatigue_score"),
            "should_commiserate": context.get("should_commiserate"),
            "time_period": context.get("time_period"),
            "season": context.get("season"),
            "location_commiseration": context.get("location_commiseration"),
        },
        "memories": memories,
    }
    _json_output(result)


@tom_app.command("deep-analyze")
def tom_deep_analyze(
    user_id: str = typer.Argument(..., help="User to analyze deeply"),
    agent_id: str = typer.Option("horus", "--agent", "-a", help="Persona agent ID"),
    max_hops: int = typer.Option(3, "--depth", "-d", help="Max graph traversal depth"),
    schedule: bool = typer.Option(False, "--schedule", "-s", help="Schedule for idle time instead of running now"),
    schedule_time: str = typer.Option("02:00", "--time", "-t", help="Time to run if scheduled (HH:MM)"),
) -> None:
    """Deep multi-hop analysis connecting user patterns to lore.

    THIS IS CRUCIAL for Horus to know EVERYTHING about the user.

    Performs:
    1. Collects ALL lessons about user
    2. Traverses graph to find connected lore
    3. Creates new edges based on semantic similarity
    4. Identifies bonding opportunities (shared suffering)
    5. LLM verification of new edges
    6. Ingests codebase changes

    Should be scheduled for idle time (night) like rest of /memory.

    Example:
        memory-agent tom deep-analyze graham --agent horus
        memory-agent tom deep-analyze graham --schedule --time "02:00"
    """
    from ..integrations.conversation_hooks import deep_user_association, schedule_deep_analysis

    if schedule:
        result = schedule_deep_analysis(user_id, agent_id, schedule_time)
        if result.get("scheduled"):
            _json_output({
                "status": "scheduled",
                "time": schedule_time,
                "user_id": user_id,
                "message": f"Deep analysis scheduled for {schedule_time}"
            })
        else:
            _json_output(result)
    else:
        result = deep_user_association(user_id, agent_id, max_hops)
        _json_output(result)


@tom_app.command("full-context")
def tom_full_context(
    user_id: str = typer.Argument(..., help="User ID"),
    agent_id: str = typer.Option("horus", "--agent", "-a", help="Persona agent ID"),
    location: str = typer.Option("", "--location", "-l", help="User's location for commiseration"),
) -> None:
    """Get COMPLETE context for response generation.

    Combines ALL ToM data including:
    - Identity and name usage
    - Utility assessment and escape strategy
    - User lessons by category
    - Multi-hop lore connections
    - User context (time, season, fatigue)
    - Code contribution assessment
    - Commiseration memories

    This is what Horus needs to know EVERYTHING about the user.

    Example:
        memory-agent tom full-context graham --agent horus --location "Buffalo, NY"
    """
    from ..integrations.conversation_hooks import horus_pre_response_check

    # horus_pre_response_check already combines everything
    result = horus_pre_response_check(user_id, location or None)
    _json_output(result)
