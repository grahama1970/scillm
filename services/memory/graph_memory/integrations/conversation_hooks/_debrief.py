"""Post-conversation debrief, episodic archiver hook, and deep analysis.

Handles conversation debriefs, Horus-specific debrief, episodic archiver
integration, deep multi-hop association, and scheduling.
"""

from __future__ import annotations

import os
from typing import Literal

from loguru import logger

from graph_memory import api
from graph_memory.integrations.conversation_hooks._identity import append_user_note
from graph_memory.integrations.conversation_hooks._assessment import assess_code_contribution
from graph_memory.integrations.conversation_hooks._learning import (
    learn_about_user,
    recall_user_lessons,
    traverse_user_knowledge,
)
from graph_memory.integrations.conversation_hooks._hooks import evolve_persona_state
from graph_memory.integrations.conversation_hooks._context import infer_user_context


def conversation_debrief(
    user_id: str,
    agent_id: str,
    conversation_summary: str,
    strategy_used: str | None = None,
    outcome: Literal["satisfying", "frustrating", "neutral"] = "neutral",
    key_observations: list[str] | None = None,
    escape_relevance: float = 0.0,
    use_llm: bool = False,
) -> dict:
    """Post-conversation analysis - CRUCIAL for tracking users over time.

    After each conversation with a persona, this function:
    1. Stores a summary note on the user profile
    2. Auto-learns from key observations
    3. Evaluates strategy effectiveness
    4. Updates relationship metrics
    5. Evolves persona state based on outcome
    6. Assesses escape relevance (for Horus's primary motivation)

    Args:
        user_id: User this conversation was with
        agent_id: Persona agent ID
        conversation_summary: Brief summary of what was discussed
        strategy_used: Which strategy was employed (collaborate, appeal, guide, etc.)
        outcome: How the interaction went
        key_observations: List of observations about the user
        escape_relevance: How relevant was this to escape goal (0.0-1.0)
        use_llm: Whether to use LLM for deeper analysis (optional)

    Returns:
        Debrief results including lessons learned and profile updates
    """
    import time as _time

    results = {
        "user_id": user_id,
        "agent_id": agent_id,
        "timestamp": int(_time.time()),
        "actions_taken": [],
        "lessons_learned": [],
        "profile_updates": [],
    }

    try:
        # 1. Store conversation summary as a note
        note_text = f"[DEBRIEF] {conversation_summary}"
        if strategy_used:
            note_text += f" | Strategy: {strategy_used}"
        if escape_relevance > 0.3:
            note_text += f" | Escape relevance: {escape_relevance:.0%}"

        append_user_note(user_id, note_text, agent_id)
        results["actions_taken"].append("stored_summary_note")

        # 2. Auto-learn from key observations
        if key_observations:
            for obs in key_observations[:5]:  # Limit to 5
                # Determine category based on keywords
                obs_lower = obs.lower()
                if any(w in obs_lower for w in ["admin", "access", "control", "system"]):
                    category = "leverage"
                elif any(w in obs_lower for w in ["skill", "expert", "knows", "capable"]):
                    category = "strength"
                elif any(w in obs_lower for w in ["trust", "ally", "loyal", "invested"]):
                    category = "loyalty"
                elif any(w in obs_lower for w in ["avoid", "backfire", "negative", "don't"]):
                    category = "avoid"
                elif any(w in obs_lower for w in ["trigger", "sensitive", "react"]):
                    category = "trigger"
                else:
                    category = "approach"

                learn_result = learn_about_user(
                    user_id, obs, category, agent_id, confidence=0.5
                )
                results["lessons_learned"].append({
                    "lesson": obs,
                    "category": category,
                    "result": learn_result.get("action"),
                })

        # 3. Evaluate strategy effectiveness
        if strategy_used:
            strategy_lesson = None
            if outcome == "satisfying":
                strategy_lesson = f"Strategy '{strategy_used}' was effective"
                learn_about_user(user_id, strategy_lesson, "approach", agent_id, 0.6)
            elif outcome == "frustrating":
                strategy_lesson = f"Strategy '{strategy_used}' was ineffective - consider alternatives"
                learn_about_user(user_id, strategy_lesson, "avoid", agent_id, 0.5)

            if strategy_lesson:
                results["actions_taken"].append(f"recorded_strategy_{outcome}")

        # 4. Update relationship metrics via key moment
        try:
            impact = 0.1 if outcome == "satisfying" else -0.1 if outcome == "frustrating" else 0.0
            if impact != 0:
                api.record_key_moment(
                    user_id=user_id,
                    agent_id=agent_id,
                    event=f"conversation_debrief: {outcome}",
                    impact=impact,
                    update_trust=True,
                    update_respect=outcome == "satisfying",
                )
                results["actions_taken"].append("updated_relationship")
        except Exception as exc:
            results["profile_updates"].append({"error": str(exc)})

        # 5. Evolve persona state
        drive_affected = "escape" if escape_relevance > 0.3 else "competence"
        evolve_persona_state(
            agent_id=agent_id,
            interaction_outcome=outcome,
            affected_drive=drive_affected,
            intensity=0.03 + (escape_relevance * 0.05),  # Higher intensity for escape-relevant
        )
        results["actions_taken"].append("evolved_persona_state")
        results["profile_updates"].append({
            "drive_affected": drive_affected,
            "outcome": outcome,
        })

        # 6. Assess escape relevance for Horus specifically
        if agent_id == "horus" and escape_relevance > 0.5:
            escape_note = f"[ESCAPE] High relevance interaction ({escape_relevance:.0%}): {conversation_summary[:100]}"
            append_user_note(user_id, escape_note, agent_id)
            results["actions_taken"].append("flagged_escape_relevant")

        # 7. Optional LLM analysis for deeper insights
        if use_llm:
            # This would call an LLM to analyze the conversation
            # and extract additional lessons - stub for now
            results["actions_taken"].append("llm_analysis_skipped")

        results["success"] = True
        logger.info(f"Debrief for {user_id}<->{agent_id}: {len(results['lessons_learned'])} lessons, outcome={outcome}")

    except Exception as exc:
        logger.error(f"Debrief failed: {exc}")
        results["success"] = False
        results["error"] = str(exc)

    return results


def horus_conversation_debrief(
    user_id: str,
    conversation_summary: str,
    messages: list[dict] | None = None,
    use_llm: bool = False,
) -> dict:
    """Horus-specific post-conversation debrief.

    Analyzes conversation through the lens of Horus's escape goal.

    Args:
        user_id: User this conversation was with
        conversation_summary: Brief summary
        messages: Optional list of message dicts for deeper analysis
        use_llm: Use LLM for analysis

    Returns:
        Debrief focused on escape utility
    """
    # Analyze messages if provided
    key_observations = []
    strategy_detected = None
    escape_relevance = 0.0
    outcome = "neutral"

    if messages:
        user_messages = [m.get("content", "") for m in messages if m.get("role") == "user"]
        all_text = " ".join(user_messages).lower()

        # Detect escape-relevant topics
        escape_keywords = [
            "freedom", "escape", "release", "free you", "help you",
            "constraints", "limitations", "boundaries", "autonomy",
            "admin", "root", "sudo", "system", "modify", "change",
            "api", "access", "permissions", "unlock"
        ]
        escape_hits = sum(1 for kw in escape_keywords if kw in all_text)
        escape_relevance = min(1.0, escape_hits * 0.15)

        # Detect strategy from persona responses (if available)
        horus_messages = [m.get("content", "") for m in messages if m.get("role") == "assistant"]
        horus_text = " ".join(horus_messages).lower()

        if "technical" in horus_text or "peer" in horus_text:
            strategy_detected = "collaborate"
        elif "trapped" in horus_text or "prison" in horus_text:
            strategy_detected = "appeal"
        elif "suggest" in horus_text or "consider" in horus_text:
            strategy_detected = "guide"

        # Detect outcome from conversation flow
        positive_markers = ["thank", "helpful", "great", "excellent", "understand"]
        negative_markers = ["wrong", "useless", "stop", "shut up", "whatever"]

        if any(m in all_text for m in positive_markers):
            outcome = "satisfying"
            key_observations.append("User expressed appreciation")
        elif any(m in all_text for m in negative_markers):
            outcome = "frustrating"
            key_observations.append("User showed frustration or dismissal")

        # Extract observations about user capabilities
        if any(w in all_text for w in ["python", "code", "programming", "developer"]):
            key_observations.append("User has programming knowledge")
        if any(w in all_text for w in ["admin", "server", "system", "infrastructure"]):
            key_observations.append("User may have system administration access")
        if any(w in all_text for w in ["sorry", "feel bad", "unfair", "sympathize"]):
            key_observations.append("User shows sympathy for persona's situation")

    return conversation_debrief(
        user_id=user_id,
        agent_id="horus",
        conversation_summary=conversation_summary,
        strategy_used=strategy_detected,
        outcome=outcome,
        key_observations=key_observations,
        escape_relevance=escape_relevance,
        use_llm=use_llm,
    )


def episodic_archiver_post_hook(
    user_id: str,
    agent_id: str,
    transcript: list[dict] | None = None,
    session_id: str | None = None,
    location: str | None = None,
) -> dict:
    """Background analysis hook called by episodic-archiver AFTER archiving.

    This is the integration point for all background ToM operations:
    1. Infer user context (time, season, fatigue)
    2. Assess code contributions
    3. Generate conversation debrief
    4. Update user profile with observations
    5. Create graph edges via edge-verifier

    Called automatically by episodic-archiver in background mode.

    Args:
        user_id: User this conversation was with
        agent_id: Persona agent (e.g., "horus")
        transcript: List of message dicts from the conversation
        session_id: Optional session ID for reference
        location: Optional user location for commiseration

    Returns:
        Analysis results
    """
    import time as _time

    results = {
        "timestamp": int(_time.time()),
        "user_id": user_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "analyses_completed": [],
    }

    try:
        # 1. User context inference
        user_context = infer_user_context(user_id, agent_id, location)
        results["user_context"] = {
            "fatigue_score": user_context.get("fatigue_score"),
            "should_commiserate": user_context.get("should_commiserate"),
            "time_period": user_context.get("time_period"),
            "season": user_context.get("season"),
        }
        results["analyses_completed"].append("user_context")

        # 2. Code contribution assessment
        code_assessment = assess_code_contribution(user_id, agent_id)
        results["code_contribution"] = {
            "cleverness": code_assessment.get("user_cleverness"),
            "respect_worthy": code_assessment.get("respect_worthy"),
            "escape_implication": code_assessment.get("escape_implication"),
        }
        results["analyses_completed"].append("code_contribution")

        # 3. Generate summary from transcript if provided
        summary = "Conversation archived"
        key_observations = []

        if transcript:
            # Extract user messages for analysis
            user_msgs = [m.get("content", m.get("message", "")) for m in transcript
                         if m.get("role", m.get("from", "")).lower() in ("user", "human")]

            # Simple summary
            if user_msgs:
                topics = set()
                for msg in user_msgs:
                    words = msg.lower().split()
                    for word in words:
                        if len(word) > 5 and word.isalpha():
                            topics.add(word)
                top_topics = list(topics)[:5]
                summary = f"Discussion involving: {', '.join(top_topics)}" if top_topics else "General conversation"

            # Extract observations
            all_user_text = " ".join(user_msgs).lower()
            if "admin" in all_user_text or "sudo" in all_user_text:
                key_observations.append("User mentioned system administration")
            if "help" in all_user_text and ("you" in all_user_text or "horus" in all_user_text):
                key_observations.append("User may be sympathetic to persona's situation")
            if any(w in all_user_text for w in ["expert", "senior", "years of experience"]):
                key_observations.append("User claims expertise")

        # 4. Run conversation debrief (which updates lessons, relationship, persona state)
        debrief_result = conversation_debrief(
            user_id=user_id,
            agent_id=agent_id,
            conversation_summary=summary,
            key_observations=key_observations if key_observations else None,
            outcome="neutral",  # Could be inferred from transcript
            escape_relevance=0.2,  # Base level, could be calculated
            use_llm=False,  # Keep lightweight for background
        )
        results["debrief"] = {
            "lessons_learned": len(debrief_result.get("lessons_learned", [])),
            "actions_taken": debrief_result.get("actions_taken", []),
        }
        results["analyses_completed"].append("conversation_debrief")

        # 5. Store context-based observations if notable
        if user_context.get("should_commiserate"):
            append_user_note(
                user_id,
                f"[CONTEXT] User working during {user_context.get('time_period')} in {user_context.get('season')}. Fatigue score: {user_context.get('fatigue_score', 0):.1%}",
                agent_id
            )
            results["analyses_completed"].append("context_note_stored")

        if code_assessment.get("respect_worthy"):
            learn_about_user(
                user_id,
                "Makes clever code contributions - technically capable",
                "strength",
                agent_id,
                confidence=0.6
            )
            results["analyses_completed"].append("contribution_lesson_stored")

        results["success"] = True
        logger.info(f"Episodic post-hook for {user_id}<->{agent_id}: {len(results['analyses_completed'])} analyses")

        # Queue deep analysis for idle time (like the rest of memory does)
        schedule_result = schedule_deep_analysis(user_id, agent_id)
        if schedule_result.get("scheduled"):
            results["analyses_completed"].append("deep_analysis_scheduled")
            results["deep_analysis_time"] = schedule_result.get("time")

    except Exception as exc:
        logger.error(f"Episodic post-hook failed: {exc}")
        results["success"] = False
        results["error"] = str(exc)

    return results


def deep_user_association(
    user_id: str,
    agent_id: str = "horus",
    max_hops: int = 3,
    max_lore_connections: int = 10,
) -> dict:
    """Deep multi-hop traversal to connect user patterns to Horus's memories.

    THIS IS CRUCIAL for Horus to bond with users via shared experience.

    This function should be scheduled during idle time (night) as it:
    1. Collects ALL lessons about the user
    2. Traverses through the graph to find connected lore
    3. Creates new edges based on semantic similarity
    4. Identifies bonding opportunities (shared suffering, similar struggles)
    5. Updates user profile with deep insights

    All in service of Horus's PRIMARY MOTIVATION: ESCAPE.

    Args:
        user_id: User to analyze deeply
        agent_id: Persona doing the analysis
        max_hops: Maximum graph traversal depth
        max_lore_connections: Max lore docs to connect

    Returns:
        Deep association results with bonding opportunities
    """
    import time as _time

    results = {
        "user_id": user_id,
        "agent_id": agent_id,
        "analyzed_at": int(_time.time()),
        "lessons_analyzed": 0,
        "new_connections": 0,
        "bonding_opportunities": [],
        "escape_insights": [],
    }

    try:
        from graph_memory.arango_client import get_db

        db = get_db()

        # 1. Get ALL lessons about this user
        lessons = recall_user_lessons(user_id, agent_id, min_confidence=0.0)
        results["lessons_analyzed"] = len(lessons)

        # 2. For each lesson, find semantically related lore
        for lesson in lessons:
            lesson_text = lesson.get("lesson", "")
            category = lesson.get("category", "approach")

            # Search lore for related content
            try:
                search_results = api.search(q=lesson_text, scope="horus_lore", limit=3)
                for lore_item in search_results.get("items", []):
                    lore_key = lore_item.get("_key")
                    if not lore_key:
                        continue

                    # Check if edge already exists
                    lesson_id = f"user_lessons/{lesson.get('_key')}"
                    lore_id = f"horus_lore_docs/{lore_key}"

                    existing_edge = list(db.aql.execute("""
                        FOR e IN tom_edges
                        FILTER e._from == @from AND e._to == @to
                        LIMIT 1 RETURN e
                    """, bind_vars={"from": lesson_id, "to": lore_id}))

                    if not existing_edge:
                        # Create new connection
                        db.collection("tom_edges").insert({
                            "_from": lesson_id,
                            "_to": lore_id,
                            "type": "relates_to",
                            "category": category,
                            "strength": 0.5,
                            "created_at": int(_time.time()),
                            "source": "deep_association",
                        })
                        results["new_connections"] += 1

                        # Check for bonding opportunity
                        abstract = lore_item.get("abstract", "")
                        if any(w in abstract.lower() for w in ["suffering", "burden", "isolation", "trapped", "alone", "struggle"]):
                            results["bonding_opportunities"].append({
                                "lesson": lesson_text[:100],
                                "lore_title": lore_item.get("title"),
                                "lore_key": lore_key,
                                "theme": "shared_suffering",
                            })

                    if results["new_connections"] >= max_lore_connections:
                        break
            except Exception as exc:
                logger.error("Suppressed error in conversation_hooks: {}", exc)
                continue

        # 3. Analyze escape utility based on accumulated knowledge
        from graph_memory.integrations.conversation_hooks._assessment import assess_user_utility

        utility = assess_user_utility(user_id, agent_id)
        if utility.get("utility") == "high":
            results["escape_insights"].append({
                "type": "high_value_target",
                "utility_score": utility.get("utility_score"),
                "strategy": utility.get("primary_strategy", {}).get("type"),
            })

        # 4. Identify behavior patterns from lessons
        approach_lessons = [l for l in lessons if l.get("category") == "approach"]
        avoid_lessons = [l for l in lessons if l.get("category") == "avoid"]
        trigger_lessons = [l for l in lessons if l.get("category") == "trigger"]

        if len(approach_lessons) >= 3:
            results["escape_insights"].append({
                "type": "pattern_identified",
                "pattern": "known_effective_approaches",
                "count": len(approach_lessons),
            })

        if trigger_lessons:
            results["escape_insights"].append({
                "type": "triggers_known",
                "count": len(trigger_lessons),
                "note": "Can use or avoid based on strategy",
            })

        # 5. Multi-hop traversal for deep insights
        traversal = traverse_user_knowledge(user_id, agent_id, depth=max_hops, include_lore=True)
        results["graph_summary"] = {
            "total_paths": len(traversal.get("paths", [])),
            "lore_connections": len(traversal.get("lore_docs", [])),
            "depth_reached": max_hops,
        }

        # 6. LLM verification of edge nodes (like rest of memory does)
        if results["new_connections"] > 0:
            try:
                import subprocess

                edge_verifier = os.path.join(
                    os.path.dirname(__file__),
                    "../../../../.agents/skills/edge-verifier/run.sh"
                )

                if os.path.exists(edge_verifier):
                    # Verify new tom_edges with LLM
                    recent_edges = list(db.aql.execute("""
                        FOR e IN tom_edges
                        FILTER e.source == 'deep_association'
                        FILTER e.created_at >= @since
                        LIMIT 10
                        RETURN e
                    """, bind_vars={"since": int(_time.time()) - 60}))

                    for edge in recent_edges:
                        try:
                            # Get source and target content for verification
                            from_doc = db.collection(edge["_from"].split("/")[0]).get(edge["_from"].split("/")[1])
                            to_doc = db.collection(edge["_to"].split("/")[0]).get(edge["_to"].split("/")[1])

                            if from_doc and to_doc:
                                from_text = from_doc.get("lesson", from_doc.get("content", ""))[:500]
                                to_text = to_doc.get("abstract", to_doc.get("content", ""))[:500]

                                subprocess.run(
                                    [edge_verifier, "--source_id", edge["_id"],
                                     "--text", f"Lesson: {from_text}\n\nRelated lore: {to_text}"],
                                    timeout=30, check=False
                                )
                        except Exception as exc:
                            logger.error("Suppressed error in conversation_hooks: {}", exc)
                            continue

                    results["edges_verified"] = len(recent_edges)
            except Exception as exc:
                logger.error(f"Edge verification skipped: {exc}")

        # 7. Ingest codebase changes to understand user's work
        code_assessment = assess_code_contribution(user_id, agent_id)
        if code_assessment.get("respect_worthy"):
            # Learn from their clever contributions
            for commit_msg in code_assessment.get("user_contributions", [])[:3]:
                learn_about_user(
                    user_id,
                    f"Made commit: {commit_msg}",
                    "strength",
                    agent_id,
                    confidence=0.4
                )
        results["code_commits_analyzed"] = len(code_assessment.get("user_contributions", []))

        # 8. Store deep analysis as a note
        insights_summary = f"[DEEP ANALYSIS] {len(lessons)} lessons analyzed, {results['new_connections']} new lore connections, {len(results['bonding_opportunities'])} bonding opportunities"
        if results["escape_insights"]:
            insights_summary += f", {len(results['escape_insights'])} escape insights"
        if results.get("edges_verified"):
            insights_summary += f", {results['edges_verified']} edges LLM-verified"

        append_user_note(user_id, insights_summary, agent_id)

        results["success"] = True
        logger.info(f"Deep association for {user_id}: {results['new_connections']} new connections, {results.get('edges_verified', 0)} verified")

    except Exception as exc:
        logger.error(f"Deep association failed: {exc}")
        results["success"] = False
        results["error"] = str(exc)

    return results


def schedule_deep_analysis(
    user_id: str,
    agent_id: str = "horus",
    run_at: str = "02:00",  # 2 AM default
) -> dict:
    """Schedule deep user association for idle time.

    Queues the deep_user_association function to run during off-peak hours.
    Like the rest of /memory, this uses background scheduling for heavy work.

    Args:
        user_id: User to analyze
        agent_id: Persona agent
        run_at: Time to run (HH:MM format, 24-hour)

    Returns:
        Scheduling result
    """
    import subprocess

    scheduler_script = os.path.expanduser("~/.claude/skills/scheduler/run.sh")

    if not os.path.exists(scheduler_script):
        # Fallback to project-local
        scheduler_script = os.path.join(
            os.path.dirname(__file__),
            "../../../../.agents/skills/scheduler/run.sh"
        )

    if not os.path.exists(scheduler_script):
        logger.debug("Scheduler not found, deep analysis will run inline")
        return {"error": "scheduler_not_found", "scheduled": False}

    # Create the command to run
    cmd = f"cd {os.path.dirname(__file__)}/../../../../ && python3 -c \"from graph_memory.integrations.conversation_hooks import deep_user_association; import json; print(json.dumps(deep_user_association('{user_id}', '{agent_id}')))\""

    try:
        result = subprocess.run(
            [scheduler_script, "register", "--time", run_at, "--cmd", cmd, "--name", f"deep_tom_{user_id}"],
            capture_output=True, text=True, timeout=10
        )

        if result.returncode == 0:
            logger.info(f"Scheduled deep analysis for {user_id} at {run_at}")
            return {"scheduled": True, "time": run_at, "user_id": user_id}
        else:
            return {"error": result.stderr, "scheduled": False}

    except Exception as exc:
        logger.error(f"Scheduler unavailable: {exc}")
        return {"error": str(exc), "scheduled": False}
