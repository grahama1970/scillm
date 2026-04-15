"""CAE (Claims-Arguments-Evidence) tree models for evidence case building.

This module defines the Pydantic models for the CAE tree structure used by
the /build-evidence-case endpoint. CAE trees help compliance officers find
and verify requirements with clear traceability.

Architecture (from COMPLETE_EXAMPLE.md):
  CLAIM (What needs to be verified)
      |
      +-- ARGUMENT (Why we believe the claim)
      |       |
      |       +-- EVIDENCE (SPARTA controls, contextual entities)
      |       +-- EVIDENCE
      |
      +-- ARGUMENT
              |
              +-- EVIDENCE

      FOUND VIA: QRAs (inference layer - how we located the evidence)

Key distinction:
  - SPARTA controls = normative evidence (obligations to verify against)
  - CWE/CAPEC/ATT&CK = contextual evidence (supporting understanding)
  - QRAs = inference layer (retrieval mechanism, NOT evidence)
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Traceability Strength
# ---------------------------------------------------------------------------

class TraceabilityStrength(str, Enum):
    """Traceability strength for CAE arguments.

    Used to indicate how confident we are in the evidence chain:
    - STRONG: 2+ QRAs AND direct crosswalk AND all gates pass
    - MODERATE: (1+ QRA OR direct crosswalk) AND all gates pass
    - WEAK: supplementary crosswalk only OR (0 QRAs AND indirect crosswalk)
    - INSUFFICIENT: no normative controls found (fails minimum_evidence gate)
    """
    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"
    INSUFFICIENT = "INSUFFICIENT"


# ---------------------------------------------------------------------------
# Evidence Models
# ---------------------------------------------------------------------------

class CAEEvidence(BaseModel):
    """A single piece of evidence in a CAE argument.

    Evidence can be either normative (SPARTA controls - obligations) or
    contextual (CWE/CAPEC/ATT&CK - supporting understanding).
    """
    entity_id: str = Field(
        description="Entity identifier (e.g., 'IA-0006', 'CWE-924')"
    )
    framework: str = Field(
        description="Source framework (SPARTA, CWE, CAPEC, ATT&CK)"
    )
    authority_class: Literal["normative", "contextual"] = Field(
        description="normative = requirements to verify; contextual = supporting info"
    )
    description: str = Field(
        description="Human-readable description from the glossary"
    )
    source_version: str | None = Field(
        default=None,
        description="Version of the source framework (e.g., '2025.1')"
    )


class CAEFoundVia(BaseModel):
    """A QRA that was used to locate evidence (inference layer).

    QRAs are NOT evidence themselves - they're the retrieval mechanism
    that connects user questions to authoritative requirements.
    """
    qra_key: str = Field(description="The QRA _key from ArangoDB")
    question: str = Field(description="The QRA question that matched")
    entity_ids: list[str] = Field(
        default_factory=list,
        description="Entities referenced by this QRA (from lineage.entity_ids)"
    )


# ---------------------------------------------------------------------------
# Argument Model
# ---------------------------------------------------------------------------

class CAEArgument(BaseModel):
    """An argument in a CAE tree.

    Each argument represents a reasoning chain from evidence to claim,
    anchored by a primary SPARTA control.
    """
    control_id: str = Field(
        description="Primary SPARTA control ID (e.g., 'IA-0006')"
    )
    reasoning: str = Field(
        description="Human-readable argument connecting evidence to claim"
    )
    evidence: list[CAEEvidence] = Field(
        default_factory=list,
        description="Evidence items supporting this argument"
    )
    found_via: list[CAEFoundVia] = Field(
        default_factory=list,
        description="QRAs that located this evidence (inference layer)"
    )
    traceability: TraceabilityStrength = Field(
        description="Strength of the traceability chain"
    )
    crosswalk_type: Literal["direct", "indirect", "supplementary"] | None = Field(
        default=None,
        description="Type of crosswalk linking contextual evidence to this control"
    )
    warning: str | None = Field(
        default=None,
        description="Warning message if traceability is weak"
    )


# ---------------------------------------------------------------------------
# Validation Gates
# ---------------------------------------------------------------------------

class StructureGates(BaseModel):
    """Structure validation gates for CAE trees.

    These gates verify the structural integrity of the evidence case:
    - glossary_exists: All entity_ids resolve to glossary entries
    - crosswalk_valid: All crosswalk endpoints exist in glossary
    - lineage_complete: All QRAs have lineage.entity_ids populated
    - no_circular_refs: No self-referential chains
    - minimum_evidence: At least one normative control cited
    """
    glossary_exists: bool = Field(
        description="All entity_ids in QRA lineage resolve to glossary entries"
    )
    crosswalk_valid: bool = Field(
        description="All crosswalk endpoints exist in glossary"
    )
    lineage_complete: bool = Field(
        description="All QRAs have lineage.entity_ids populated"
    )
    no_circular_refs: bool = Field(
        description="No self-referential chains in the evidence graph"
    )
    minimum_evidence: bool = Field(
        description="At least one normative control (SPARTA) cited"
    )
    details: dict[str, str] = Field(
        default_factory=dict,
        description="Per-gate detail messages for failures"
    )


class ProvenanceGates(BaseModel):
    """Provenance validation gates for CAE trees.

    These gates verify the provenance (origin and review status) of evidence:
    - source_version_pinned: All glossary entries have source_version
    - mapping_reviewed: Ratio of crosswalks with review_status='approved'
    - normative_vs_contextual: SPARTA controls are properly marked normative
    """
    source_version_pinned: bool = Field(
        description="All glossary entries have source_version populated"
    )
    mapping_reviewed: bool = Field(
        description="Crosswalks have been reviewed (>50% with approved status)"
    )
    normative_vs_contextual: bool = Field(
        description="SPARTA controls are properly marked as normative authority"
    )
    details: dict[str, str] = Field(
        default_factory=dict,
        description="Per-gate detail messages for failures"
    )


# ---------------------------------------------------------------------------
# Claim Model
# ---------------------------------------------------------------------------

class CAEClaim(BaseModel):
    """The top-level claim in a CAE tree.

    Claims are always NEEDS_VERIFICATION - the tool surfaces requirements,
    but compliance determination is the human's job.
    """
    claim_text: str = Field(
        description="The claim statement derived from the user's question"
    )
    status: Literal["NEEDS_VERIFICATION"] = Field(
        default="NEEDS_VERIFICATION",
        description="Always NEEDS_VERIFICATION - tool doesn't determine compliance"
    )
    traceability: TraceabilityStrength = Field(
        description="Overall traceability strength (aggregated from arguments)"
    )
    arguments: list[CAEArgument] = Field(
        default_factory=list,
        description="Arguments supporting this claim"
    )
    structure_gates: StructureGates = Field(
        description="Structure validation results"
    )
    provenance_gates: ProvenanceGates = Field(
        description="Provenance validation results"
    )
    validation_passed: bool = Field(
        description="True if all critical gates passed"
    )
    validation_warnings: list[str] = Field(
        default_factory=list,
        description="Non-blocking warnings from validation"
    )


# ---------------------------------------------------------------------------
# Response Model
# ---------------------------------------------------------------------------

class CAETreeResponse(BaseModel):
    """Complete CAE tree response from /build-evidence-case.

    Contains the claim tree and summary statistics.
    """
    claim: CAEClaim = Field(description="The root claim with its argument tree")
    summary: dict = Field(
        default_factory=dict,
        description="Summary counts: arguments, evidence_normative, evidence_contextual, qras_used"
    )

    @classmethod
    def empty(cls, question: str, reason: str) -> "CAETreeResponse":
        """Create an empty CAE tree response for failure cases."""
        return cls(
            claim=CAEClaim(
                claim_text=f"Unable to build evidence case: {reason}",
                traceability=TraceabilityStrength.INSUFFICIENT,
                arguments=[],
                structure_gates=StructureGates(
                    glossary_exists=False,
                    crosswalk_valid=False,
                    lineage_complete=False,
                    no_circular_refs=True,
                    minimum_evidence=False,
                    details={"error": reason}
                ),
                provenance_gates=ProvenanceGates(
                    source_version_pinned=False,
                    mapping_reviewed=False,
                    normative_vs_contextual=False,
                    details={"error": reason}
                ),
                validation_passed=False,
                validation_warnings=[reason]
            ),
            summary={
                "arguments": 0,
                "evidence_normative": 0,
                "evidence_contextual": 0,
                "qras_used": 0,
                "original_question": question
            }
        )
