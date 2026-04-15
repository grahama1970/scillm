"""Tiered proof assessment for memory lessons.

Tier 1: Fast local classifier (regex patterns) - instant, free
Tier 2: DeepSeek Prover V2 assessment - ~500ms, ~$0.0002
Tier 3: Full proof via certainly-prover - background, uses Lean4

Usage:
    from graph_memory.integrations.proof_assessment import (
        likely_provable,      # Tier 1: fast local check
        assess_provability,   # Tier 2: DeepSeek Prover V2
        ProofQueue,           # Background proof processing
    )

    # Quick check (Tier 1)
    if likely_provable("For all n, n + 0 = n"):
        # Detailed assessment (Tier 2)
        result = await assess_provability("For all n, n + 0 = n")
        if result["provable"]:
            # Queue for background proof (Tier 3)
            await queue.push(lesson_key, result["lean4_sketch"], result["suggested_tactics"])
"""
from graph_memory.integrations.proof_assessment._tier1 import (
    PROVABLE_PATTERNS,
    NON_PROVABLE_PATTERNS,
    likely_provable,
    get_provability_score,
    hash_claim,
)
from graph_memory.integrations.proof_assessment._tier2 import (
    DEEPSEEK_PROVER_MODEL,
    ASSESSMENT_PROMPT,
    assess_provability,
    has_proof,
    get_proof_id,
    get_or_queue_proof,
    store_proof,
    link_proof_to_document,
    main,
)
from graph_memory.integrations.proof_assessment._tier3_queue import (
    ProofJob,
    ProofQueue,
    ArangoProofQueue,
    proof_worker,
)

__all__ = [
    # Tier 1: Local classifier
    "PROVABLE_PATTERNS",
    "NON_PROVABLE_PATTERNS",
    "likely_provable",
    "get_provability_score",
    "hash_claim",
    # Tier 2: Assessment + universal proof system
    "DEEPSEEK_PROVER_MODEL",
    "ASSESSMENT_PROMPT",
    "assess_provability",
    "has_proof",
    "get_proof_id",
    "get_or_queue_proof",
    "store_proof",
    "link_proof_to_document",
    "main",
    # Tier 3: Queue + worker
    "ProofJob",
    "ProofQueue",
    "ArangoProofQueue",
    "proof_worker",
]
