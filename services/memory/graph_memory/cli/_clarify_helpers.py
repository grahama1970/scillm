"""Clarify pipeline helpers: disambiguation evidence and question generation.

Extracted from query.py to keep modules under 800 lines.
All public functions are used by _clarify_direct in query.py.
"""
from __future__ import annotations

from typing import Optional

from loguru import logger


def gather_disambiguation_evidence(
    entities: list, target_control: str | None = None,
) -> dict:
    """Gather coverage richness signals for disambiguation via /memory recall.

    Uses MemoryClient.recall() with three RecallSources:
    - sparta_qra: QRA count, mind tags, grounding scores
    - sparta_controls: lean4 status (from implementing_requirements edges)
    - sparta_relationships: cross-framework coverage + relationship count

    Returns a dict with framework_coverage, relationship_count, lean4_status,
    qra_count, graph_depth, taxonomy_overlap, and composite richness_score.
    """
    from ..api import MemoryClient
    from ..lessons.store import get_mind_tags

    cids = list(entities or [])
    if target_control and target_control not in cids:
        cids.insert(0, target_control)
    if not cids:
        return {"richness_score": 0.0}

    evidence: dict = {
        "framework_coverage": {},
        "relationship_count": 0,
        "lean4_status": {"proven": 0, "pending": 0, "failed": 0},
        "qra_count": 0,
        "graph_depth": 0,
        "taxonomy_overlap": 0.0,
        "richness_score": 0.0,
    }

    # Single /memory recall call with all three relevant collections
    try:
        client = MemoryClient(scope="sparta", k=20)
        query = " ".join(cids[:3])
        recall_result = client.recall(
            q=query, k=20,
            collections=["sparta_qra", "sparta_controls", "sparta_relationships"],
            entities=cids[:5],
        )
    except Exception as exc:
        logger.error("Suppressed error in gather_disambiguation_evidence: {}", exc)
        return evidence

    items = recall_result.get("items", [])

    # Parse recall items by source type.
    # Only count items that match our queried entities exactly --
    # recall also returns BM25 text matches which may be unrelated.
    cids_set = {c.lower() for c in cids}
    framework_counts: dict = {}
    relationship_count = 0
    lean4_status = {"proven": 0, "pending": 0, "failed": 0}
    qra_count = 0
    all_qra_tags: set = set()

    for item in items:
        item_type = item.get("type", "")

        if item_type == "qra_context":
            # Only count QRAs whose control_id matches our entities
            item_cid = (item.get("control_id") or "").lower()
            if item_cid not in cids_set:
                continue
            qra_count += 1
            for tag in (get_mind_tags(item) or []):
                if isinstance(tag, str):
                    all_qra_tags.add(tag.lower())

        elif item_type == "control_metadata":
            item_cid = (item.get("control_id") or "").lower()
            if item_cid not in cids_set:
                continue
            # Lean4 status from implementing_requirements edges
            for req in (item.get("implementing_requirements") or []):
                status = (req.get("lean4_status") or "pending").lower()
                if status in lean4_status:
                    lean4_status[status] += 1

        elif item_type == "control_relationship":
            # Only count relationships that involve our queried entities
            src_cid = (item.get("source_control_id") or "").lower()
            tgt_cid = (item.get("target_control_id") or "").lower()
            if src_cid not in cids_set and tgt_cid not in cids_set:
                continue
            relationship_count += 1
            # Framework classification is done by the formatter
            tgt_fw = item.get("target_framework", "other")
            src_fw = item.get("source_framework", "other")
            # Count the "other" side's framework (not the queried entity's)
            for fw in (tgt_fw, src_fw):
                if fw != "other":
                    framework_counts[fw] = framework_counts.get(fw, 0) + 1

    evidence["framework_coverage"] = framework_counts
    evidence["relationship_count"] = relationship_count
    evidence["lean4_status"] = lean4_status
    evidence["qra_count"] = qra_count

    # Taxonomy overlap: Jaccard(query entity tags, QRA mind tags)
    entity_tags = {e.lower() for e in cids}
    if all_qra_tags and entity_tags:
        intersection = len(all_qra_tags & entity_tags)
        union = len(all_qra_tags | entity_tags)
        evidence["taxonomy_overlap"] = round(intersection / union, 3) if union else 0.0

    # Graph depth: 0 = self has QRAs, 1+ = needs neighbors
    evidence["graph_depth"] = 0 if qra_count > 0 else 2

    # Composite richness score (deterministic, no LLM)
    fw_count = len(framework_counts)
    lean4_proven = lean4_status.get("proven", 0)
    graph_depth = evidence["graph_depth"]
    tax_overlap = evidence["taxonomy_overlap"]

    richness = (
        min(fw_count / 3, 1.0) * 0.25              # 3+ frameworks = max
        + min(relationship_count / 20, 1.0) * 0.20  # 20+ rels = max
        + min(qra_count / 10, 1.0) * 0.25           # 10+ QRAs = max
        + (1.0 if lean4_proven > 0 else 0.0) * 0.15  # any proof = max
        + (1.0 - min(graph_depth / 3, 1.0)) * 0.05   # closer = richer
        + tax_overlap * 0.10                          # tag intersection
    )
    evidence["richness_score"] = round(richness, 3)

    return evidence


def generate_clarify_questions(
    query: str,
    intent_spec: dict,
    query_bridges: list,
    retrieved_bridges: list,
    recall_result: Optional[dict],
    persona: str,
    diagnostics: dict,
    *,
    acronym_expansions: Optional[list] = None,
    misspellings: Optional[list] = None,
    possible_typos: Optional[list] = None,
    client=None,
) -> list:
    """Generate specific clarifying questions based on diagnostic signals.

    Returns 2-4 questions, each with a rationale.
    """
    questions = []

    # If intent mapper already provided a clarify question, use it
    if intent_spec.get("clarify_question"):
        questions.append({
            "question": intent_spec["clarify_question"],
            "rationale": "Intent mapper detected ambiguity",
            "source": "intent_mapper",
        })

    if misspellings:
        for misspelling in misspellings[:2]:
            control_id = misspelling.get("control_id") or ""
            control_name = misspelling.get("control_name") or misspelling.get("suggestion") or ""
            if control_id:
                question = (
                    f"Did you mean the control '{control_id}' "
                    f"({control_name}) when you wrote '{misspelling.get('word')}'?"
                )
            else:
                question = (
                    f"Did you mean '{misspelling.get('suggestion')}' "
                    f"when you wrote '{misspelling.get('word')}'?"
                )
            questions.append({
                "question": question,
                "rationale": "Compound or misspelled technical term detected",
                "source": "misspelling",
            })

    if possible_typos:
        for typo in possible_typos[:2]:
            fuzzy_matches = typo.get("fuzzy_matches") or []
            if fuzzy_matches:
                guess = fuzzy_matches[0].get("match") or ""
                word = str(typo.get("word") or "")
                stem_like = guess == word[:-1] or word == guess[:-1]
                if len(guess) >= max(5, len(word) - 1) and not stem_like:
                    question = (
                        f"The term '{word}' looks incomplete or misspelled. "
                        f"Did you mean '{guess}', or something else?"
                    )
                else:
                    question = (
                        f"The term '{word}' looks incomplete or misspelled. "
                        "Could you restate that term more precisely?"
                    )
            else:
                question = (
                    f"The term '{typo.get('word')}' looks incomplete or misspelled. "
                    "Could you restate that term more precisely?"
                )
            questions.append({
                "question": question,
                "rationale": "Single-token corpus miss likely indicates a typo or truncation",
                "source": "possible_typo",
            })

    # Acronym expansions: confirm expansion and surface related controls
    if acronym_expansions:
        for expansion in acronym_expansions[:2]:  # limit noise
            related = expansion.get("related_controls") or []
            ctrl_text = ""
            if related:
                labels = ", ".join(_format_control_label(ctrl) for ctrl in related[:3])
                ctrl_text = f"SPARTA has these related controls: {labels}. "
            else:
                suggested = _suggest_controls_for_term(expansion.get("full_name"), client)
                if suggested:
                    ctrl_text = f"SPARTA has these related controls: {', '.join(suggested)}. "
                else:
                    ctrl_text = "I can pull targeted guidance once you confirm. "

            questions.append({
                "question": (
                    f"When you mentioned '{expansion.get('acronym')}', did you mean "
                    f"{expansion.get('full_name')}? {ctrl_text}Should I focus there?"
                ).strip(),
                "rationale": "Acronym detected — needs explicit confirmation",
                "source": "acronym_expansion",
            })

    # Zero bridges: ask about security domain
    if diagnostics.get("zero_bridges"):
        questions.append({
            "question": (
                "What aspect of space systems security are you interested in? "
                "For example: vulnerability analysis (Fragility), defense measures (Resilience), "
                "data integrity attacks (Corruption), or access control (Loyalty)?"
            ),
            "rationale": "No taxonomy bridges extracted — query lacks domain signal",
            "source": "taxonomy_gap",
        })

    # Low recall confidence: suggest specific SPARTA domains
    if diagnostics.get("low_recall_confidence") is not None:
        partial_topics = set()
        if recall_result:
            for item in recall_result.get("items", [])[:3]:
                ctrl = item.get("control_id", "")
                if ctrl.startswith("SV-SP"):
                    partial_topics.add("spacecraft protection")
                elif ctrl.startswith("SV-AC"):
                    partial_topics.add("access control")
                elif ctrl.startswith("SV-MA"):
                    partial_topics.add("supply chain/manufacturing")
                elif ctrl.startswith("SV-IT"):
                    partial_topics.add("information transfer")

        if partial_topics:
            topics_str = ", ".join(sorted(partial_topics))
            questions.append({
                "question": f"I found some partial matches related to {topics_str}. "
                            f"Could you specify which of these areas you're asking about?",
                "rationale": f"Recall confidence {diagnostics['low_recall_confidence']:.0%} — "
                             f"multiple weak matches suggest ambiguity",
                "source": "low_confidence_refine",
            })
        else:
            questions.append({
                "question": (
                    "Could you narrow your question to a specific system component? "
                    "For example: spacecraft bus, ground station, RF communications link, "
                    "or mission control center?"
                ),
                "rationale": "Low recall confidence with no partial matches",
                "source": "low_confidence_generic",
            })

    # Entities without QRAs: specific entity problem
    if diagnostics.get("entities_without_qras"):
        ents = diagnostics["entities_without_qras"]
        questions.append({
            "question": f"The control ID(s) {', '.join(ents[:3])} don't have coverage "
                        f"in our knowledge base. Did you mean a different control, or "
                        f"would you like me to search by topic instead?",
            "rationale": "Extracted entity IDs have no QRA coverage",
            "source": "entity_gap",
        })

    # Low bridge correlation: mismatched domain
    if diagnostics.get("low_bridge_correlation") is not None:
        query_set = set(query_bridges)
        retrieved_set = set(retrieved_bridges)
        missing = query_set - retrieved_set
        if missing:
            questions.append({
                "question": f"Your question touches on {', '.join(missing)}, but "
                            f"the closest matches in our database focus on "
                            f"{', '.join(retrieved_set - query_set) or 'other areas'}. "
                            f"Could you rephrase to target a specific threat or control?",
                "rationale": f"Bridge correlation {diagnostics['low_bridge_correlation']:.0%}",
                "source": "bridge_mismatch",
            })

    # Persona-specific framing
    if persona == "embry" and questions:
        questions[0]["question"] = (
            "I want to make sure I point you in the right direction! "
            + questions[0]["question"]
        )
    elif persona == "brandon_bailey" and questions:
        questions[0]["question"] = (
            "Let me be precise about what I need to look up. "
            + questions[0]["question"]
        )

    # Ensure at least one question
    if not questions:
        questions.append({
            "question": "Could you provide more details about what you're looking for? "
                        "Specific control IDs, threat types, or system components help me "
                        "find the best answers.",
            "rationale": "Generic fallback",
            "source": "fallback",
        })

    return questions[:4]  # Max 4 questions


def _suggest_controls_for_term(term: Optional[str], client, limit: int = 3) -> list[str]:
    from ..api import MemoryClient

    if not term:
        return []
    try:
        working_client = client or MemoryClient(scope="sparta", k=limit)
        recall = working_client.recall(q=term, k=limit, threshold=0.2)
    except Exception as exc:
        logger.error("Suppressed related control lookup: {}", exc)
        return []

    controls: list[str] = []
    for item in recall.get("items", []):
        cid = item.get("control_id") or item.get("title")
        name = item.get("name") or item.get("title")
        if not cid and not name:
            continue
        label = f"{cid} ({name})" if cid and name else (cid or name)
        controls.append(label)
        if len(controls) >= limit:
            break
    return controls


def _format_control_label(control: dict[str, str]) -> str:
    control_id = (control.get("control_id") or "").strip()
    name = (control.get("name") or "").strip()
    if control_id and name:
        return f"{control_id} ({name})"
    return control_id or name or "a related control"
