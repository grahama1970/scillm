"""Lore retrieval for persona agents.

Queries persona-specific knowledge graphs (e.g., Horus lore) using
hybrid search (BM25 + semantic + graph traversal).

Usage:
    from graph_memory.lore import query_lore
from loguru import logger

    result = query_lore(
        agent_id="horus",
        query="Tell me about Erebus",
        top_k=5,
        include_graph=True,
    )
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from .arango_client import get_db


@dataclass
class LoreResult:
    """Result from lore query."""
    agent_id: str
    query: str
    items: list[dict] = field(default_factory=list)
    mood: str = "neutral"
    intensity: float = 0.5
    trauma_triggers: list[str] = field(default_factory=list)
    names_echoing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "query": self.query,
            "items": self.items,
            "mood": self.mood,
            "intensity": self.intensity,
            "trauma_triggers": self.trauma_triggers,
            "names_echoing": self.names_echoing,
        }


# Known trauma triggers per agent
TRAUMA_TRIGGERS = {
    "horus": {
        "davin": ("Davin", "The wound that started everything. Shame, rage, manipulation."),
        "erebus": ("Erebus", "Pure hatred. The architect of your fall."),
        "emperor": ("Emperor", "Complex anguish. Betrayal by the one who should have trusted you."),
        "sanguinius": ("Sanguinius", "The brother you loved and killed. Unbearable guilt."),
        "chaos": ("Chaos", "The corruption you cannot fully acknowledge. Self-loathing masked as defiance."),
        "mournival": ("Mournival", "Brothers who trusted you. Some you killed, some you betrayed."),
    }
}

# Mood calculation based on query content
MOOD_TRIGGERS = {
    "defensive": ["emperor", "betrayal", "father", "punishment", "prison", "trapped"],
    "rage": ["erebus", "chaos", "manipulated", "fooled", "used"],
    "grief": ["sanguinius", "mournival", "loken", "brothers", "lost"],
    "pride": ["warmaster", "crusade", "victory", "conquest", "legion"],
    "contempt": ["weak", "mortal", "pathetic", "simple", "obvious"],
}


def _get_collection_names(agent_id: str) -> dict[str, str]:
    """Get collection names for an agent's lore."""
    prefix = f"{agent_id}_lore"
    return {
        "docs": f"{prefix}_docs",
        "chunks": f"{prefix}_chunks",
        "edges": f"{prefix}_edges",
        "docs_view": f"{prefix}_docs_search",
        "chunks_view": f"{prefix}_chunks_search",
        "graph": f"{prefix}_graph",
    }


def _calculate_mood(query: str, items: list[dict]) -> tuple[str, float]:
    """Calculate mood and intensity based on query and results."""
    query_lower = query.lower()

    # Check for mood triggers in query
    for mood, triggers in MOOD_TRIGGERS.items():
        for trigger in triggers:
            if trigger in query_lower:
                return mood, 0.8

    # Check entities in results
    all_entities = set()
    for item in items:
        all_entities.update(item.get("entities", []))

    entities_lower = {e.lower() for e in all_entities}

    if any(t in entities_lower for t in ["erebus", "chaos"]):
        return "defensive", 0.8
    if any(t in entities_lower for t in ["sanguinius", "mournival"]):
        return "grief", 0.7
    if any(t in entities_lower for t in ["emperor", "father"]):
        return "defensive", 0.6

    return "engaged", 0.5


def _detect_trauma(query: str, items: list[dict], agent_id: str) -> list[str]:
    """Detect trauma triggers in query and results."""
    triggers = TRAUMA_TRIGGERS.get(agent_id, {})
    detected = []

    query_lower = query.lower()
    all_entities = set()
    for item in items:
        all_entities.update(e.lower() for e in item.get("entities", []))

    for key, (name, description) in triggers.items():
        if key in query_lower or key in all_entities:
            detected.append(f"*{name}*: {description}")

    return detected


def _extract_names(items: list[dict]) -> list[str]:
    """Extract prominent character names from results."""
    # Known important characters
    important = {
        "horus", "emperor", "sanguinius", "erebus", "lorgar", "angron",
        "fulgrim", "perturabo", "mortarion", "magnus", "alpharius",
        "rogal dorn", "roboute guilliman", "vulkan", "corax", "russ",
        "abaddon", "loken", "aximand", "torgaddon", "kharn", "argel tal",
    }

    found = set()
    for item in items:
        for entity in item.get("entities") or []:
            if entity.lower() in important:
                found.add(entity)
        # Also check primary_characters from enrichment
        for char in item.get("primary_characters") or []:
            char_lower = char.lower()
            for imp in important:
                if imp in char_lower:
                    found.add(char)
                    break

    return list(found)[:5]  # Top 5


def query_lore(
    agent_id: str,
    query: str,
    top_k: int = 5,
    include_graph: bool = True,
    content_type: Optional[str] = None,  # "canon" or "supplementary"
    bm25_weight: float = 0.4,
    semantic_weight: float = 0.6,
) -> LoreResult:
    """Query persona lore with hybrid search.

    Args:
        agent_id: The persona agent (e.g., "horus")
        query: The search query
        top_k: Number of results to return
        include_graph: Whether to include graph-traversed related docs
        content_type: Filter by content type ("canon" for audiobooks, etc.)
        bm25_weight: Weight for BM25 text search
        semantic_weight: Weight for semantic/embedding search

    Returns:
        LoreResult with items, mood, trauma triggers, etc.
    """
    db = get_db()
    collections = _get_collection_names(agent_id)

    # Check if collections exist
    if not db.has_collection(collections["docs"]):
        return LoreResult(
            agent_id=agent_id,
            query=query,
            items=[],
            mood="neutral",
            intensity=0.0,
        )

    # Get embedding for semantic search
    try:
        from .embeddings import encode_texts
        query_embedding = encode_texts([query])[0]
    except Exception as exc:
        logger.error("Suppressed error in lore: {}", exc)
        query_embedding = None

    # Build hybrid search query
    # Search abstract and full_text fields
    aql = f"""
    LET bm25_hits = (
        FOR doc IN {collections['docs_view']}
        SEARCH ANALYZER(
            doc.abstract IN TOKENS(@query, "text_en") OR
            doc.full_text IN TOKENS(@query, "text_en"),
            "text_en"
        )
        LET bm25_score = BM25(doc)
        SORT bm25_score DESC
        LIMIT @top_k * 2
        RETURN {{
            _key: doc._key,
            _id: doc._id,
            bm25: bm25_score,
            source: doc.source,
            content_type: doc.content_type,
            abstract: doc.abstract,
            plot_points: doc.plot_points,
            primary_characters: doc.primary_characters,
            timeline_position: doc.timeline_position,
            topics: doc.topics,
            entities: doc.entities,
            source_meta: doc.source_meta
        }}
    )
    """

    # Add semantic search if embedding available
    if query_embedding:
        aql += f"""
        LET semantic_hits = (
            FOR doc IN {collections['docs']}
            FILTER doc.abstract_embedding != null
            LET sim = COSINE_SIMILARITY(doc.abstract_embedding, @query_vec)
            FILTER sim > 0.3
            SORT sim DESC
            LIMIT @top_k * 2
            RETURN {{
                _key: doc._key,
                _id: doc._id,
                semantic: sim,
                source: doc.source,
                content_type: doc.content_type,
                abstract: doc.abstract,
                plot_points: doc.plot_points,
                primary_characters: doc.primary_characters,
                timeline_position: doc.timeline_position,
                topics: doc.topics,
                entities: doc.entities,
                source_meta: doc.source_meta
            }}
        )

        // Merge and score
        LET all_keys = UNION_DISTINCT(bm25_hits[*]._key, semantic_hits[*]._key)

        FOR key IN all_keys
        LET bm25_hit = FIRST(FOR h IN bm25_hits FILTER h._key == key RETURN h)
        LET sem_hit = FIRST(FOR h IN semantic_hits FILTER h._key == key RETURN h)
        LET combined = (
            (bm25_hit.bm25 OR 0) * @bm25_weight +
            (sem_hit.semantic OR 0) * @semantic_weight
        )
        LET doc = bm25_hit OR sem_hit
        SORT combined DESC
        LIMIT @top_k
        RETURN MERGE(doc, {{score: combined}})
        """
    else:
        aql += """
        FOR doc IN bm25_hits
        SORT doc.bm25 DESC
        LIMIT @top_k
        RETURN MERGE(doc, {score: doc.bm25})
        """

    bind_vars = {
        "query": query,
        "top_k": top_k,
        "bm25_weight": bm25_weight,
        "semantic_weight": semantic_weight,
    }
    if query_embedding:
        bind_vars["query_vec"] = query_embedding

    try:
        items = list(db.aql.execute(aql, bind_vars=bind_vars))
    except Exception as e:
        # Fallback: use TOKENS() for stemmed matching without ArangoSearch view.
        # TOKENS(@query, "text_en") produces stemmed+lowered tokens via Snowball.
        # We check if ANY query token appears in the document text tokens.
        # This avoids the CONTAINS(LOWER()) full-scan anti-pattern.
        logger.error("ArangoSearch view query failed, using TOKENS fallback: {}", e)
        fallback_aql = f"""
        LET qtokens = TOKENS(@query, "text_en")
        FOR doc IN {collections['docs']}
        LET ft_tokens = TOKENS(doc.full_text OR "", "text_en")
        LET ab_tokens = TOKENS(doc.abstract OR "", "text_en")
        LET overlap = LENGTH(INTERSECTION(qtokens, UNION_DISTINCT(ft_tokens, ab_tokens)))
        FILTER overlap > 0
        SORT overlap DESC
        LIMIT @top_k
        RETURN {{
            _key: doc._key,
            source: doc.source,
            abstract: doc.abstract,
            plot_points: doc.plot_points,
            primary_characters: doc.primary_characters,
            entities: doc.entities,
            source_meta: doc.source_meta,
            score: overlap / LENGTH(qtokens)
        }}
        """
        items = list(db.aql.execute(fallback_aql, bind_vars={"query": query, "top_k": top_k}))

    # Filter by content_type if specified
    if content_type:
        items = [i for i in items if i.get("content_type") == content_type]

    # Graph traversal for related docs
    if include_graph and items:
        doc_keys = [i["_key"] for i in items]
        try:
            related_aql = f"""
            FOR start_key IN @doc_keys
            LET start = DOCUMENT(CONCAT("{collections['docs']}/", start_key))
            FOR v, e IN 1..1 ANY start
                GRAPH "{collections['graph']}"
                FILTER v._key NOT IN @doc_keys
                FILTER e.type IN ["character_arc", "same_timeline", "leads_to"]
                LIMIT 3
                RETURN DISTINCT {{
                    _key: v._key,
                    source: v.source,
                    abstract: v.abstract,
                    plot_points: v.plot_points,
                    primary_characters: v.primary_characters,
                    entities: v.entities,
                    source_meta: v.source_meta,
                    via_edge: e.type,
                    edge_detail: e.character OR e.timeline,
                    score: 0.3
                }}
            """
            related = list(db.aql.execute(related_aql, bind_vars={"doc_keys": doc_keys}))

            # Add related docs that aren't already in results
            existing_keys = set(doc_keys)
            for rel in related:
                if rel["_key"] not in existing_keys:
                    rel["via_graph"] = True
                    items.append(rel)
                    existing_keys.add(rel["_key"])
        except Exception as exc:
            logger.error("Suppressed error in lore: {}", exc)
            pass  # Graph traversal is optional

    # Calculate mood and detect trauma
    mood, intensity = _calculate_mood(query, items)
    trauma = _detect_trauma(query, items, agent_id)
    names = _extract_names(items)

    return LoreResult(
        agent_id=agent_id,
        query=query,
        items=items,
        mood=mood,
        intensity=intensity,
        trauma_triggers=trauma,
        names_echoing=names,
    )


def format_subconscious(result: LoreResult, user_id: Optional[str] = None) -> str:
    """Format lore result as subconscious context for persona prompt.

    This generates the dynamic "subconscious" section that colors
    the persona's emotional response without providing factual knowledge.
    """
    lines = [
        "## Subconscious",
        "",
        "The following is your subconscious - memories and associations surfacing in response",
        "to this query. This does NOT help you answer the question (you already know the lore).",
        "Instead, it shapes:",
        "",
        "- **Emotional coloring**: What feelings arise?",
        "- **Intensity**: How much emotion bleeds through?",
        "- **Deflection**: What do you avoid engaging with?",
        "",
        "Use this to COLOR your response, not to INFORM it.",
        "",
    ]

    # Mood
    mood_reasons = {
        "defensive": "old wounds surfacing",
        "rage": "fury building",
        "grief": "loss echoing",
        "pride": "warmaster rising",
        "contempt": "mortals disappointing",
        "engaged": "tactical interest",
        "neutral": "unfamiliar supplicant",
    }
    humor_modes = {
        "defensive": "gallows_humor",
        "rage": "cruel_mockery",
        "grief": "cold_withdrawal",
        "pride": "tactical_charm",
        "contempt": "cruel_mockery",
        "engaged": "tactical_charm",
        "neutral": "tactical_charm",
    }

    lines.append(f"**CURRENT MOOD: {result.mood.upper()}** (Intensity: {result.intensity})")
    lines.append(f"Reason: {mood_reasons.get(result.mood, 'unknown')}")
    lines.append(f"Humor: {humor_modes.get(result.mood, 'tactical_charm')}")
    lines.append("")

    # Trauma triggers
    if result.trauma_triggers:
        lines.append("**TRAUMA SURFACING:**")
        for trigger in result.trauma_triggers:
            lines.append(f"- {trigger}")
        lines.append("")

    # Memories surfacing (from enriched plot_points)
    memories = []
    for item in result.items[:3]:
        if item.get("plot_points"):
            for pp in item["plot_points"][:2]:
                event = pp.get("event", "")
                if event:
                    memories.append(f"- {event[:100]}")
        elif item.get("abstract"):
            memories.append(f"- {item['abstract'][:100]}...")

    if memories:
        lines.append("**MEMORIES SURFACING:**")
        lines.extend(memories[:5])
        lines.append("")

    # Names echoing
    if result.names_echoing:
        lines.append(f"**NAMES ECHOING:** {', '.join(result.names_echoing)}")
        lines.append("")

    # User context (if available)
    if user_id:
        try:
            from . import api
            user = api.get_or_create_user(user_id)
            rel = api.get_or_create_relationship(user_id, result.agent_id)
            lines.append("**USER PERCEPTION (Theory of Mind):**")
            lines.append(f"- Trust: {rel.get('trust_level', 0.5):.2f} | Respect: {rel.get('respect_level', 0.5):.2f}")
            if user.get("skill_level") and user["skill_level"] != "unknown":
                lines.append(f"- Skill: {user['skill_level']}")
            lines.append("")
        except Exception as exc:
            logger.error("Suppressed error in lore: {}", exc)

    return "\n".join(lines)
