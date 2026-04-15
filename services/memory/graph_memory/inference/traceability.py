"""Traceability strength calculation for CAE arguments.

Calculates how confident we should be in evidence chains based on:
- Number of QRAs that reference the control
- Type of crosswalk (direct vs indirect)
- Whether all validation gates pass

Traceability thresholds (from COMPLETE_EXAMPLE.md):
- STRONG: 2+ QRAs AND direct crosswalk AND all gates pass
- MODERATE: (1+ QRA OR direct crosswalk) AND all gates pass
- WEAK: supplementary crosswalk only OR (0 QRAs AND indirect crosswalk)
- INSUFFICIENT: no normative controls found (fails minimum_evidence gate)
"""

from __future__ import annotations

from .cae_models import TraceabilityStrength, StructureGates


def calculate_traceability_strength(
    qra_count: int,
    crosswalk_type: str | None,
    all_gates_pass: bool
) -> TraceabilityStrength:
    """Calculate traceability strength from basic metrics.

    Args:
        qra_count: Number of QRAs that reference this control
        crosswalk_type: "direct", "indirect", "supplementary", or None
        all_gates_pass: Whether all validation gates passed

    Returns:
        TraceabilityStrength enum value
    """
    # If gates don't pass, we can't be confident
    if not all_gates_pass:
        # Still differentiate between having some evidence vs none
        if qra_count == 0 and crosswalk_type is None:
            return TraceabilityStrength.INSUFFICIENT
        return TraceabilityStrength.WEAK

    # STRONG: 2+ QRAs AND direct crosswalk
    if qra_count >= 2 and crosswalk_type == "direct":
        return TraceabilityStrength.STRONG

    # MODERATE: 1+ QRA OR direct crosswalk (with gates passing)
    if qra_count >= 1 or crosswalk_type == "direct":
        return TraceabilityStrength.MODERATE

    # WEAK: supplementary only OR (0 QRAs AND indirect)
    if crosswalk_type in ("supplementary", "indirect"):
        return TraceabilityStrength.WEAK

    # No evidence at all
    return TraceabilityStrength.INSUFFICIENT


def calculate_argument_traceability(
    argument_control_id: str,
    qras_for_control: list[dict],
    crosswalk_chain: dict | None,
    structure_gates: StructureGates
) -> tuple[TraceabilityStrength, str | None]:
    """Calculate traceability for a specific argument.

    This is the higher-level function that takes actual QRA and crosswalk
    data and computes the traceability strength.

    Args:
        argument_control_id: The SPARTA control ID (e.g., "IA-0006")
        qras_for_control: List of QRAs that reference this control
        crosswalk_chain: Crosswalk data if a chain exists, None otherwise
        structure_gates: The structure gates results

    Returns:
        Tuple of (TraceabilityStrength, warning_message or None)
    """
    qra_count = len(qras_for_control)

    # Determine crosswalk type
    crosswalk_type = None
    if crosswalk_chain:
        # Extract crosswalk type from the chain
        # Crosswalk chains have a 'strength' or 'type' field
        crosswalk_type = crosswalk_chain.get("type") or crosswalk_chain.get("strength")
        if crosswalk_type and crosswalk_type.lower() in ("strong", "primary"):
            crosswalk_type = "direct"
        elif crosswalk_type and crosswalk_type.lower() in ("medium", "secondary"):
            crosswalk_type = "indirect"
        elif crosswalk_type:
            crosswalk_type = "supplementary"

    # Check if all critical gates pass
    all_gates_pass = (
        structure_gates.glossary_exists and
        structure_gates.minimum_evidence and
        structure_gates.lineage_complete
    )

    strength = calculate_traceability_strength(qra_count, crosswalk_type, all_gates_pass)

    # Generate warning message for weak traceability
    warning = None
    if strength == TraceabilityStrength.WEAK:
        reasons = []
        if qra_count == 0:
            reasons.append("no QRAs reference this control")
        if crosswalk_type in ("supplementary", None):
            reasons.append(f"crosswalk is {crosswalk_type or 'missing'}")
        if not all_gates_pass:
            reasons.append("validation gates did not all pass")
        warning = f"Weak traceability for {argument_control_id}: {'; '.join(reasons)}"
    elif strength == TraceabilityStrength.INSUFFICIENT:
        warning = f"Insufficient evidence for {argument_control_id}: no normative controls linked"

    return strength, warning


def aggregate_traceability(argument_strengths: list[TraceabilityStrength]) -> TraceabilityStrength:
    """Aggregate multiple argument traceabilities into overall claim traceability.

    Logic:
    - STRONG if majority of arguments are STRONG
    - MODERATE if any STRONG or majority MODERATE
    - WEAK if no STRONG/MODERATE
    - INSUFFICIENT if no arguments or all INSUFFICIENT

    Args:
        argument_strengths: List of TraceabilityStrength values from arguments

    Returns:
        Aggregated TraceabilityStrength for the claim
    """
    if not argument_strengths:
        return TraceabilityStrength.INSUFFICIENT

    counts = {
        TraceabilityStrength.STRONG: 0,
        TraceabilityStrength.MODERATE: 0,
        TraceabilityStrength.WEAK: 0,
        TraceabilityStrength.INSUFFICIENT: 0,
    }

    for s in argument_strengths:
        counts[s] += 1

    total = len(argument_strengths)

    # All insufficient
    if counts[TraceabilityStrength.INSUFFICIENT] == total:
        return TraceabilityStrength.INSUFFICIENT

    # Majority strong
    if counts[TraceabilityStrength.STRONG] > total / 2:
        return TraceabilityStrength.STRONG

    # Any strong or majority moderate
    if counts[TraceabilityStrength.STRONG] > 0 or counts[TraceabilityStrength.MODERATE] > total / 2:
        return TraceabilityStrength.MODERATE

    # At least some moderate
    if counts[TraceabilityStrength.MODERATE] > 0:
        return TraceabilityStrength.MODERATE

    # Only weak or insufficient
    return TraceabilityStrength.WEAK


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Running traceability calculation tests...")

    # Test basic calculation
    assert calculate_traceability_strength(2, "direct", True) == TraceabilityStrength.STRONG
    assert calculate_traceability_strength(1, "direct", True) == TraceabilityStrength.MODERATE
    assert calculate_traceability_strength(3, "indirect", True) == TraceabilityStrength.MODERATE
    assert calculate_traceability_strength(0, "direct", True) == TraceabilityStrength.MODERATE
    assert calculate_traceability_strength(0, "supplementary", True) == TraceabilityStrength.WEAK
    assert calculate_traceability_strength(0, "indirect", True) == TraceabilityStrength.WEAK
    assert calculate_traceability_strength(0, None, True) == TraceabilityStrength.INSUFFICIENT
    assert calculate_traceability_strength(2, "direct", False) == TraceabilityStrength.WEAK

    # Test aggregation
    assert aggregate_traceability([]) == TraceabilityStrength.INSUFFICIENT
    assert aggregate_traceability([
        TraceabilityStrength.STRONG,
        TraceabilityStrength.STRONG,
        TraceabilityStrength.MODERATE
    ]) == TraceabilityStrength.STRONG
    assert aggregate_traceability([
        TraceabilityStrength.MODERATE,
        TraceabilityStrength.MODERATE,
        TraceabilityStrength.WEAK
    ]) == TraceabilityStrength.MODERATE
    assert aggregate_traceability([
        TraceabilityStrength.WEAK,
        TraceabilityStrength.WEAK
    ]) == TraceabilityStrength.WEAK

    print("All traceability tests PASSED")
