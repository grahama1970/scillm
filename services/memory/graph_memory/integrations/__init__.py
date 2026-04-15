"""Integration shims for external pipelines (Extractor, Lean4, etc.).

These modules are intentionally lightweight and IO-focused so that
existing systems can call into Graph Memory without adopting internal
APIs. Keep dependencies minimal and avoid side effects on import.
"""

from graph_memory.integrations.proof_assessment import (
    # Tier 1: Local classifier
    likely_provable,
    get_provability_score,
    # Tier 2: DeepSeek assessment
    assess_provability,
    # Universal proof system
    hash_claim,
    has_proof,
    get_proof_id,
    get_or_queue_proof,
    store_proof,
    link_proof_to_document,
    # Legacy queue classes (prefer universal functions)
    ProofQueue,
    ProofJob,
    ArangoProofQueue,
    proof_worker,
)

__all__ = [
    # Tier 1
    "likely_provable",
    "get_provability_score",
    # Tier 2
    "assess_provability",
    # Universal proof system
    "hash_claim",
    "has_proof",
    "get_proof_id",
    "get_or_queue_proof",
    "store_proof",
    "link_proof_to_document",
    # Legacy
    "ProofQueue",
    "ProofJob",
    "ArangoProofQueue",
    "proof_worker",
]

