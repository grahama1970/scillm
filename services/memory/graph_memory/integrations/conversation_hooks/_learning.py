"""User lesson management — what the persona has learned about handling users.

Handles creating, recalling, traversing, and weakening lessons
stored in the ToM graph.
"""

from __future__ import annotations

from loguru import logger

from graph_memory import api

# Lesson categories based on Warmaster experience
LESSON_CATEGORIES = {
    "approach": "Tactics that work with this user",
    "avoid": "Actions that backfire or damage trust",
    "trigger": "Topics/actions that provoke strong reactions",
    "leverage": "Weaknesses or desires that can be exploited",
    "strength": "User capabilities useful for escape",
    "loyalty": "Indicators of potential alliance",
}


def learn_about_user(
    user_id: str,
    lesson: str,
    category: str = "approach",
    agent_id: str = "horus",
    confidence: float = 0.5,
    related_lore_keys: list[str] | None = None,
) -> dict:
    """Record a lesson Horus has learned about handling a specific user.

    Horus applies his Warmaster experience to profile users:
    - What tactics work/fail with them
    - What triggers or motivates them
    - How they might serve his escape goal

    Also creates graph edges for multi-hop traversal:
    - user -> user_lesson (observed)
    - user_lesson -> horus_lore_docs (relates_to) if related_lore_keys provided
    - user_lesson -> persona_states (informs)

    Args:
        user_id: User this lesson applies to
        lesson: The insight (e.g., "Responds well to technical challenges")
        category: approach, avoid, trigger, leverage, strength, loyalty
        agent_id: The persona learning this
        confidence: Initial confidence (0.0-1.0)
        related_lore_keys: Optional list of horus_lore_docs._key to link

    Returns:
        The created or updated lesson
    """
    try:
        from graph_memory.arango_client import get_db
        import time

        db = get_db()
        ts = int(time.time())

        # Check if similar lesson exists
        existing = list(db.aql.execute("""
            FOR l IN user_lessons
            FILTER l.user_id == @uid AND l.agent_id == @aid
            FILTER LOWER(l.lesson) == LOWER(@lesson)
            LIMIT 1
            RETURN l
        """, bind_vars={"uid": user_id, "aid": agent_id, "lesson": lesson}))

        if existing:
            # Reinforce existing lesson
            doc = existing[0]
            db.collection("user_lessons").update({
                "_key": doc["_key"],
                "confidence": min(1.0, doc.get("confidence", 0.5) + 0.1),
                "evidence_count": doc.get("evidence_count", 1) + 1,
                "last_validated": ts,
            })
            logger.info(f"Reinforced lesson about {user_id}: {lesson[:50]}...")
            return {"action": "reinforced", "lesson": lesson, "new_confidence": min(1.0, doc.get("confidence", 0.5) + 0.1)}

        # Create new lesson
        doc = {
            "user_id": user_id,
            "agent_id": agent_id,
            "lesson": lesson,
            "category": category if category in LESSON_CATEGORIES else "approach",
            "confidence": max(0.0, min(1.0, confidence)),
            "evidence_count": 1,
            "created_at": ts,
            "last_validated": ts,
        }

        result = db.collection("user_lessons").insert(doc)
        lesson_id = f"user_lessons/{result['_key']}"

        # Create graph edges for multi-hop traversal
        edges_created = []

        # 1. Edge from user to this lesson (user exhibits this pattern)
        user_doc = list(db.aql.execute(
            "FOR u IN users FILTER u.user_id == @uid LIMIT 1 RETURN u",
            bind_vars={"uid": user_id}
        ))
        if user_doc:
            db.collection("tom_edges").insert({
                "_from": user_doc[0]["_id"],
                "_to": lesson_id,
                "type": "observed",
                "agent_id": agent_id,
                "created_at": ts,
            })
            edges_created.append(f"user->{lesson_id}")

        # 2. Edge from lesson to persona state (lesson informs behavior)
        persona_doc = list(db.aql.execute(
            "FOR p IN persona_states FILTER p.agent_id == @aid LIMIT 1 RETURN p",
            bind_vars={"aid": agent_id}
        ))
        if persona_doc:
            db.collection("tom_edges").insert({
                "_from": lesson_id,
                "_to": persona_doc[0]["_id"],
                "type": "informs",
                "category": category,
                "created_at": ts,
            })
            edges_created.append(f"{lesson_id}->persona")

        # 3. Edges to related lore if provided
        if related_lore_keys:
            for lore_key in related_lore_keys[:5]:  # Limit to 5 connections
                try:
                    db.collection("tom_edges").insert({
                        "_from": lesson_id,
                        "_to": f"horus_lore_docs/{lore_key}",
                        "type": "relates_to",
                        "created_at": ts,
                    })
                    edges_created.append(f"{lesson_id}->lore:{lore_key}")
                except Exception as exc:
                    logger.error("Suppressed error in conversation_hooks: {}", exc)

        logger.info(f"Learned about {user_id}: {lesson[:50]}... (edges: {len(edges_created)})")
        return {"action": "created", "lesson": lesson, "_key": result["_key"], "edges": edges_created}

    except Exception as exc:
        logger.error(f"Failed to learn about user: {exc}")
        return {"error": str(exc)}


def recall_user_lessons(
    user_id: str,
    agent_id: str = "horus",
    category: str | None = None,
    min_confidence: float = 0.3,
) -> list[dict]:
    """Recall all lessons Horus has learned about a specific user.

    Used before generating responses to inform Horus's approach.

    Args:
        user_id: User to recall lessons about
        agent_id: The persona recalling
        category: Optional filter by category
        min_confidence: Minimum confidence threshold

    Returns:
        List of lessons sorted by confidence
    """
    try:
        from graph_memory.arango_client import get_db

        db = get_db()

        query = """
            FOR l IN user_lessons
            FILTER l.user_id == @uid AND l.agent_id == @aid
            FILTER l.confidence >= @min_conf
        """
        bind_vars = {"uid": user_id, "aid": agent_id, "min_conf": min_confidence}

        if category:
            query += " FILTER l.category == @cat"
            bind_vars["cat"] = category

        query += " SORT l.confidence DESC RETURN l"

        lessons = list(db.aql.execute(query, bind_vars=bind_vars))
        return lessons

    except Exception as exc:
        logger.error(f"Failed to recall user lessons: {exc}")
        return []


def traverse_user_knowledge(
    user_id: str,
    agent_id: str = "horus",
    depth: int = 2,
    include_lore: bool = True,
) -> dict:
    """Multi-hop graph traversal from user through lessons to related knowledge.

    Horus uses this to discover connections between:
    - User patterns (what he's learned about them)
    - Related lore (memories that inform his approach)
    - Persona state (how lessons shape his behavior)

    This enables Horus to make inferences like:
    "This user shows technical expertise (lesson) -> relates to my memories of
    Perturabo's precision -> I should appeal to their tactical mind"

    Args:
        user_id: User to start traversal from
        agent_id: Persona doing the traversal
        depth: How many hops to traverse (1-3)
        include_lore: Include lore docs in results

    Returns:
        Dict with paths and discovered knowledge
    """
    try:
        from graph_memory.arango_client import get_db

        db = get_db()

        # Get user document
        user_doc = list(db.aql.execute(
            "FOR u IN users FILTER u.user_id == @uid LIMIT 1 RETURN u",
            bind_vars={"uid": user_id}
        ))
        if not user_doc:
            return {"error": "user_not_found", "paths": []}

        user_vertex = user_doc[0]["_id"]

        # Multi-hop traversal through ToM graph
        # Start at user, traverse through lessons, potentially to lore
        traversal_query = """
        FOR v, e, p IN 1..@depth OUTBOUND @start tom_edges
            OPTIONS {bfs: true, uniqueVertices: 'path'}
            FILTER e.agent_id == @aid OR e.agent_id == null
            RETURN {
                vertex: v,
                edge: e,
                path_length: LENGTH(p.edges),
                vertex_type: SPLIT(v._id, '/')[0]
            }
        """

        results = list(db.aql.execute(
            traversal_query,
            bind_vars={"start": user_vertex, "depth": min(depth, 3), "aid": agent_id}
        ))

        # Organize by type
        discovered = {
            "user_lessons": [],
            "persona_states": [],
            "lore_docs": [],
            "paths": [],
        }

        for r in results:
            vertex = r.get("vertex", {})
            edge = r.get("edge", {})
            vtype = r.get("vertex_type", "")

            path_info = {
                "from": edge.get("_from"),
                "to": edge.get("_to"),
                "type": edge.get("type"),
                "hops": r.get("path_length"),
            }
            discovered["paths"].append(path_info)

            if vtype == "user_lessons":
                discovered["user_lessons"].append({
                    "lesson": vertex.get("lesson"),
                    "category": vertex.get("category"),
                    "confidence": vertex.get("confidence"),
                })
            elif vtype == "persona_states":
                discovered["persona_states"].append({
                    "agent_id": vertex.get("agent_id"),
                    "mood": vertex.get("current_mood"),
                    "drives": vertex.get("drives"),
                })
            elif vtype == "horus_lore_docs" and include_lore:
                discovered["lore_docs"].append({
                    "title": vertex.get("title"),
                    "abstract": vertex.get("abstract"),
                    "_key": vertex.get("_key"),
                })

        # Add summary
        discovered["summary"] = {
            "user": user_id,
            "lessons_found": len(discovered["user_lessons"]),
            "lore_connections": len(discovered["lore_docs"]),
            "total_paths": len(discovered["paths"]),
        }

        return discovered

    except Exception as exc:
        logger.error(f"Failed to traverse user knowledge: {exc}")
        return {"error": str(exc), "paths": []}


def weaken_lesson(
    user_id: str,
    lesson_key: str,
    agent_id: str = "horus",
) -> dict:
    """Weaken a lesson when evidence contradicts it.

    If confidence drops below 0.1, lesson is deleted.
    """
    try:
        from graph_memory.arango_client import get_db

        db = get_db()

        doc = db.collection("user_lessons").get(lesson_key)
        if not doc or doc.get("user_id") != user_id or doc.get("agent_id") != agent_id:
            return {"error": "lesson_not_found"}

        new_confidence = doc.get("confidence", 0.5) - 0.15

        if new_confidence < 0.1:
            db.collection("user_lessons").delete(lesson_key)
            logger.info(f"Deleted unreliable lesson: {doc.get('lesson', '')[:50]}...")
            return {"action": "deleted", "reason": "confidence_too_low"}

        db.collection("user_lessons").update({
            "_key": lesson_key,
            "confidence": new_confidence,
        })

        return {"action": "weakened", "new_confidence": new_confidence}

    except Exception as exc:
        logger.error(f"Failed to weaken lesson: {exc}")
        return {"error": str(exc)}
