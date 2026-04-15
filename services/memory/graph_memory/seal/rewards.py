"""Reward scaffold for future GRPO/RLHF.

Currently unused; keep as optional helpers when wiring reward-based training.
Signals (suggested):
- r_cite: fraction of rationale tokens backed by retrieved lessons/edges.
- r_path: AQL path exists between cited nodes.
- r_schema: JSON/plan validates against schema.
- r_abstain: bonus for safe refusal when context is weak.

All rewards should treat Arango as the authority; do not bypass graph validation.
"""
from __future__ import annotations
from typing import Dict, Any


def compute_rewards(sample: Dict[str, Any], *, cited_ok: bool = True, path_ok: bool = True, schema_ok: bool = True, abstained: bool = False) -> Dict[str, float]:
    # Simple placeholder; real implementation should compute from outputs + graph checks.
    rewards = {
        "r_cite": 1.0 if cited_ok else 0.0,
        "r_path": 1.0 if path_ok else 0.0,
        "r_schema": 1.0 if schema_ok else 0.0,
        "r_abstain": 0.5 if abstained else 0.0,
    }
    # Weighted sum suggestion (adjust upstream):
    rewards["r_total"] = 0.4 * rewards["r_path"] + 0.25 * rewards["r_cite"] + 0.2 * rewards["r_schema"] + rewards["r_abstain"]
    return rewards
