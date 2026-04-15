"""Validation gates for CAE tree integrity.

This module implements the structure and provenance gates that validate
CAE trees before they're returned to the user. Gates catch data quality
issues that would undermine confidence in the evidence case.

Structure Gates (verify data integrity):
- glossary_exists: All entity_ids resolve to glossary entries
- crosswalk_valid: All crosswalk endpoints exist in glossary
- lineage_complete: All QRAs have lineage.entity_ids populated
- no_circular_refs: No self-referential chains
- minimum_evidence: At least one normative control cited

Provenance Gates (verify data origins):
- source_version_pinned: All glossary entries have source_version
- mapping_reviewed: Ratio of crosswalks with review_status='approved'
- normative_vs_contextual: SPARTA controls are properly marked normative
"""

from __future__ import annotations

from .cae_models import StructureGates, ProvenanceGates


def check_structure_gates(
    glossary: list[dict],
    crosswalk_chains: list[dict],
    qras: list[dict]
) -> StructureGates:
    """Check structure validation gates.

    Args:
        glossary: List of glossary entry dicts with 'id' or '_key' fields
        crosswalk_chains: List of crosswalk chain dicts
        qras: List of QRA dicts with optional 'lineage' field

    Returns:
        StructureGates with all gate results and details
    """
    details: dict[str, str] = {}

    # Build glossary ID set for lookup
    glossary_ids = set()
    for entry in glossary:
        entry_id = entry.get("id") or entry.get("_key") or entry.get("entity_id")
        if entry_id:
            glossary_ids.add(entry_id)

    # 1. glossary_exists: All entity_ids in QRA lineage resolve to glossary
    missing_entities = []
    for qra in qras:
        lineage = qra.get("lineage") or {}
        entity_ids = lineage.get("entity_ids") or []
        for eid in entity_ids:
            if eid not in glossary_ids:
                missing_entities.append(eid)

    glossary_exists = len(missing_entities) == 0
    if not glossary_exists:
        unique_missing = list(set(missing_entities))[:5]  # Limit detail length
        details["glossary_exists"] = f"Missing entities: {unique_missing}"

    # 2. crosswalk_valid: All crosswalk endpoints exist in glossary
    invalid_crosswalks = []
    for chain in crosswalk_chains:
        source = chain.get("source") or chain.get("_from")
        target = chain.get("target") or chain.get("_to")
        # Extract ID from edge format if needed (e.g., "sparta_controls/IA-0006")
        if source and "/" in str(source):
            source = str(source).split("/")[-1]
        if target and "/" in str(target):
            target = str(target).split("/")[-1]

        if source and source not in glossary_ids:
            invalid_crosswalks.append(f"source:{source}")
        if target and target not in glossary_ids:
            invalid_crosswalks.append(f"target:{target}")

    crosswalk_valid = len(invalid_crosswalks) == 0
    if not crosswalk_valid:
        details["crosswalk_valid"] = f"Invalid endpoints: {invalid_crosswalks[:5]}"

    # 3. lineage_complete: All QRAs have lineage.entity_ids populated
    qras_without_lineage = []
    for qra in qras:
        lineage = qra.get("lineage") or {}
        entity_ids = lineage.get("entity_ids")
        if not entity_ids:
            key = qra.get("_key") or qra.get("key") or "unknown"
            qras_without_lineage.append(key)

    lineage_complete = len(qras_without_lineage) == 0
    if not lineage_complete:
        details["lineage_complete"] = f"QRAs missing lineage: {qras_without_lineage[:5]}"

    # 4. no_circular_refs: No self-referential chains
    # Check if any crosswalk source == target
    circular_refs = []
    for chain in crosswalk_chains:
        source = chain.get("source") or chain.get("_from")
        target = chain.get("target") or chain.get("_to")
        if source and target and source == target:
            circular_refs.append(source)

    no_circular_refs = len(circular_refs) == 0
    if not no_circular_refs:
        details["no_circular_refs"] = f"Circular refs: {circular_refs[:5]}"

    # 5. minimum_evidence: At least one normative control cited
    normative_count = sum(
        1 for entry in glossary
        if entry.get("authority_class") == "normative" or
           entry.get("framework") == "SPARTA"
    )
    minimum_evidence = normative_count > 0
    if not minimum_evidence:
        details["minimum_evidence"] = "No normative (SPARTA) controls in glossary"

    return StructureGates(
        glossary_exists=glossary_exists,
        crosswalk_valid=crosswalk_valid,
        lineage_complete=lineage_complete,
        no_circular_refs=no_circular_refs,
        minimum_evidence=minimum_evidence,
        details=details
    )


def check_provenance_gates(
    glossary: list[dict],
    crosswalk_chains: list[dict]
) -> ProvenanceGates:
    """Check provenance validation gates.

    Args:
        glossary: List of glossary entry dicts
        crosswalk_chains: List of crosswalk chain dicts

    Returns:
        ProvenanceGates with all gate results and details
    """
    details: dict[str, str] = {}

    # 1. source_version_pinned: All glossary entries have source_version
    entries_without_version = []
    for entry in glossary:
        if not entry.get("source_version"):
            entry_id = entry.get("id") or entry.get("_key") or "unknown"
            entries_without_version.append(entry_id)

    source_version_pinned = len(entries_without_version) == 0
    if not source_version_pinned:
        details["source_version_pinned"] = f"Missing version: {entries_without_version[:5]}"

    # 2. mapping_reviewed: Ratio of crosswalks with review_status='approved'
    # Consider >50% approved as passing
    approved_count = sum(
        1 for chain in crosswalk_chains
        if chain.get("review_status") == "approved"
    )
    total_crosswalks = len(crosswalk_chains)

    if total_crosswalks > 0:
        approval_ratio = approved_count / total_crosswalks
        mapping_reviewed = approval_ratio > 0.5
        details["mapping_reviewed"] = f"{approved_count}/{total_crosswalks} approved ({approval_ratio:.0%})"
    else:
        # No crosswalks = gate passes (nothing to review)
        mapping_reviewed = True

    # 3. normative_vs_contextual: SPARTA controls marked normative
    # Check if any SPARTA controls are incorrectly marked contextual
    mismarked = []
    for entry in glossary:
        framework = entry.get("framework") or entry.get("source_framework")
        authority = entry.get("authority_class")
        entry_id = entry.get("id") or entry.get("_key") or "unknown"

        if framework == "SPARTA" and authority == "contextual":
            mismarked.append(entry_id)
        elif framework in ("CWE", "CAPEC", "ATT&CK") and authority == "normative":
            mismarked.append(entry_id)

    normative_vs_contextual = len(mismarked) == 0
    if not normative_vs_contextual:
        details["normative_vs_contextual"] = f"Mismarked authority: {mismarked[:5]}"

    return ProvenanceGates(
        source_version_pinned=source_version_pinned,
        mapping_reviewed=mapping_reviewed,
        normative_vs_contextual=normative_vs_contextual,
        details=details
    )


def all_critical_gates_pass(
    structure_gates: StructureGates,
    provenance_gates: ProvenanceGates
) -> tuple[bool, list[str]]:
    """Check if all critical validation gates pass.

    Critical gates (must pass):
    - glossary_exists
    - minimum_evidence

    Important gates (should pass, but not blocking):
    - lineage_complete
    - no_circular_refs
    - source_version_pinned
    - normative_vs_contextual

    Args:
        structure_gates: Results from check_structure_gates
        provenance_gates: Results from check_provenance_gates

    Returns:
        Tuple of (all_critical_pass, list of warning messages)
    """
    warnings = []

    # Critical gates
    critical_pass = True
    if not structure_gates.glossary_exists:
        critical_pass = False
        warnings.append("Critical: Some entity IDs do not resolve to glossary entries")
    if not structure_gates.minimum_evidence:
        critical_pass = False
        warnings.append("Critical: No normative (SPARTA) controls cited")

    # Important but non-blocking gates
    if not structure_gates.lineage_complete:
        warnings.append("Warning: Some QRAs are missing lineage data")
    if not structure_gates.no_circular_refs:
        warnings.append("Warning: Circular references detected in crosswalk chains")
    if not provenance_gates.source_version_pinned:
        warnings.append("Warning: Some glossary entries are missing source_version")
    if not provenance_gates.normative_vs_contextual:
        warnings.append("Warning: Some entries have incorrect authority_class")

    return critical_pass, warnings


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Running validation gate tests...")

    # Test data
    glossary = [
        {"id": "IA-0006", "framework": "SPARTA", "authority_class": "normative", "source_version": "2025.1"},
        {"id": "CWE-924", "framework": "CWE", "authority_class": "contextual", "source_version": "4.14"},
    ]
    crosswalks = [
        {"source": "CWE-924", "target": "IA-0006", "review_status": "approved"},
    ]
    qras = [
        {"_key": "qra_001", "lineage": {"entity_ids": ["IA-0006", "CWE-924"]}},
    ]

    # Test structure gates - all should pass
    sg = check_structure_gates(glossary, crosswalks, qras)
    assert sg.glossary_exists == True, f"glossary_exists should be True: {sg.details}"
    assert sg.crosswalk_valid == True, f"crosswalk_valid should be True: {sg.details}"
    assert sg.lineage_complete == True, f"lineage_complete should be True: {sg.details}"
    assert sg.no_circular_refs == True, f"no_circular_refs should be True: {sg.details}"
    assert sg.minimum_evidence == True, f"minimum_evidence should be True: {sg.details}"

    # Test provenance gates - all should pass
    pg = check_provenance_gates(glossary, crosswalks)
    assert pg.source_version_pinned == True
    assert pg.mapping_reviewed == True
    assert pg.normative_vs_contextual == True

    # Test failure cases
    bad_qras = [
        {"_key": "qra_002", "lineage": {"entity_ids": ["IA-9999"]}},  # Missing entity
    ]
    sg_bad = check_structure_gates(glossary, crosswalks, bad_qras)
    assert sg_bad.glossary_exists == False

    # Test with missing lineage
    qras_no_lineage = [{"_key": "qra_003"}]  # No lineage
    sg_no_lineage = check_structure_gates(glossary, crosswalks, qras_no_lineage)
    assert sg_no_lineage.lineage_complete == False

    # Test with no normative controls
    contextual_only = [
        {"id": "CWE-924", "framework": "CWE", "authority_class": "contextual"},
    ]
    sg_contextual = check_structure_gates(contextual_only, crosswalks, qras_no_lineage)
    assert sg_contextual.minimum_evidence == False

    print("All validation gate tests PASSED")
