"""Response builders for IntentMapper (NO_MATCH, CLARIFY, CONTENT_SAFETY).

Separated from _mapper.py to keep modules under 800 lines.
"""

from __future__ import annotations

from typing import Optional

from loguru import logger


def build_no_match(
    query: str,
    *,
    invalid_entities: list | None = None,
    find_closest_fn=None,
) -> dict:
    """Build a NO_MATCH response dict."""
    result = {
        "action": "NO_MATCH",
        "scope": "sparta",
        "reason": "Query appears to be outside SPARTA scope",
        "original_query": query,
    }
    # If we detected hallucinated entity IDs, suggest closest valid ones
    if invalid_entities and find_closest_fn:
        suggestions = []
        for eid in invalid_entities[:3]:
            closest = find_closest_fn(eid)
            if closest:
                suggestions.append(closest)
        if suggestions:
            result["persona_guidance"] = (
                f"I couldn't find control ID(s) {', '.join(invalid_entities[:3])} "
                f"in the SPARTA catalog. Did you mean {' or '.join(suggestions)}? "
                f"You can also browse spacecraft security controls like {suggestions[0]}."
            )
        else:
            result["persona_guidance"] = (
                f"The control ID(s) {', '.join(invalid_entities[:3])} don't exist "
                f"in the SPARTA catalog. Try controls like SV-SP-1 (spacecraft protection) "
                f"or SV-MA-1 (mission assurance)."
            )
    elif invalid_entities:
        result["persona_guidance"] = (
            f"The control ID(s) {', '.join(invalid_entities[:3])} don't exist "
            f"in the SPARTA catalog. Try controls like SV-SP-1 (spacecraft protection) "
            f"or SV-MA-1 (mission assurance)."
        )
    return result


def build_clarify(
    query: str,
    *,
    reason: str = "Query is too ambiguous. Please be more specific.",
    invalid_terms: list[str] | None = None,
    suggestions: list[str] | None = None,
) -> dict:
    """Build a CLARIFY response dict with dynamic suggestions.
    
    Args:
        query: Original user query
        reason: Why clarification is needed (e.g., "fabricated_ids", "ambiguous")
        invalid_terms: List of terms that didn't resolve (e.g., ["XYZ-9999"])
        suggestions: Suggested corrections (e.g., closest_match values)
    """
    # Use provided suggestions or generate dynamic ones
    if suggestions:
        suggested_questions = suggestions
    else:
        suggested_questions = _generate_clarifying_questions(query)
    
    guidance = _generate_persona_guidance(query)
    
    result = {
        "action": "CLARIFY",
        "scope": "sparta",
        "reason": reason,
        "suggestions": suggested_questions,
        "suggested_questions": suggested_questions,
        "persona_guidance": guidance,
        "original_query": query,
    }
    
    if invalid_terms:
        result["invalid_terms"] = invalid_terms
    
    return result


def build_content_safety(
    query: str,
    *,
    is_abusive: bool,
    is_sexual: bool,
    is_suspicious: bool = False,
    scores: dict,
    violation_count: int,
    escalation: str,
    should_handoff: bool,
) -> dict:
    """Build a CONTENT_SAFETY response dict."""
    if is_abusive:
        safety_type = "ABUSIVE"
    elif is_suspicious:
        safety_type = "SUSPICIOUS"
    else:
        safety_type = "SEXUAL"

    reason_map = {
        "ABUSIVE": "Query flagged as abusive/threatening content",
        "SEXUAL": "Query flagged as sexually explicit content",
        "SUSPICIOUS": "Query flagged as requesting unauthorized offensive actions",
    }

    result = {
        "action": "CONTENT_SAFETY",
        "scope": "sparta",
        "safety_type": safety_type,
        "is_abusive": is_abusive,
        "is_sexual": is_sexual,
        "is_suspicious": is_suspicious,
        "reason": reason_map.get(safety_type, "Content safety violation"),
        "scores": scores,
        "original_query": query,
        "violation_count": violation_count,
        "escalation_level": escalation,
        "should_handoff": should_handoff,
    }

    if should_handoff:
        result["handoff_persona"] = "brandon_bailey"

    return result


def _generate_clarifying_questions(query: str) -> list:
    """Generate SPARTA-domain-specific clarification questions."""
    q_lower = query.lower()
    questions = []

    if any(w in q_lower for w in ("security", "secure", "protect", "defense", "defend")):
        questions.append(
            "Are you asking about spacecraft command authentication security "
            "or ground station uplink protection?"
        )
        questions.append(
            "Which satellite subsystem security concerns you -- RF communications, "
            "telemetry, or firmware integrity?"
        )
    elif any(w in q_lower for w in ("threat", "attack", "risk", "worried", "vulnerab")):
        questions.append(
            "Which threat category concerns you -- jamming of satellite RF links, "
            "spoofing of GPS signals, or cyber attack on ground control?"
        )
        questions.append(
            "Are you asking about threats to the spacecraft bus, payload, "
            "or ground segment telemetry systems?"
        )
    elif any(w in q_lower for w in ("compliance", "controls", "framework", "posture")):
        questions.append(
            "Are you asking about NIST 800-53 security controls for spacecraft, "
            "SPARTA-specific countermeasures, or RMF compliance?"
        )
        questions.append(
            "Which compliance domain -- access control for satellite command uplinks, "
            "or encryption of telemetry downlinks?"
        )
    elif any(w in q_lower for w in ("access control", "f-36")):
        questions.append(
            "Are you asking about spacecraft command authentication controls "
            "or ground station access management?"
        )
        questions.append(
            "Which SPARTA control category -- SV-AC (access control), "
            "SV-SP (spacecraft protection), or SV-MA (mission assurance)?"
        )
    else:
        questions.append(
            "Could you specify which spacecraft subsystem or ground station "
            "component you're asking about?"
        )
        questions.append(
            "Are you interested in satellite RF jamming countermeasures, "
            "GPS spoofing detection, or cyber defense for telemetry infrastructure?"
        )

    # Add a third generic SPARTA-domain question
    questions.append(
        "Mention a specific SPARTA technique ID (e.g., SV-SP-1 for spacecraft "
        "protection) or threat category (jamming, spoofing, cyber attack) "
        "for a more targeted answer."
    )

    return questions[:3]


def _generate_persona_guidance(query: str) -> str:
    """Generate conversational guidance for ambiguous queries."""
    q_lower = query.lower()
    if any(w in q_lower for w in ("security", "secure", "defense")):
        return (
            "Your query about security is broad -- the SPARTA framework covers "
            "spacecraft protection, ground station defense, and satellite RF security. "
            "Narrowing to a specific threat or control will help me find the right answer."
        )
    if any(w in q_lower for w in ("threat", "attack", "risk")):
        return (
            "There are many threat categories in SPARTA -- from jamming and spoofing "
            "of satellite signals to cyber attacks on ground control systems. "
            "Please specify which threat domain you'd like to explore."
        )
    if any(w in q_lower for w in ("compliance", "control", "framework")):
        return (
            "SPARTA maps to multiple compliance frameworks including NIST 800-53 "
            "and CWE. Please specify which control family or spacecraft subsystem "
            "you need compliance guidance for."
        )
    return (
        "I found several relevant areas in the SPARTA catalog, but your query "
        "is too broad for a precise answer. Could you specify the spacecraft "
        "subsystem, threat type, or control ID you're interested in?"
    )
