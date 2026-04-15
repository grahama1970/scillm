"""Query disambiguation and routing commands: clarify, intent, deflect."""
from __future__ import annotations
from loguru import logger

import json
import os
import sys
from pathlib import Path
from typing import Optional

import typer

from ._helpers import app, _SERVICE_URL, _SERVICE_TIMEOUT, _service_post, _json_output
from ..api import MemoryClient
from ..http_clients import get_session as _get_session

# Register deflect command by importing its module (side-effect: @app.command registers it)
from . import _deflect  # noqa: F401


# =============================================================================
# CLARIFY - Query disambiguation and content safety
# =============================================================================


@app.command("clarify")
def clarify_cmd(
    q: str = typer.Option(..., "--q", "-q", help="User query to analyze for ambiguity"),
    persona: str = typer.Option("embry", "--persona", "-p", help="Active persona (for voice)"),
    scope: str = typer.Option("", "--scope", "-s", help="Project scope"),
    context: Optional[str] = typer.Option(None, "--context", "-c",
        help="Prior clarification context (re-query after user answered a clarifying question)"),
    k: int = typer.Option(5, "--k", help="Number of candidate results to inspect"),
) -> None:
    """Detect ambiguous queries and generate specific clarifying questions.

    Runs 3-stage analysis:
      1. Intent mapping (QuerySpec) via /sparta-intent
      2. Taxonomy extraction (bridge keyword + LLM bridges)
      3. Correlation check against QRA corpus

    Triggers clarification when:
      - Intent mapper returns CLARIFY action
      - Taxonomy extraction yields 0 bridges (no domain signal)
      - QRA retrieval confidence < 0.5 (weak matches)
      - Entity IDs extracted but 0 have QRA coverage
      - Intent bridges don't correlate with retrieved QRA bridges

    If query is clear (strong intent + taxonomy + QRA matches), returns
    the recall results directly — no clarification needed.

    Examples:
        memory-agent clarify --q "How do I secure it?"
        memory-agent clarify --q "GPS spoofing" --context "I meant the F-36 GPS receiver"
        memory-agent clarify --q "security" --scope sparta
    """
    # Try service first
    payload = {"q": q, "persona_id": persona, "scope": scope, "k": k}
    if context:
        payload["context"] = context
    service_result = _service_post("/clarify", payload)
    if service_result is not None:
        _json_output(service_result)
        return

    # Direct mode: 3-stage analysis
    result = _clarify_direct(q, persona, scope, context, k)
    _json_output(result)


def _clarify_direct(
    q: str,
    persona: str = "embry",
    scope: str = "",
    context: Optional[str] = None,
    k: int = 5,
) -> dict:
    """Direct-mode clarify without service.

    Returns:
        {
            "needs_clarification": bool,
            "confidence": float,       # 0-1, how clear the query is
            "clarify_questions": [...], # specific follow-up questions (if needed)
            "intent": {...},            # QuerySpec from intent mapper
            "taxonomy": {...},          # extracted bridges + tier1
            "recall": {...},            # recall results (if query is clear)
            "diagnostics": {...},       # why clarification was triggered
        }
    """
    from ._clarify_helpers import gather_disambiguation_evidence, generate_clarify_questions
    from ..lessons.store import get_mind_tags

    def _extract_entities_safe(text: str):  # local to avoid circular load on import
        try:
            from ..entity_extraction import extract_entities as _extract_entities

            return _extract_entities(text)
        except Exception as exc:  # pragma: no cover — diagnostics only
            logger.error("Suppressed entity extraction in clarify: {}", exc)
            return None

    # Combine query with prior context if re-querying
    effective_q = f"{context}. {q}" if context else q

    # -- Stage 1: Intent Mapping --
    intent_spec = _run_intent_mapping(effective_q)
    intent_action = intent_spec.get("action", "QUERY")
    intent_confidence = intent_spec.get("confidence", 0.5)
    intent_bridges = intent_spec.get("bridges", [])
    intent_entities = intent_spec.get("entities", [])
    entity_result = _extract_entities_safe(effective_q)
    acronym_expansions = entity_result.acronym_expansions if entity_result else []
    misspellings = entity_result.misspellings if entity_result else []
    possible_typos = entity_result.possible_typos if entity_result else []

    # -- Stage 2: Taxonomy Extraction --
    taxonomy_bridges = _extract_taxonomy_bridges(effective_q)

    # Merge bridges from both sources
    all_bridges = list(dict.fromkeys(intent_bridges + taxonomy_bridges))

    # -- Stage 3: QRA Corpus Correlation --
    recall_result = None
    qra_correlation = 0.0
    recall_confidence = 0.0
    retrieved_bridges = []

    client = MemoryClient(scope=scope or "sparta", k=k)
    if intent_action == "QUERY":
        recall_result = client.recall(q=effective_q, k=k, threshold=0.3)
        recall_confidence = recall_result.get("confidence", 0.0)

        # Extract mind tags from retrieved QRAs
        for item in recall_result.get("items", []):
            for tag in (get_mind_tags(item) or item.get("bridge_attributes") or []):
                if tag not in retrieved_bridges:
                    retrieved_bridges.append(tag)

        # Calculate bridge correlation: overlap between query bridges and QRA bridges
        if all_bridges and retrieved_bridges:
            overlap = len(set(all_bridges) & set(retrieved_bridges))
            qra_correlation = overlap / max(len(all_bridges), 1)
        elif recall_confidence >= 0.5:
            qra_correlation = 0.7  # Good recall even without bridge match

    # -- Decision Logic --
    diagnostics = {}
    needs_clarification = False

    # Trigger 1: Intent mapper explicitly said CLARIFY
    if intent_action == "CLARIFY":
        needs_clarification = True
        diagnostics["intent_says_clarify"] = True

    # Trigger 2: Zero taxonomy bridges (no domain signal at all)
    if not all_bridges and intent_action != "NO_MATCH":
        needs_clarification = True
        diagnostics["zero_bridges"] = True

    # Trigger 3: Low recall confidence
    if intent_action == "QUERY" and recall_confidence < 0.4:
        needs_clarification = True
        diagnostics["low_recall_confidence"] = recall_confidence

    # Trigger 4: Entities extracted but none have QRAs
    # Check recall items for entity-matched QRAs (no raw AQL -- use /memory results)
    if intent_entities and recall_result:
        items = recall_result.get("items", [])
        covered = any(
            any(e.lower() == (item.get("control_id", "") or "").lower()
                for e in intent_entities)
            for item in items
        )
        if not covered:
            needs_clarification = True
            diagnostics["entities_without_qras"] = intent_entities

    # Trigger 5: Low bridge correlation between query and retrieved QRAs
    if all_bridges and retrieved_bridges and qra_correlation < 0.3:
        needs_clarification = True
        diagnostics["low_bridge_correlation"] = qra_correlation

    # Trigger 6: Acronym expansions detected — confirm meaning explicitly
    if acronym_expansions:
        needs_clarification = True
        diagnostics["acronym_expansions"] = [exp["acronym"] for exp in acronym_expansions]

    if misspellings:
        needs_clarification = True
        diagnostics["misspellings"] = [entry["word"] for entry in misspellings]

    if possible_typos:
        needs_clarification = True
        diagnostics["possible_typos"] = [entry["word"] for entry in possible_typos]

    # -- Skill-Chain Check (before clarify) --
    # If the query maps to a known skill chain, it's not ambiguous --
    # route to the skill handler instead of asking clarifying questions.
    skill_route = None
    if needs_clarification or intent_action == "CLARIFY":
        try:
            from ..lessons.capability_routing import check_skill_chain
            skill_route = check_skill_chain(effective_q)
        except Exception as exc:
            logger.error("Suppressed error in query: {}", exc)
    if skill_route:
        if misspellings or possible_typos or acronym_expansions:
            diagnostics["skill_chain_suppressed"] = skill_route["chain"]
        else:
            needs_clarification = False
            diagnostics["skill_chain_override"] = skill_route["chain"]

    # -- Disambiguation Evidence --
    evidence = {"richness_score": 0.0}
    if intent_entities:
        try:
            evidence = gather_disambiguation_evidence(intent_entities)
        except Exception as exc:
            logger.error("Suppressed error in query: {}", exc)

    # Rich coverage suppresses clarification
    if (
        evidence.get("richness_score", 0) >= 0.6
        and not misspellings
        and not possible_typos
        and not acronym_expansions
    ):
        needs_clarification = False
        diagnostics["richness_override"] = evidence["richness_score"]

    # Sparse coverage reinforces clarification
    if evidence.get("richness_score", 0) < 0.2 and intent_entities:
        if not needs_clarification:
            needs_clarification = True
            diagnostics["sparse_coverage"] = evidence["richness_score"]

    # Calculate overall confidence (evidence gets 25% weight)
    confidence = (
        intent_confidence * 0.30
        + recall_confidence * 0.30
        + qra_correlation * 0.15
        + evidence.get("richness_score", 0) * 0.25
    )

    # Override: if recall found strong matches, no clarification needed
    if (
        recall_confidence >= 0.7
        and not diagnostics.get("intent_says_clarify")
        and not misspellings
        and not possible_typos
        and not acronym_expansions
    ):
        needs_clarification = False
        confidence = max(confidence, recall_confidence)

    # -- Generate Clarifying Questions --
    clarify_questions = []
    if needs_clarification:
        clarify_questions = generate_clarify_questions(
            q,
            intent_spec,
            all_bridges,
            retrieved_bridges,
            recall_result,
            persona,
            diagnostics,
            acronym_expansions=acronym_expansions,
            misspellings=misspellings,
            possible_typos=possible_typos,
            client=client,
        )

    output = {
        "needs_clarification": needs_clarification,
        "confidence": round(confidence, 3),
        "clarify_questions": clarify_questions,
        "intent": {
            "action": intent_action,
            "entities": intent_entities,
            "bridges": intent_bridges,
            "tier1": intent_spec.get("tier1", []),
            "confidence": intent_confidence,
        },
        "taxonomy": {
            "bridges": taxonomy_bridges,
            "merged_bridges": all_bridges,
        },
        "evidence": evidence,
        "diagnostics": diagnostics,
    }

    # Include skill_route if detected
    if skill_route:
        output["skill_route"] = skill_route

    # Include recall results if query is clear
    if not needs_clarification and recall_result:
        output["recall"] = recall_result

    if acronym_expansions:
        output["acronym_expansions"] = acronym_expansions

    return output


def _run_intent_mapping(query: str) -> dict:
    """Run intent mapping via the consolidated IntentMapper in graph_memory.intent."""
    try:
        from graph_memory.intent import get_mapper
        mapper = get_mapper(use_llm=False, use_classifiers=True)
        result = mapper.infer(query)
        # Normalize: ensure 'bridges' key exists for clarify_direct compatibility
        if "bridges" not in result:
            result["bridges"] = result.get("tier0", [])
        if "confidence" not in result:
            result["confidence"] = 0.7 if result.get("action") == "QUERY" else 0.3
        return result
    except Exception as e:
        # Fallback: static extraction (no classifier)
        from graph_memory.intent import extract_entities, extract_tier0_tags, extract_keywords
        bridges = extract_tier0_tags(query)
        entities = extract_entities(query)
        kws = extract_keywords(query)

        words = query.strip().split()
        if len(words) < 3:
            return {
                "action": "CLARIFY",
                "clarify_question": "Could you provide more details about what you're looking for?",
                "bridges": bridges,
                "entities": entities,
                "confidence": 0.3,
                "_fallback": True,
            }
        return {
            "action": "QUERY",
            "bridges": bridges,
            "entities": entities,
            "tier1": [],
            "keywords": kws,
            "confidence": 0.6,
            "_fallback": True,
        }


def _extract_taxonomy_bridges(text: str) -> list:
    """Extract taxonomy bridges using the consolidated bridge keywords."""
    try:
        from graph_memory.intent import extract_tier0_tags
        return extract_tier0_tags(text)
    except Exception as exc:
        logger.error("Suppressed error in query: {}", exc)
        # Inline minimal extraction
        text_lower = text.lower()
        bridges = []
        patterns = {
            "Precision": ["gps", "navigation", "timing", "sensor", "accuracy"],
            "Resilience": ["defense", "recover", "harden", "protect", "mitigat"],
            "Fragility": ["vulnerab", "weakness", "exploit", "attack surface"],
            "Corruption": ["tamper", "spoof", "inject", "manipulat", "integrity"],
            "Loyalty": ["auth", "encrypt", "trust", "access control", "identity"],
            "Stealth": ["evasion", "covert", "exfiltrat", "persist", "hide"],
        }
        for bridge, pats in patterns.items():
            if any(p in text_lower for p in pats):
                bridges.append(bridge)
        return bridges


@app.command("intent")
def intent_cmd(
    q: str = typer.Option(..., "--q", "-q", help="User query to analyze"),
    scope: str = typer.Option("", "--scope", "-s", help="Project scope"),
    fast: bool = typer.Option(False, "--fast", help="Keyword-only taxonomy (no LLM)"),
) -> None:
    """Extract intent + taxonomy from ANY query. Run BEFORE recall.

    Returns structured IntentSpec:
      - action: QUERY | CLARIFY | LEARN | NO_MATCH
      - entities: extracted entity IDs (control_ids, CWE-*, T1xxx, etc.)
      - taxonomy_tags: bridge attributes + domain tags
      - keywords: extracted keywords for recall
      - recommended_collections: which RecallSources to prioritize

    This is domain-agnostic. Works for SPARTA, Horus, extractor, any project.

    Examples:
        memory-agent intent --q "How does SV-SP-1 protect against laser attacks" --scope sparta
        memory-agent intent --q "What is Horus's opinion on loyalty" --scope horus
    """
    result = _intent_direct(q, scope, fast)
    _json_output(result)


def _intent_direct(q: str, scope: str = "", fast: bool = False) -> dict:
    """Direct-mode intent extraction."""
    # 1. Taxonomy extraction (bridges + domain tags)
    taxonomy_bridges = _extract_taxonomy_bridges(q)

    # 2. Intent mapping (action + entities + keywords)
    intent_spec = _run_intent_mapping(q)

    # 3. Merge and recommend collections
    all_tags = list(dict.fromkeys(
        intent_spec.get("bridges", []) + taxonomy_bridges
    ))

    # Recommend collections based on detected entities/scope
    recommended = []
    entities = intent_spec.get("entities", [])
    q_lower = q.lower()

    # SPARTA signals
    if scope == "sparta" or any(
        e.startswith(("SV-", "REC-", "DE-", "EX-", "CM")) for e in entities
    ):
        recommended.append("sparta_qra")
    if any(e.startswith("CWE-") for e in entities) or "cwe" in q_lower:
        recommended.append("sparta_qra")
    # ATT&CK signals
    if any(e.startswith("T1") for e in entities) or "att&ck" in q_lower or "mitre" in q_lower:
        recommended.append("sparta_qra")
    # Always include datalake and lessons as base
    recommended.extend(["datalake_chunks", "lessons"])

    return {
        "action": intent_spec.get("action", "QUERY"),
        "entities": entities,
        "taxonomy_tags": all_tags,
        "keywords": intent_spec.get("tier1", []),
        "confidence": intent_spec.get("confidence", 0.5),
        "recommended_collections": list(dict.fromkeys(recommended)),
        "scope": scope,
    }
