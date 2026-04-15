"""Tier 1: Fast local classifier for provability.

Uses regex patterns to quickly filter out ~90% of non-provable claims.
Instant, free, no external dependencies.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict

from loguru import logger

# Patterns that indicate a claim might be provable in Lean4
PROVABLE_PATTERNS = [
    # Universal/existential quantifiers
    (r'\bfor\s+all\b', 2),
    (r'\bforall\b', 2),
    (r'\bfor\s+any\b', 2),
    (r'\bfor\s+every\b', 2),
    (r'\bthere\s+exists\b', 2),
    (r'∀', 3),
    (r'∃', 3),

    # Implications and logical connectives
    (r'\bif\s+.+\s+then\b', 2),
    (r'\bimplies\b', 2),
    (r'\biff\b', 2),
    (r'\band\b.*\bor\b|\bor\b.*\band\b', 1),
    (r'→|⟹|⇒', 3),
    (r'↔|⟺|⇔', 3),

    # Mathematical operators and comparisons
    (r'[<>=≤≥≠]+', 1),
    (r'\+|\-|\*|\/|×|÷', 1),
    (r'\bmod\b|\brem\b', 1),

    # Proof-related keywords
    (r'\bprove\b', 3),
    (r'\btheorem\b', 3),
    (r'\blemma\b', 3),
    (r'\bproposition\b', 2),
    (r'\bcorollary\b', 2),

    # Invariants and properties
    (r'\bmust\s+be\b', 1),
    (r'\balways\b', 1),
    (r'\bnever\b', 1),
    (r'\binvariant\b', 2),
    (r'\bproperty\b', 1),

    # Mathematical terms
    (r'\bnatural\s+number', 2),
    (r'\binteger\b', 1),
    (r'\barray\b.*\bindex\b|\bindex\b.*\barray\b', 2),
    (r'\bbound\b', 1),
    (r'\bsize\b.*\blength\b|\blength\b.*\bsize\b', 1),

    # Common proof patterns
    (r'\bn\s*\+\s*0\s*=\s*n', 3),
    (r'\b0\s*\+\s*n\s*=\s*n', 3),
    (r'\ba\s*\+\s*b\s*=\s*b\s*\+\s*a', 3),  # commutativity
]

# Patterns that indicate a claim is NOT provable (heuristics, procedures)
NON_PROVABLE_PATTERNS = [
    (r'\brestart\b', -2),
    (r'\btry\b', -1),
    (r'\bclick\b', -2),
    (r'\bbutton\b', -1),
    (r'\binstall\b', -1),
    (r'\bdownload\b', -1),
    (r'\bconfigure\b', -1),
    (r'\bset\s+up\b', -1),
    (r'\brun\b.*\bcommand\b', -1),
    (r'\bopen\b.*\bfile\b', -1),
    (r'\busually\b', -2),
    (r'\bsometimes\b', -2),
    (r'\bprobably\b', -2),
    (r'\bmight\b', -1),
    (r'\bcould\b', -1),
    (r'\bshould\b', -1),  # weak - "should" can be normative
]


def likely_provable(text: str, threshold: float = 0.03) -> bool:
    """Tier 1: Fast local check - is this text likely a provable claim?

    Uses regex patterns to quickly filter out ~90% of non-provable claims.
    Returns True if the text has enough provable indicators.

    Args:
        text: The claim to assess
        threshold: Minimum score ratio (0.0-1.0) to consider provable

    Returns:
        True if likely provable, False otherwise
    """
    if not text or len(text) < 10:
        return False

    text_lower = text.lower()

    # Calculate positive score
    pos_score = 0
    max_pos = 0
    for pattern, weight in PROVABLE_PATTERNS:
        max_pos += weight
        if re.search(pattern, text_lower, re.IGNORECASE):
            pos_score += weight

    # Calculate negative score
    neg_score = 0
    for pattern, weight in NON_PROVABLE_PATTERNS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            neg_score += abs(weight)

    # Net score
    net_score = pos_score - neg_score

    # Normalize to 0-1 range
    if max_pos == 0:
        return False

    ratio = net_score / max_pos
    return ratio >= threshold


def get_provability_score(text: str) -> Dict[str, Any]:
    """Get detailed provability score breakdown.

    Args:
        text: The claim to assess

    Returns:
        Dict with score breakdown and matched patterns
    """
    if not text:
        return {"score": 0, "likely_provable": False, "matched": [], "anti_matched": []}

    text_lower = text.lower()

    matched = []
    anti_matched = []
    pos_score = 0
    neg_score = 0

    for pattern, weight in PROVABLE_PATTERNS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            pos_score += weight
            matched.append(pattern)

    for pattern, weight in NON_PROVABLE_PATTERNS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            neg_score += abs(weight)
            anti_matched.append(pattern)

    net_score = pos_score - neg_score
    max_pos = sum(w for _, w in PROVABLE_PATTERNS)
    ratio = net_score / max_pos if max_pos > 0 else 0

    return {
        "score": net_score,
        "ratio": round(ratio, 3),
        "likely_provable": ratio >= 0.03,
        "pos_score": pos_score,
        "neg_score": neg_score,
        "matched": matched,
        "anti_matched": anti_matched,
    }


def hash_claim(claim: str) -> str:
    """Normalize and hash a claim for deduplication.

    Normalization:
    - Lowercase
    - Strip whitespace
    - Collapse multiple spaces
    - Remove punctuation variations

    Returns:
        SHA256 hash of normalized claim (first 32 chars)
    """
    normalized = claim.lower().strip()
    normalized = re.sub(r'\s+', ' ', normalized)
    normalized = re.sub(r'[.,;:!?]+$', '', normalized)
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:32]
