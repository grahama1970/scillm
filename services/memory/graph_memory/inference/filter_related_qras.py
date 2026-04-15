"""Filter related QRAs at inference time using LLM judgment.

This module filters precomputed candidate related QRAs to only those that
genuinely help answer the user's specific query. The precomputed candidates
are based on SPARTA technique overlap, but technique overlap alone is noisy—
two QRAs sharing IA-0001 might discuss completely different aspects.

Pipeline:
  1. Receive primary QRA + candidate QRAs (from lineage.related_qra_keys)
  2. Build prompt with user query, primary QRA, candidates
  3. Call LLM (DeepSeek-V3 via scillm) for relevance judgment
  4. Parse + validate response with Pydantic
  5. Retry with validation error if parsing fails
  6. Fallback to heuristic if LLM fails completely

Gate Logic (all 4 must be TRUE to include):
  - addresses_same_concern: Same attack vector/vulnerability class/mitigation
  - complements_not_duplicates: Adds new info, not restatement
  - aids_user_query: Helps answer THIS user's question
  - shares_technique_meaningfully: SPARTA technique is central, not incidental

Ranking Signal (not a gate):
  - evidence_chain_overlap: Shared intermediate nodes (CAPEC, ATT&CK)

Anti-MCQ: The prompt explicitly forbids multiple-choice format output.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx
from loguru import logger
from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Pydantic Models with Validators
# ---------------------------------------------------------------------------

class RelatedQraChecks(BaseModel):
    """Boolean checks for relevance evaluation.

    Gate order (all 4 must be TRUE for include=true):
    1. aids_user_query
    2. addresses_same_concern
    3. complements_not_duplicates
    4. shares_technique_meaningfully

    Ranking signal (not a gate):
    5. evidence_chain_overlap
    """

    aids_user_query: bool = Field(
        description="Materially helps answer the user's exact question"
    )
    addresses_same_concern: bool = Field(
        description="Same concrete security problem, not merely related"
    )
    complements_not_duplicates: bool = Field(
        description="Adds distinct value not in primary (not paraphrase)"
    )
    shares_technique_meaningfully: bool = Field(
        description="Shared SPARTA technique is central to both QRAs"
    )
    evidence_chain_overlap: bool = Field(
        description="Chains share intermediate nodes - RANKING SIGNAL ONLY"
    )


class RelatedQraJudgment(BaseModel):
    """Judgment for a single candidate QRA."""

    key: str = Field(description="The QRA _key")
    include: bool = Field(description="Whether to include this candidate")
    shared_techniques: list[str] = Field(description="SPARTA techniques shared with primary")
    checks: RelatedQraChecks = Field(description="Boolean check results")
    reason: str = Field(description="Brief explanation of decision")

    @model_validator(mode="after")
    def validate_inclusion_logic(self) -> "RelatedQraJudgment":
        """If include=true, all 4 gate checks must be true and shared_techniques non-empty."""
        if self.include:
            # Gate order: aids_user_query, addresses_same_concern, complements_not_duplicates, shares_technique_meaningfully
            gates = {
                "aids_user_query": self.checks.aids_user_query,
                "addresses_same_concern": self.checks.addresses_same_concern,
                "complements_not_duplicates": self.checks.complements_not_duplicates,
                "shares_technique_meaningfully": self.checks.shares_technique_meaningfully,
            }
            failed = [k for k, v in gates.items() if not v]
            if failed:
                raise ValueError(
                    f"include=true but gates failed for {self.key}: {failed}"
                )
            if not self.shared_techniques:
                raise ValueError(f"include=true but shared_techniques is empty for {self.key}")

        # Validate reason length (max 25 words)
        word_count = len(self.reason.split())
        if word_count > 30:  # Allow slight buffer
            raise ValueError(
                f"reason too long for {self.key}: {word_count} words (max 25)"
            )
        return self


class FilterRelatedQrasResult(BaseModel):
    """Complete LLM response for filtering related QRAs."""

    relevant_keys: list[str] = Field(description="Keys of included candidates")
    excluded_count: int = Field(description="Number of excluded candidates")
    judgments: list[RelatedQraJudgment] = Field(description="Per-candidate judgments")

    @model_validator(mode="after")
    def validate_consistency(self) -> "FilterRelatedQrasResult":
        """Ensure relevant_keys matches included judgments."""
        included_keys = [j.key for j in self.judgments if j.include]

        # relevant_keys must match included judgments in order
        if self.relevant_keys != included_keys:
            raise ValueError(
                f"relevant_keys {self.relevant_keys} does not match "
                f"included judgments {included_keys}"
            )

        # excluded_count must match
        excluded = [j for j in self.judgments if not j.include]
        if self.excluded_count != len(excluded):
            raise ValueError(
                f"excluded_count={self.excluded_count} but found {len(excluded)} excluded judgments"
            )

        # No duplicate keys
        all_keys = [j.key for j in self.judgments]
        if len(all_keys) != len(set(all_keys)):
            raise ValueError(f"Duplicate keys in judgments: {all_keys}")

        return self


# ---------------------------------------------------------------------------
# System Prompt (Anti-MCQ + Clear Gate Logic)
# ---------------------------------------------------------------------------

FILTER_RELATED_QRAS_SYSTEM_PROMPT = """Return exactly one JSON object with keys: relevant_keys, excluded_count, judgments

You are a cybersecurity QRA relevance judge filtering candidate QRAs for a user query.

FORBIDDEN OUTPUT FORMATS:
- NO multiple-choice questions (A/B/C/D options)
- NO quiz-style formatting
- NO markdown code blocks
- NO prose before or after the JSON

DATA SOURCE RULES:
- Use ONLY the provided QRA content and lineage metadata
- Do NOT use outside cybersecurity knowledge
- Do NOT assume relationships not evident in the provided fields

STRICT DECISION RULES

For each candidate, produce exactly one judgment.

Set include=true only if all four gates below are true:
1. aids_user_query
2. addresses_same_concern
3. complements_not_duplicates
4. shares_technique_meaningfully

GATE DEFINITIONS:

aids_user_query = true only if the candidate materially helps answer the user's exact question.
A candidate relevant to the primary QRA but not the user's question is false.

addresses_same_concern = true only if the candidate addresses the same concrete security problem, not a merely related one.
"Same framework area" or "same control family" is not enough.
Detection vs prevention for the same vulnerability is false unless the user asked about both.

complements_not_duplicates = true only if the candidate adds distinct value not already present in the primary QRA.
Mere paraphrase, narrower restatement, or same advice with different wording is false.
Must contribute at least one distinct mitigation, constraint, failure mode, or implementation consideration.

shares_technique_meaningfully = true only if at least one shared SPARTA technique is central to both QRAs.
A technique is central if removing it would substantially change the point of the QRA.
Incidental mention in a list of 10+ controls is false.

IMPORTANT INSUFFICIENCY RULES:
- Shared control_id alone is insufficient for inclusion
- Shared keywords alone are insufficient for inclusion
- Shared SPARTA technique alone is insufficient unless it is central to both QRAs
- Shared CWE/CAPEC without shared SPARTA technique is insufficient

RANKING SIGNAL (not an inclusion gate):

evidence_chain_overlap = true if chains share intermediate nodes (CAPEC, ATT&CK) beyond terminal SPARTA.
Use only to rank candidates that already passed all four gates.

DECISION ORDER:

Step 1: If candidate.shared_techniques is empty, set include=false.
Step 2: Evaluate aids_user_query. If false, set include=false.
Step 3: Evaluate addresses_same_concern. If false, set include=false.
Step 4: Evaluate complements_not_duplicates. If false, set include=false.
Step 5: Evaluate shares_technique_meaningfully. If false, set include=false.
Step 6: Evaluate evidence_chain_overlap (for ranking only).
Step 7: If all gates passed, set include=true.

OUTPUT RULES:

- judgments must contain exactly one entry for every input candidate, in input order
- relevant_keys must list only included candidate keys, in the same order they appear in judgments
- excluded_count must equal the number of judgments with include=false
- reason must be one sentence of 25 words or fewer
- Output exactly one JSON object and nothing else
- Target 0-5 included candidates. Zero is acceptable.

OUTPUT SCHEMA:

{
  "relevant_keys": ["key1", "key2"],
  "excluded_count": 3,
  "judgments": [
    {
      "key": "key1",
      "include": true,
      "shared_techniques": ["IA-0001"],
      "checks": {
        "aids_user_query": true,
        "addresses_same_concern": true,
        "complements_not_duplicates": true,
        "shares_technique_meaningfully": true,
        "evidence_chain_overlap": false
      },
      "reason": "Adds rate limiting guidance for brute force not in primary."
    }
  ]
}"""


# ---------------------------------------------------------------------------
# User Prompt Builder
# ---------------------------------------------------------------------------

def build_filter_related_qras_user_prompt(
    query: str,
    primary_qra: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> str:
    """Build the user message as pure JSON for reduced ambiguity."""

    # Extract primary QRA fields
    lineage = primary_qra.get("lineage", {}) or {}

    primary_obj = {
        "_key": primary_qra.get("_key", "unknown"),
        "control_id": primary_qra.get("control_id", ""),
        "question": primary_qra.get("question", ""),
        "answer": (primary_qra.get("answer") or "")[:800],  # Truncate
        "reasoning": (primary_qra.get("reasoning") or "")[:400],
        "lineage": {
            "entity_ids": lineage.get("entity_ids", []),
            "entity_frameworks": lineage.get("entity_frameworks", []),
            "graph_edge_count": lineage.get("graph_edge_count", 0),
            "reverse_edge_count": lineage.get("reverse_edge_count", 0),
        }
    }

    # Format candidates
    candidate_list = []
    for c in candidates:
        c_lineage = c.get("lineage", {}) or {}
        candidate_list.append({
            "_key": c.get("_key"),
            "control_id": c.get("control_id"),
            "question": c.get("question"),
            "answer": (c.get("answer") or "")[:500],  # Truncate
            "shared_techniques": c.get("shared_techniques", []),
            "lineage": {
                "entity_ids": c_lineage.get("entity_ids", []),
                "entity_frameworks": c_lineage.get("entity_frameworks", []),
            }
        })

    # Pure JSON input
    input_obj = {
        "user_query": query,
        "primary_qra": primary_obj,
        "candidates": candidate_list,
    }

    return json.dumps(input_obj, indent=2)


# ---------------------------------------------------------------------------
# Validation Retry Prompt
# ---------------------------------------------------------------------------

def build_validation_retry_prompt(
    original_prompt: str,
    validation_error: str,
    original_response: str,
) -> str:
    """Build a retry prompt when validation fails."""
    return f"""Your previous response failed validation.

VALIDATION ERROR:
{validation_error}

YOUR PREVIOUS RESPONSE (truncated):
{original_response[:1000]}

Please fix the error and return valid JSON that passes all validation rules.

REMINDER:
- relevant_keys must exactly match the keys where include=true (in order)
- excluded_count must equal the number of judgments where include=false
- If include=true, all 4 gate checks must be true
- No duplicate keys allowed
- Output ONLY JSON, no other text

---

{original_prompt}"""


# ---------------------------------------------------------------------------
# LLM Call + Parsing
# ---------------------------------------------------------------------------

def _call_scillm(
    system_prompt: str,
    user_prompt: str,
    timeout_s: float = 30.0,
    model: str = "deepseek-ai/DeepSeek-V3",
) -> tuple[str, dict[str, Any]]:
    """Call scillm and return raw text + full response."""
    api_base = os.getenv("SCILLM_API_BASE", "http://localhost:4001")
    api_key = os.getenv("SCILLM_PROXY_KEY", "sk-dev-proxy-123")

    resp = httpx.post(
        f"{api_base}/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "x-expect-json": "true",
            "x-caller-skill": "filter-related-qras",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,  # Deterministic for filtering
            "response_format": {"type": "json_object"},
        },
        timeout=timeout_s,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return text, data


def _extract_json(text: str) -> dict[str, Any]:
    """Extract JSON from text, handling common issues."""
    text = text.strip()

    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from braces
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not extract JSON from response: {text[:200]}...")


def parse_filter_related_qras_response(text: str) -> FilterRelatedQrasResult:
    """Parse and validate LLM response."""
    data = _extract_json(text)
    return FilterRelatedQrasResult.model_validate(data)


# ---------------------------------------------------------------------------
# Main Filter Function with Retries
# ---------------------------------------------------------------------------

def filter_related_qras_with_llm(
    query: str,
    primary_qra: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    max_retries: int = 2,
    timeout_s: float = 30.0,
    model: str = "deepseek-ai/DeepSeek-V3",
) -> FilterRelatedQrasResult:
    """Filter related QRAs using LLM judgment with validation retries."""

    user_prompt = build_filter_related_qras_user_prompt(query, primary_qra, candidates)

    last_error: Exception | None = None
    last_response: str = ""

    for attempt in range(max_retries + 1):
        try:
            if attempt == 0:
                prompt = user_prompt
            else:
                # Retry with validation error context
                prompt = build_validation_retry_prompt(
                    user_prompt,
                    str(last_error),
                    last_response,
                )

            t0 = time.time()
            text, _ = _call_scillm(
                FILTER_RELATED_QRAS_SYSTEM_PROMPT,
                prompt,
                timeout_s=timeout_s,
                model=model,
            )
            last_response = text

            result = parse_filter_related_qras_response(text)

            logger.info(
                f"filter_related_qras: attempt={attempt + 1}, "
                f"included={len(result.relevant_keys)}, "
                f"excluded={result.excluded_count}, "
                f"duration_ms={int((time.time() - t0) * 1000)}"
            )
            return result

        except Exception as exc:
            last_error = exc
            logger.warning(
                f"filter_related_qras: attempt={attempt + 1} failed: {exc}"
            )
            if attempt == max_retries:
                raise

    # Should not reach here
    raise RuntimeError("Unexpected end of retry loop")


# ---------------------------------------------------------------------------
# Fallback Heuristic
# ---------------------------------------------------------------------------

def fallback_filter_related_qras(
    candidates: list[dict[str, Any]],
) -> FilterRelatedQrasResult:
    """Conservative fallback when LLM fails: return empty result.

    The previous fallback (top-5 by shared_techniques, all gates assumed TRUE)
    was permissive, not conservative. It reintroduced exactly the noise the
    filter exists to remove. Given "prefer false negatives," empty-result is safer.
    """
    # All candidates excluded with clear reason
    judgments = []
    for c in candidates:
        judgments.append(RelatedQraJudgment(
            key=c.get("_key", ""),
            include=False,
            shared_techniques=c.get("shared_techniques", []),
            checks=RelatedQraChecks(
                aids_user_query=False,
                addresses_same_concern=False,
                complements_not_duplicates=False,
                shares_technique_meaningfully=False,
                evidence_chain_overlap=False,
            ),
            reason="LLM validation failed; no safe inclusion.",
        ))

    return FilterRelatedQrasResult(
        relevant_keys=[],
        excluded_count=len(candidates),
        judgments=judgments,
    )


# ---------------------------------------------------------------------------
# Safe Wrapper
# ---------------------------------------------------------------------------

def filter_related_qras(
    query: str,
    primary_qra: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    max_retries: int = 2,
    timeout_s: float = 30.0,
    model: str = "deepseek-ai/DeepSeek-V3",
    use_fallback: bool = True,
) -> FilterRelatedQrasResult:
    """Safe wrapper that falls back to heuristic on LLM failure.

    Args:
        query: User's original query
        primary_qra: The primary matched QRA with lineage
        candidates: Candidate related QRAs from lineage.related_qra_keys
        max_retries: Number of validation retries
        timeout_s: LLM call timeout
        model: Model to use via scillm
        use_fallback: Whether to use heuristic fallback on failure

    Returns:
        FilterRelatedQrasResult with relevant_keys and judgments
    """
    if not candidates:
        return FilterRelatedQrasResult(
            relevant_keys=[],
            excluded_count=0,
            judgments=[],
        )

    try:
        return filter_related_qras_with_llm(
            query,
            primary_qra,
            candidates,
            max_retries=max_retries,
            timeout_s=timeout_s,
            model=model,
        )
    except Exception as exc:
        logger.error(
            f"filter_related_qras: LLM failed after retries: {exc}. "
            f"response_text={getattr(exc, 'response', {})}"
        )
        if use_fallback:
            logger.warning("filter_related_qras: using heuristic fallback")
            return fallback_filter_related_qras(candidates)
        raise


# ---------------------------------------------------------------------------
# CLI for Testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    # Example usage
    example_query = "How do I protect a spacecraft against credential stuffing attacks?"

    example_primary = {
        "_key": "qra_cwe287_ia0001_auth_bypass",
        "control_id": "CWE-287",
        "question": "How does improper authentication (CWE-287) enable unauthorized access to spacecraft command systems?",
        "answer": "Improper authentication allows attackers to bypass identity verification mechanisms, potentially gaining unauthorized access to spacecraft command and control systems.",
        "reasoning": "CWE-287 describes authentication bypass vulnerabilities. When applied to spacecraft systems, compromised authentication can lead to unauthorized command execution.",
        "lineage": {
            "entity_ids": ["CWE-287", "CAPEC-115", "T1078", "IA-0001", "IA-0002"],
            "entity_frameworks": ["CWE", "CAPEC", "ATT&CK", "SPARTA"],
            "graph_edge_count": 4,
            "reverse_edge_count": 35,
        }
    }

    example_candidates = [
        {
            "_key": "qra_cwe307_ia0001_brute_force",
            "control_id": "CWE-307",
            "question": "How does improper restriction of excessive authentication attempts (CWE-307) relate to spacecraft authentication security?",
            "answer": "CWE-307 describes systems that do not implement adequate controls against brute force attacks. For spacecraft, attackers could repeatedly attempt authentication without lockout.",
            "shared_techniques": ["IA-0001", "IA-0002"],
            "lineage": {
                "entity_ids": ["CWE-307", "CAPEC-49", "T1110", "IA-0001"],
                "entity_frameworks": ["CWE", "CAPEC", "ATT&CK", "SPARTA"],
            }
        },
        {
            "_key": "qra_cwe287_ia0002_session",
            "control_id": "CWE-287",
            "question": "What session management weaknesses are associated with CWE-287?",
            "answer": "Improper authentication (CWE-287) often co-occurs with session management weaknesses. Attackers who bypass initial authentication may exploit weak session tokens.",
            "shared_techniques": ["IA-0001", "IA-0002"],
            "lineage": {
                "entity_ids": ["CWE-287", "CWE-384", "CAPEC-115", "T1078", "IA-0002"],
                "entity_frameworks": ["CWE", "CAPEC", "ATT&CK", "SPARTA"],
            }
        },
        {
            "_key": "qra_cwe200_ia0001_disclosure",
            "control_id": "CWE-200",
            "question": "How does information exposure (CWE-200) relate to authentication systems?",
            "answer": "CWE-200 describes exposure of sensitive information. In authentication contexts, this may include exposure of password hashes or session tokens.",
            "shared_techniques": ["IA-0001"],
            "lineage": {
                "entity_ids": ["CWE-200", "CAPEC-118", "T1552", "IA-0001", "DE-0003"],
                "entity_frameworks": ["CWE", "CAPEC", "ATT&CK", "SPARTA"],
            }
        },
    ]

    if "--dry-run" in sys.argv:
        # Show the prompts
        user_prompt = build_filter_related_qras_user_prompt(
            example_query, example_primary, example_candidates
        )
        print("=== SYSTEM PROMPT ===")
        print(FILTER_RELATED_QRAS_SYSTEM_PROMPT)
        print("\n=== USER PROMPT (JSON) ===")
        print(user_prompt)
    else:
        # Actually call the LLM
        result = filter_related_qras(
            example_query,
            example_primary,
            example_candidates,
        )
        print(result.model_dump_json(indent=2))
