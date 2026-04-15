"""Generate LLM response for evidence case: ANSWER, DEFLECT, or CLARIFY.

This module takes a complete evidence case and generates an appropriate response:
- ANSWER: Synthesize a grounded answer using the evidence
- DEFLECT: Redirect off-topic queries with helpful guidance
- CLARIFY: Generate clarifying questions for ambiguous queries

The response is grounded in the evidence case — citations reference specific
entities, crosswalk chains, and QRAs from the evidence.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from loguru import logger
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Response Models
# ---------------------------------------------------------------------------

class AnswerResponse(BaseModel):
    """Grounded answer synthesized from evidence."""

    content: str = Field(description="The synthesized answer text")
    citations: list[str] = Field(
        default_factory=list,
        description="Entity IDs cited in the answer (e.g., IA-0006, CWE-924)"
    )
    confidence: str = Field(
        default="medium",
        description="Confidence level: high, medium, low"
    )
    caveats: list[str] = Field(
        default_factory=list,
        description="Important limitations or caveats"
    )


class ClarifyResponse(BaseModel):
    """Clarifying questions for ambiguous queries."""

    questions: list[str] = Field(
        description="Clarifying questions to ask the user"
    )
    reason: str = Field(
        description="Why clarification is needed"
    )
    suggestions: list[str] = Field(
        default_factory=list,
        description="Example refined queries the user could try"
    )


class DeflectResponse(BaseModel):
    """Redirect for off-topic queries."""

    message: str = Field(
        description="Helpful redirect message"
    )
    in_scope_topics: list[str] = Field(
        default_factory=list,
        description="Topics that ARE in scope for this system"
    )


class EvidenceCaseResponse(BaseModel):
    """Complete LLM response for an evidence case."""

    action: str = Field(description="ANSWER, CLARIFY, or DEFLECT")
    answer: AnswerResponse | None = None
    clarify: ClarifyResponse | None = None
    deflect: DeflectResponse | None = None
    model: str = Field(default="", description="Model used for generation")
    latency_ms: int = Field(default=0, description="LLM call latency")


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a SPARTA compliance expert assistant. Your role is to help users understand space system security requirements by providing grounded, authoritative answers.

You have access to an evidence case containing:
- Glossary: Definitions of relevant security entities (CWE, SPARTA, ATT&CK, CAPEC)
- Crosswalk Chains: Mappings between frameworks (e.g., CWE-924 → IA-0006)
- QRA Evidence: Question-Rationale-Answer triples from the knowledge base
- Related QRAs: Additional QRAs that share SPARTA techniques with the primary evidence

CRITICAL RULES:
1. Only cite entities that appear in the glossary or crosswalk chains
2. Only make claims supported by the QRA evidence
3. If evidence is insufficient, say so — do not hallucinate
4. Prefer specific, actionable guidance over vague generalities
5. When citing SPARTA controls, use the format "SPARTA IA-0006" or similar

OUTPUT FORMAT: Return valid JSON matching the schema below.
"""

ANSWER_PROMPT_TEMPLATE = """Based on the evidence case below, synthesize a grounded answer to the user's question.

## User Question
{question}

## Glossary (Entity Definitions)
{glossary}

## Crosswalk Chains (Framework Mappings)
{crosswalk_chains}

## Primary QRA Evidence
{prior_qra_evidence}

## Related QRA Evidence (Shared SPARTA Techniques)
{related_qra_evidence}

## Shared Techniques Summary
{shared_techniques_summary}

---

Synthesize an answer that:
1. Directly addresses the user's question
2. Cites specific entities from the glossary (by ID)
3. References QRA evidence where applicable
4. Acknowledges any limitations in the evidence

Return JSON:
{{
  "content": "Your synthesized answer here...",
  "citations": ["IA-0006", "CWE-924"],
  "confidence": "high" | "medium" | "low",
  "caveats": ["Any important limitations"]
}}
"""

CLARIFY_PROMPT_TEMPLATE = """The user's question lacks sufficient grounding evidence. Generate clarifying questions to help refine the query.

## User Question
{question}

## Available Glossary (What We Know About)
{glossary}

## Evidence Status
- Crosswalk chains found: {has_crosswalk}
- QRA evidence found: {has_qra}
- Related QRAs found: {has_related}

---

Generate 2-3 clarifying questions that would help narrow down the query to something we can answer with our SPARTA compliance knowledge base.

Return JSON:
{{
  "questions": ["Clarifying question 1?", "Clarifying question 2?"],
  "reason": "Why we need clarification",
  "suggestions": ["Example: How do I protect satellite C2 links from jamming?"]
}}
"""

DEFLECT_PROMPT_TEMPLATE = """The user's question appears to be outside the scope of SPARTA compliance and space system security. Generate a helpful redirect.

## User Question
{question}

## In-Scope Topics (What This System Covers)
- SPARTA framework controls and countermeasures
- Space system security requirements
- CWE weaknesses relevant to space systems
- ATT&CK techniques targeting space infrastructure
- Compliance mapping between security frameworks

---

Generate a polite redirect that:
1. Acknowledges the question
2. Explains what this system covers
3. Suggests how to rephrase if there's a space security angle

Return JSON:
{{
  "message": "Your redirect message here...",
  "in_scope_topics": ["SPARTA controls", "Space system security", "CWE mappings"]
}}
"""


# ---------------------------------------------------------------------------
# LLM Call
# ---------------------------------------------------------------------------

def _call_scillm(
    system_prompt: str,
    user_prompt: str,
    timeout_s: float = 45.0,
    model: str = "text",
) -> tuple[str, int]:
    """Call scillm and return raw text + latency_ms."""
    import time

    api_base = os.getenv("SCILLM_API_BASE", "http://localhost:4001")
    api_key = os.getenv("SCILLM_PROXY_KEY", "sk-dev-proxy-123")

    start = time.perf_counter()
    resp = httpx.post(
        f"{api_base}/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "x-expect-json": "true",
            "x-caller-skill": "evidence-case-response",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,  # Slight creativity for answers
            "response_format": {"type": "json_object"},
        },
        timeout=timeout_s,
    )
    latency_ms = int((time.perf_counter() - start) * 1000)
    resp.raise_for_status()
    data = resp.json()
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return text, latency_ms


def _extract_json(text: str) -> dict[str, Any]:
    """Extract JSON from text."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not extract JSON: {text[:200]}...")


def _format_glossary(glossary: list[dict]) -> str:
    """Format glossary for prompt."""
    if not glossary:
        return "(No entities found)"
    lines = []
    for g in glossary[:15]:  # Cap at 15 entities
        lines.append(f"- {g.get('id', '?')} ({g.get('framework', '?')}): {g.get('description', '')[:200]}")
    return "\n".join(lines)


def _format_crosswalk(chains: list[dict]) -> str:
    """Format crosswalk chains for prompt."""
    if not chains:
        return "(No crosswalk chains found)"
    lines = []
    for c in chains[:10]:  # Cap at 10 chains
        hops = " → ".join(h.get("id", "?") for h in c.get("hops", []))
        lines.append(f"- {c.get('from', '?')} ({c.get('from_framework', '?')}) → {hops}")
    return "\n".join(lines)


def _format_qras(qras: list[dict], label: str = "QRA") -> str:
    """Format QRA evidence for prompt."""
    if not qras:
        return f"(No {label} evidence found)"
    lines = []
    for q in qras[:8]:  # Cap at 8 QRAs
        question = (q.get("question") or q.get("citation_id") or "")[:150]
        answer = (q.get("answer") or "")[:300]
        techniques = q.get("shared_techniques", [])
        tech_str = f" [shares: {', '.join(techniques)}]" if techniques else ""
        lines.append(f"- Q: {question}\n  A: {answer}{tech_str}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main Function
# ---------------------------------------------------------------------------

def generate_evidence_case_response(
    action: str,
    question: str,
    glossary: list[dict],
    crosswalk_chains: list[dict],
    prior_qra_evidence: list[dict],
    related_qra_evidence: list[dict],
    shared_techniques_summary: dict[str, list[str]],
    *,
    timeout_s: float = 45.0,
    model: str = "text",
) -> EvidenceCaseResponse:
    """Generate LLM response for an evidence case.

    Args:
        action: ANSWER, CLARIFY, or DEFLECT
        question: User's original question
        glossary: Entity definitions from evidence case
        crosswalk_chains: Framework mappings
        prior_qra_evidence: Primary QRA evidence
        related_qra_evidence: Related QRAs via lineage
        shared_techniques_summary: Technique → [qra_keys] mapping
        timeout_s: LLM call timeout
        model: Model to use

    Returns:
        EvidenceCaseResponse with appropriate action-specific response
    """
    # Format evidence for prompts
    glossary_str = _format_glossary(glossary)
    crosswalk_str = _format_crosswalk(crosswalk_chains)
    prior_qra_str = _format_qras(prior_qra_evidence, "Primary QRA")
    related_qra_str = _format_qras(related_qra_evidence, "Related QRA")
    techniques_str = json.dumps(shared_techniques_summary, indent=2) if shared_techniques_summary else "(none)"

    try:
        if action == "ANSWER":
            user_prompt = ANSWER_PROMPT_TEMPLATE.format(
                question=question,
                glossary=glossary_str,
                crosswalk_chains=crosswalk_str,
                prior_qra_evidence=prior_qra_str,
                related_qra_evidence=related_qra_str,
                shared_techniques_summary=techniques_str,
            )
            text, latency_ms = _call_scillm(SYSTEM_PROMPT, user_prompt, timeout_s, model)
            data = _extract_json(text)
            answer = AnswerResponse(
                content=data.get("content", ""),
                citations=data.get("citations", []),
                confidence=data.get("confidence", "medium"),
                caveats=data.get("caveats", []),
            )
            return EvidenceCaseResponse(
                action="ANSWER",
                answer=answer,
                model=model,
                latency_ms=latency_ms,
            )

        elif action == "CLARIFY":
            user_prompt = CLARIFY_PROMPT_TEMPLATE.format(
                question=question,
                glossary=glossary_str,
                has_crosswalk=bool(crosswalk_chains),
                has_qra=bool(prior_qra_evidence),
                has_related=bool(related_qra_evidence),
            )
            text, latency_ms = _call_scillm(SYSTEM_PROMPT, user_prompt, timeout_s, model)
            data = _extract_json(text)
            clarify = ClarifyResponse(
                questions=data.get("questions", []),
                reason=data.get("reason", ""),
                suggestions=data.get("suggestions", []),
            )
            return EvidenceCaseResponse(
                action="CLARIFY",
                clarify=clarify,
                model=model,
                latency_ms=latency_ms,
            )

        elif action == "DEFLECT":
            user_prompt = DEFLECT_PROMPT_TEMPLATE.format(question=question)
            text, latency_ms = _call_scillm(SYSTEM_PROMPT, user_prompt, timeout_s, model)
            data = _extract_json(text)
            deflect = DeflectResponse(
                message=data.get("message", ""),
                in_scope_topics=data.get("in_scope_topics", []),
            )
            return EvidenceCaseResponse(
                action="DEFLECT",
                deflect=deflect,
                model=model,
                latency_ms=latency_ms,
            )

        else:
            raise ValueError(f"Unknown action: {action}")

    except Exception as exc:
        logger.error("evidence-case-response: LLM call failed: {}", exc)
        # Return empty response with error info
        return EvidenceCaseResponse(
            action=action,
            model=model,
            latency_ms=0,
        )
