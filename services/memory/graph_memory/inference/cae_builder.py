"""CAE tree builder for /build-evidence-case endpoint.

This module transforms raw evidence case data (glossary, crosswalks, QRAs)
into a structured CAE (Claims-Arguments-Evidence) tree with traceability
strength and validation gates.

The builder performs these steps:
1. Generate claim text from the user's question
2. Group evidence by SPARTA control (build arguments)
3. Calculate traceability per argument
4. Run validation gates
5. Aggregate overall traceability
6. Build summary counts
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .cae_models import (
    CAEArgument,
    CAEClaim,
    CAEEvidence,
    CAEFoundVia,
    CAETreeResponse,
    TraceabilityStrength,
)
from .traceability import (
    aggregate_traceability,
    calculate_argument_traceability,
)
from .validation_gates import (
    all_critical_gates_pass,
    check_provenance_gates,
    check_structure_gates,
)


def _generate_claim_text(question: str, glossary: list[dict]) -> str:
    """Generate a conservative claim text from the user's question.

    Uses templates to avoid overstating what we're claiming:
    - Discovery: "[Topic] requirements are identified"
    - Targeted (if control_id in question): "[Control] requirements regarding [topic] are documented"

    Args:
        question: The user's original question
        glossary: Glossary entries to check for control IDs

    Returns:
        Conservative claim text
    """
    # Check if question references a specific control ID
    glossary_ids = {
        entry.get("id") or entry.get("_key") or entry.get("entity_id")
        for entry in glossary
        if entry.get("framework") == "SPARTA" or entry.get("authority_class") == "normative"
    }

    # Look for control ID pattern in question (e.g., IA-0006, CM-0001)
    control_pattern = r'\b([A-Z]{2}-\d{4})\b'
    matches = re.findall(control_pattern, question.upper())
    referenced_control = None
    for match in matches:
        if match in glossary_ids:
            referenced_control = match
            break

    # Extract topic from question (remove common question words)
    topic = question.lower()
    for word in ["how do i", "how to", "what", "which", "why", "when", "where"]:
        topic = topic.replace(word, "")
    topic = topic.strip().strip("?").strip()

    # Limit topic length
    if len(topic) > 60:
        topic = topic[:60].rsplit(" ", 1)[0] + "..."

    if referenced_control:
        return f"{referenced_control} requirements regarding {topic} are documented"
    else:
        # Capitalize first letter
        topic = topic[0].upper() + topic[1:] if topic else "Requirements"
        return f"{topic} requirements are identified"


def _group_evidence_by_control(
    glossary: list[dict],
    crosswalk_chains: list[dict],
    qras: list[dict]
) -> dict[str, dict]:
    """Group glossary entries and QRAs by their primary SPARTA control.

    Args:
        glossary: List of glossary entries
        crosswalk_chains: List of crosswalk chains
        qras: List of QRAs

    Returns:
        Dict mapping control_id -> {
            "control": dict,
            "contextual_evidence": list[dict],
            "crosswalks": list[dict],
            "qras": list[dict]
        }
    """
    groups: dict[str, dict] = {}

    # First, identify all SPARTA controls
    sparta_controls = {}
    for entry in glossary:
        if entry.get("framework") == "SPARTA" or entry.get("authority_class") == "normative":
            control_id = entry.get("id") or entry.get("_key") or entry.get("entity_id")
            if control_id:
                sparta_controls[control_id] = entry
                groups[control_id] = {
                    "control": entry,
                    "contextual_evidence": [],
                    "crosswalks": [],
                    "qras": [],
                }

    # If no SPARTA controls, return empty
    if not sparta_controls:
        return groups

    # Map contextual evidence via crosswalks
    for chain in crosswalk_chains:
        target = chain.get("target") or chain.get("_to") or ""
        if "/" in str(target):
            target = str(target).split("/")[-1]

        if target in groups:
            groups[target]["crosswalks"].append(chain)

            # Find the source entity and add as contextual evidence
            source = chain.get("source") or chain.get("_from") or ""
            if "/" in str(source):
                source = str(source).split("/")[-1]

            for entry in glossary:
                entry_id = entry.get("id") or entry.get("_key") or entry.get("entity_id")
                if entry_id == source:
                    groups[target]["contextual_evidence"].append(entry)
                    break

    # Map QRAs to controls via lineage.entity_ids
    for qra in qras:
        lineage = qra.get("lineage") or {}
        entity_ids = lineage.get("entity_ids") or []

        for eid in entity_ids:
            if eid in groups:
                groups[eid]["qras"].append(qra)

    return groups


def build_cae_tree(
    question: str,
    glossary: list[dict],
    crosswalk_chains: list[dict],
    prior_qra_evidence: list[dict],
    related_qra_evidence: list[dict],
    shared_techniques_summary: dict[str, list[str]]
) -> CAETreeResponse:
    """Build a complete CAE tree from evidence case data.

    Args:
        question: The user's original question
        glossary: List of glossary entries (SPARTA controls, CWE, etc.)
        crosswalk_chains: List of crosswalk chains between frameworks
        prior_qra_evidence: QRAs that directly matched the question
        related_qra_evidence: QRAs found via lineage.related_qra_keys
        shared_techniques_summary: Map of technique_id -> [qra_keys]

    Returns:
        Complete CAETreeResponse with claim, arguments, evidence, and validation
    """
    # Combine all QRAs
    all_qras = prior_qra_evidence + related_qra_evidence

    # Handle empty case
    if not glossary:
        return CAETreeResponse.empty(question, "No glossary entries found")

    # Run validation gates
    structure_gates = check_structure_gates(glossary, crosswalk_chains, all_qras)
    provenance_gates = check_provenance_gates(glossary, crosswalk_chains)
    validation_passed, warnings = all_critical_gates_pass(structure_gates, provenance_gates)

    # If minimum_evidence fails, return insufficient
    if not structure_gates.minimum_evidence:
        return CAETreeResponse.empty(question, "No normative (SPARTA) controls in evidence")

    # Generate claim text
    claim_text = _generate_claim_text(question, glossary)

    # Group evidence by SPARTA control
    control_groups = _group_evidence_by_control(glossary, crosswalk_chains, all_qras)

    # Build arguments for each control
    arguments: list[CAEArgument] = []
    argument_strengths: list[TraceabilityStrength] = []

    for control_id, group in control_groups.items():
        control = group["control"]
        contextual = group["contextual_evidence"]
        crosswalks = group["crosswalks"]
        qras_for_control = group["qras"]

        # Build evidence list (control + contextual)
        evidence_items: list[CAEEvidence] = []

        # Add the SPARTA control as primary evidence
        evidence_items.append(CAEEvidence(
            entity_id=control_id,
            framework=control.get("framework", "SPARTA"),
            authority_class="normative",
            description=control.get("description") or control.get("title") or "",
            source_version=control.get("source_version"),
        ))

        # Add contextual evidence
        for ctx in contextual:
            ctx_id = ctx.get("id") or ctx.get("_key") or ctx.get("entity_id")
            evidence_items.append(CAEEvidence(
                entity_id=ctx_id or "unknown",
                framework=ctx.get("framework", "CWE"),
                authority_class="contextual",
                description=ctx.get("description") or ctx.get("title") or "",
                source_version=ctx.get("source_version"),
            ))

        # Build found_via list from QRAs
        found_via: list[CAEFoundVia] = []
        for qra in qras_for_control:
            qra_key = qra.get("_key") or qra.get("key") or "unknown"
            lineage = qra.get("lineage") or {}
            found_via.append(CAEFoundVia(
                qra_key=qra_key,
                question=qra.get("question") or "",
                entity_ids=lineage.get("entity_ids") or [],
            ))

        # Determine crosswalk type
        crosswalk_chain = crosswalks[0] if crosswalks else None

        # Calculate traceability
        traceability, warning = calculate_argument_traceability(
            control_id,
            qras_for_control,
            crosswalk_chain,
            structure_gates,
        )
        argument_strengths.append(traceability)

        # Determine crosswalk type string
        crosswalk_type = None
        if crosswalk_chain:
            chain_type = crosswalk_chain.get("type") or crosswalk_chain.get("strength")
            if chain_type and chain_type.lower() in ("strong", "primary", "direct"):
                crosswalk_type = "direct"
            elif chain_type and chain_type.lower() in ("medium", "secondary"):
                crosswalk_type = "indirect"
            elif chain_type:
                crosswalk_type = "supplementary"

        # Build reasoning text
        reasoning = f"Per {control_id}, "
        if contextual:
            ctx_ids = [c.get("id") or c.get("_key") for c in contextual[:3]]
            reasoning += f"which addresses {', '.join(filter(None, ctx_ids))}, "
        reasoning += f"this requirement applies to the user's query."

        arguments.append(CAEArgument(
            control_id=control_id,
            reasoning=reasoning,
            evidence=evidence_items,
            found_via=found_via,
            traceability=traceability,
            crosswalk_type=crosswalk_type,
            warning=warning,
        ))

    # Aggregate overall traceability
    overall_traceability = aggregate_traceability(argument_strengths)

    # Build claim
    claim = CAEClaim(
        claim_text=claim_text,
        status="NEEDS_VERIFICATION",
        traceability=overall_traceability,
        arguments=arguments,
        structure_gates=structure_gates,
        provenance_gates=provenance_gates,
        validation_passed=validation_passed,
        validation_warnings=warnings,
    )

    # Build summary
    normative_count = sum(
        1 for arg in arguments
        for ev in arg.evidence
        if ev.authority_class == "normative"
    )
    contextual_count = sum(
        1 for arg in arguments
        for ev in arg.evidence
        if ev.authority_class == "contextual"
    )
    qras_used = len(set(
        fv.qra_key for arg in arguments for fv in arg.found_via
    ))

    summary = {
        "arguments": len(arguments),
        "evidence_normative": normative_count,
        "evidence_contextual": contextual_count,
        "qras_used": qras_used,
        "original_question": question,
    }

    return CAETreeResponse(claim=claim, summary=summary)
