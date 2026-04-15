"""Rule-based extraction quality verification.

Fast heuristic checks that catch common extraction errors without LLM:
- False positives: common words extracted as entities
- False negatives: control IDs in question but not in spans
- Wrong categorization: control ID patterns labeled as phrases

Usage:
    python -m graph_memory.maintenance.verify_extraction_rules --sample 1000
    python -m graph_memory.maintenance.verify_extraction_rules --full --json > report.json
"""

from __future__ import annotations

import argparse
import json
import re
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger


# Control ID patterns - must match what extract_entities uses
CONTROL_ID_PATTERNS = [
    re.compile(r'\bCWE-\d+\b', re.I),           # CWE-79
    re.compile(r'\bCAPEC-\d+\b', re.I),         # CAPEC-86
    re.compile(r'\bT\d{4}(?:\.\d+)?\b'),        # T1595, T1595.002
    re.compile(r'\b[A-Z]{2}-\d+\b'),            # IA-0006, AC-1
    re.compile(r'\bCM\d{4}\b'),                 # CM0001
    re.compile(r'\bNIST SP \d+-\d+\b', re.I),   # NIST SP 800-53
]

# Use spaCy's comprehensive stop word list (326 words)
from spacy.lang.en import stop_words as spacy_stop_words
COMMON_WORDS = spacy_stop_words.STOP_WORDS

# POS tags that indicate dangling boundaries
# DET = determiner (the, a, an, this, that)
# ADP = adposition/preposition (of, in, on, at, by, for, with, to, from)
# CCONJ = coordinating conjunction (and, or, but)
# SCONJ = subordinating conjunction (that, which, because, if)
# AUX = auxiliary verb (is, are, was, were, be, can, could, would)
LEADING_BAD_POS = {"DET", "ADP", "CCONJ", "SCONJ"}
TRAILING_BAD_POS = {"ADP", "CCONJ", "SCONJ", "AUX"}

# Lazy-loaded spaCy model
_nlp = None

def _get_nlp():
    """Load spaCy model once, cache for reuse."""
    global _nlp
    if _nlp is None:
        import spacy
        for model in ("en_core_web_sm", "en_core_web_md", "en_core_web_lg"):
            try:
                _nlp = spacy.load(model)
                break
            except OSError:
                continue
        if _nlp is None:
            raise RuntimeError("No spaCy English model found")
    return _nlp

# Short words (1-2 chars) that are almost never valid entities
SHORT_WORDS = {'a', 'i', 'an', 'am', 'as', 'at', 'be', 'by', 'do', 'go',
               'he', 'if', 'in', 'is', 'it', 'me', 'my', 'no', 'of', 'on',
               'or', 'so', 'to', 'up', 'us', 'we'}


@dataclass
class VerificationIssue:
    """A single verification issue found."""
    issue_type: str  # false_positive, false_negative, wrong_category
    text: str
    reason: str
    qra_key: str = ""
    severity: str = "warning"  # warning, error


@dataclass
class VerificationResult:
    """Result of verifying a single QRA's extraction."""
    qra_key: str
    question: str
    span_count: int
    issues: list[VerificationIssue] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        return len(self.issues) > 0

    @property
    def issue_count(self) -> int:
        return len(self.issues)


def find_control_ids_in_text(text: str) -> set[str]:
    """Find all control ID patterns in text."""
    found = set()
    for pattern in CONTROL_ID_PATTERNS:
        for match in pattern.finditer(text):
            found.add(match.group().upper())
    return found


def is_common_word(text: str) -> bool:
    """Check if text is a common English word."""
    lower = text.lower().strip()
    return lower in COMMON_WORDS or lower in SHORT_WORDS


def has_dangling_boundary(text: str) -> tuple[bool, str]:
    """Check if span has dangling words at boundaries using spaCy POS tags.

    Returns (has_issue, description).
    """
    text = text.strip()
    if not text or len(text) < 2:
        return False, ""

    try:
        nlp = _get_nlp()
        doc = nlp(text)
        if not doc:
            return False, ""

        # Check first token
        first = doc[0]
        if first.pos_ in LEADING_BAD_POS:
            return True, f"leading {first.pos_} '{first.text}'"

        # Check last token
        last = doc[-1]
        if last.pos_ in TRAILING_BAD_POS:
            return True, f"trailing {last.pos_} '{last.text}'"

        return False, ""
    except Exception:
        # Fallback: simple word check if spaCy fails
        words = text.lower().split()
        if words[0] in {'the', 'a', 'an', 'this', 'that', 'of', 'in', 'on', 'at', 'by', 'for', 'with'}:
            return True, f"leading '{words[0]}'"
        if words[-1] in {'of', 'in', 'on', 'at', 'by', 'for', 'with', 'and', 'or', 'is', 'are', 'was', 'were'}:
            return True, f"trailing '{words[-1]}'"
        return False, ""


def is_control_id_pattern(text: str) -> bool:
    """Check if text matches a control ID pattern."""
    for pattern in CONTROL_ID_PATTERNS:
        if pattern.fullmatch(text.strip()):
            return True
    return False


def verify_single_qra(qra: dict) -> VerificationResult:
    """Verify a single QRA's entity extraction."""
    qra_key = qra.get("_key", "unknown")
    question = qra.get("question", "")
    spans = qra.get("spans", [])

    result = VerificationResult(
        qra_key=qra_key,
        question=question[:200],
        span_count=len(spans),
    )

    # Check 1: False positives - common words extracted
    for span in spans:
        text = span.get("text", "")
        kind = span.get("kind", "")

        if is_common_word(text):
            result.issues.append(VerificationIssue(
                issue_type="false_positive",
                text=text,
                reason=f"Common word '{text}' should not be extracted as {kind}",
                qra_key=qra_key,
                severity="error",
            ))

    # Check 2: Wrong categorization - control ID labeled as phrase
    for span in spans:
        text = span.get("text", "")
        kind = span.get("kind", "")

        if is_control_id_pattern(text) and kind != "control_id":
            result.issues.append(VerificationIssue(
                issue_type="wrong_category",
                text=text,
                reason=f"'{text}' matches control_id pattern but labeled as '{kind}'",
                qra_key=qra_key,
                severity="warning",
            ))

    # Check 3: False negatives - control IDs in question but not in spans
    control_ids_in_question = find_control_ids_in_text(question)
    extracted_texts = {span.get("text", "").upper() for span in spans}

    for control_id in control_ids_in_question:
        if control_id not in extracted_texts:
            # Check if it's in any span (might be part of longer text)
            found = False
            for span in spans:
                if control_id in span.get("text", "").upper():
                    found = True
                    break

            if not found:
                result.issues.append(VerificationIssue(
                    issue_type="false_negative",
                    text=control_id,
                    reason=f"Control ID '{control_id}' in question but not extracted",
                    qra_key=qra_key,
                    severity="error",
                ))

    # Check 4: Dangling boundaries - spans starting/ending with articles, prepositions, etc.
    for span in spans:
        text = span.get("text", "")
        if len(text.split()) > 1:  # Only check multi-word spans
            has_dangler, dangler_desc = has_dangling_boundary(text)
            if has_dangler:
                result.issues.append(VerificationIssue(
                    issue_type="dangling_boundary",
                    text=text,
                    reason=f"Span has {dangler_desc}",
                    qra_key=qra_key,
                    severity="warning",
                ))

    return result


def verify_sample(
    db,
    sample_size: int = 1000,
    full: bool = False,
) -> dict[str, Any]:
    """Verify extraction quality on a sample of QRAs.

    Args:
        db: ArangoDB connection
        sample_size: Number of QRAs to sample (ignored if full=True)
        full: If True, check all QRAs with evidence_case

    Returns:
        Stats dict with issue counts and examples
    """

    # Fetch QRAs with evidence_case
    if full:
        aql = """
        FOR doc IN sparta_qra
          FILTER doc.evidence_case != null AND doc.evidence_case.spans != null
          RETURN {
            _key: doc._key,
            question: doc.question,
            spans: doc.evidence_case.spans
          }
        """
        bind_vars = {}
        logger.info("Running full verification on all QRAs with spans...")
    else:
        aql = """
        FOR doc IN sparta_qra
          FILTER doc.evidence_case != null AND doc.evidence_case.spans != null
          LIMIT @limit
          RETURN {
            _key: doc._key,
            question: doc.question,
            spans: doc.evidence_case.spans
          }
        """
        # Get more than needed, then sample
        pool_size = min(sample_size * 10, 50000)
        bind_vars = {"limit": pool_size}

    pool = list(db.aql.execute(aql, bind_vars=bind_vars))

    if full:
        sample = pool
    elif len(pool) < sample_size:
        logger.warning("Only {} QRAs with spans available", len(pool))
        sample = pool
    else:
        sample = random.sample(pool, sample_size)

    logger.info("Verifying {} QRAs with rule-based checks", len(sample))

    # Run verification
    results: list[VerificationResult] = []
    issue_counts = {
        "false_positive": 0,
        "false_negative": 0,
        "wrong_category": 0,
        "dangling_boundary": 0,
    }
    total_spans = 0

    for i, qra in enumerate(sample):
        if (i + 1) % 1000 == 0:
            logger.info("Progress: {}/{}", i + 1, len(sample))

        result = verify_single_qra(qra)
        results.append(result)
        total_spans += result.span_count

        for issue in result.issues:
            issue_counts[issue.issue_type] += 1

    # Compute stats
    qras_with_issues = sum(1 for r in results if r.has_issues)
    total_issues = sum(r.issue_count for r in results)

    # Collect worst examples
    worst_examples = sorted(
        [r for r in results if r.has_issues],
        key=lambda r: -r.issue_count
    )[:10]

    # Count most common false positive terms
    fp_counts: dict[str, int] = {}
    for r in results:
        for issue in r.issues:
            if issue.issue_type == "false_positive":
                term = issue.text.lower()
                fp_counts[term] = fp_counts.get(term, 0) + 1

    common_fps = sorted(fp_counts.items(), key=lambda x: -x[1])[:10]

    return {
        "sample_size": len(sample),
        "total_spans_evaluated": total_spans,
        "qras_with_issues": qras_with_issues,
        "total_issues": total_issues,
        "issue_counts": issue_counts,
        "issue_rate": qras_with_issues / len(sample) if sample else 0,

        "common_false_positives": [
            {"term": t, "count": c} for t, c in common_fps
        ],

        "worst_examples": [
            {
                "qra_key": r.qra_key,
                "question": r.question,
                "issue_count": r.issue_count,
                "issues": [
                    {"type": i.issue_type, "text": i.text, "reason": i.reason}
                    for i in r.issues
                ]
            }
            for r in worst_examples
        ],
    }


def main():
    parser = argparse.ArgumentParser(description="Rule-based extraction verification")
    parser.add_argument("--sample", type=int, default=1000, help="Sample size")
    parser.add_argument("--full", action="store_true", help="Check all QRAs (slow)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv())

    from ..arango_client import get_db
    db = get_db()

    stats = verify_sample(db, sample_size=args.sample, full=args.full)

    if args.json:
        print(json.dumps(stats, indent=2))
    else:
        print("\n" + "=" * 60)
        print("EXTRACTION QUALITY VERIFICATION (Rule-Based)")
        print("=" * 60)
        print(f"Sample size:         {stats['sample_size']}")
        print(f"Total spans:         {stats['total_spans_evaluated']}")
        print(f"QRAs with issues:    {stats['qras_with_issues']} ({stats['issue_rate']:.1%})")
        print(f"Total issues:        {stats['total_issues']}")
        print()
        print("Issue breakdown:")
        for issue_type, count in stats['issue_counts'].items():
            print(f"  {issue_type}: {count}")

        if stats["common_false_positives"]:
            print("\nMost common false positive terms:")
            for fp in stats["common_false_positives"][:5]:
                print(f"  - '{fp['term']}' ({fp['count']} times)")

        if stats["worst_examples"]:
            print("\nWorst examples:")
            for ex in stats["worst_examples"][:3]:
                print(f"\n  QRA: {ex['qra_key']}")
                print(f"  Q: {ex['question'][:80]}...")
                print(f"  Issues ({ex['issue_count']}):")
                for issue in ex['issues'][:3]:
                    print(f"    - [{issue['type']}] {issue['reason']}")


if __name__ == "__main__":
    main()
