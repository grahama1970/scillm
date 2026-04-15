"""Brandon's inline QRA quality assessment — portable version.

Extracted from sparta/pipeline_duckdb/qra_assessment.py for use in
graph_memory without cross-venv imports. Zero LLM calls, ~3μs per QRA.

Usage:
    from graph_memory.quality.assess import assess_qra, detect_framework
    result = assess_qra(qra_doc)
    # result["grade"] in ("PASS", "WARN", "FAIL")
"""
from __future__ import annotations

from loguru import logger

from ..lessons.store import VALID_MIND_TAGS, get_mind_tags

# ---------------------------------------------------------------------------
# Valid taxonomy tags (2-system architecture)
# Mind = 8 SPARTA tactical tags — single source of truth in store.py.
# Imported above; re-exported automatically for callers of this module.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# DB-backed caches — domain knowledge lives in ArangoDB, not Python.
# See /best-practices-arangodb rules arango-no-hardcoded-domain-lists,
# arango-no-regex-classification.
# ---------------------------------------------------------------------------

_framework_map_cache: dict[str, str] | None = None
_framework_terms_cache: dict[str, list[str]] | None = None
_answer_hints_cache: dict[str, list[str]] | None = None


def _get_framework_map() -> dict[str, str]:
    """Cache control_id → source_framework from ArangoDB."""
    global _framework_map_cache
    if _framework_map_cache is not None:
        return _framework_map_cache
    try:
        from ..arango_client import get_db
        db = get_db()
        cursor = db.aql.execute(
            "FOR c IN sparta_controls "
            "RETURN {cid: UPPER(c.control_id), fw: c.source_framework}"
        )
        _framework_map_cache = {r["cid"]: r["fw"] for r in cursor if r["cid"] and r["fw"]}
    except Exception as exc:
        logger.error("framework_map cache load failed (DB unavailable?): {}", exc)
        _framework_map_cache = {}
    return _framework_map_cache


def _get_framework_terms() -> dict[str, list[str]]:
    """Load framework terms from taxonomy_vocabulary."""
    global _framework_terms_cache
    if _framework_terms_cache is not None:
        return _framework_terms_cache
    try:
        from ..arango_client import get_db
        db = get_db()
        cursor = db.aql.execute(
            "FOR t IN taxonomy_vocabulary FILTER t.category == 'framework_term' "
            "RETURN {fw: t.framework, term: t.term}"
        )
        _framework_terms_cache = {}
        for row in cursor:
            _framework_terms_cache.setdefault(row["fw"], []).append(row["term"].lower())
    except Exception as exc:
        logger.error("framework_terms cache load failed (DB unavailable?): {}", exc)
        _framework_terms_cache = {}
    return _framework_terms_cache


def _get_answer_hints() -> dict[str, list[str]]:
    """Load taxonomy answer hints from taxonomy_vocabulary."""
    global _answer_hints_cache
    if _answer_hints_cache is not None:
        return _answer_hints_cache
    try:
        from ..arango_client import get_db
        db = get_db()
        cursor = db.aql.execute(
            "FOR t IN taxonomy_vocabulary FILTER t.category == 'answer_hint' "
            "RETURN {tag: t.bridge_concept, term: t.term}"
        )
        _answer_hints_cache = {}
        for row in cursor:
            _answer_hints_cache.setdefault(row["tag"], []).append(row["term"].lower())
    except Exception as exc:
        logger.error("answer_hints cache load failed (DB unavailable?): {}", exc)
        _answer_hints_cache = {}
    return _answer_hints_cache


# ---------------------------------------------------------------------------
# Framework detection — queries ArangoDB, not prefix guessing
# ---------------------------------------------------------------------------

def detect_framework(control_id: str) -> str:
    """Detect framework from control ID by looking up sparta_controls.

    ArangoDB knows definitively which framework a control belongs to.
    Uses a module-level cache populated on first call (~4K entries, <10ms).
    """
    cid = (control_id or "").upper()
    if not cid:
        return "UNKNOWN"
    fw_map = _get_framework_map()
    return fw_map.get(cid, "UNKNOWN")


FRAMEWORK_GROUNDING = {
    "D3FEND": {"fail": 0.55, "warn": 0.65},
    "CWE": {"fail": 0.55, "warn": 0.65},
    "ESA": {"fail": 0.60, "warn": 0.70},
    "ATT&CK": {"fail": 0.50, "warn": 0.60},
    "NIST": {"fail": 0.55, "warn": 0.65},
    "SPARTA": {"fail": 0.60, "warn": 0.70},
    "UNKNOWN": {"fail": 0.55, "warn": 0.65},
}

META_PATTERNS = [
    "this question", "this examines", "this addresses", "this explores",
    "this highlights", "this delves", "the question", "this reverses the perspective",
]

# ---------------------------------------------------------------------------
# Main assessment function
# ---------------------------------------------------------------------------

def assess_qra(qra: dict) -> dict:
    """Brandon's per-QRA quality assessment (local, no LLM calls).

    Checks:
    0. Grounding score >= framework-specific threshold
    1. Answer contains framework-appropriate terms
    2. Anchoring: answer references the control
    3. Not too short / not empty
    4. Citations present
    5. Federated Taxonomy tags present and coherent
    6. Reasoning quality (the "R" in QRA)

    Returns dict with grade ("PASS"/"WARN"/"FAIL"), framework, notes, etc.
    """
    grade = "PASS"
    notes: list[str] = []
    grounding = qra.get("grounding_score", 0.0) or 0.0
    control_id = qra.get("control_id") or ""

    framework = detect_framework(control_id)
    thresholds = FRAMEWORK_GROUNDING.get(framework, FRAMEWORK_GROUNDING["UNKNOWN"])

    # 1. Grounding check
    if grounding < thresholds["fail"]:
        grade = "FAIL"
        notes.append(f"Grounding {grounding:.3f} < {thresholds['fail']} ({framework} FAIL)")
    elif grounding < thresholds["warn"]:
        grade = "WARN"
        notes.append(f"Grounding {grounding:.3f} < {thresholds['warn']} ({framework} WARN)")

    # 2. Framework terms (from ArangoDB taxonomy_vocabulary)
    answer = (qra.get("answer") or "").lower()
    all_fw_terms = _get_framework_terms()
    fw_terms = all_fw_terms.get(framework, all_fw_terms.get("SPARTA", []))
    matched_terms = [t for t in fw_terms if t in answer]
    space_terms_ok = len(matched_terms) >= 2

    if not space_terms_ok:
        if grade == "PASS":
            grade = "WARN"
        notes.append(f"Insufficient {framework} terms ({len(matched_terms)}/2)")

    # 3. Anchoring
    anchoring_ok = True
    if control_id and control_id.lower() not in answer:
        parts = control_id.replace("-", " ").replace(":", " ").split()
        matched_anchor = [p for p in parts if len(p) > 2 and p.lower() in answer]
        if not matched_anchor:
            anchoring_ok = False
            if grade == "PASS":
                grade = "WARN"
            notes.append(f"Answer doesn't reference control {control_id}")

    # 4. Length
    if len(answer) < 50:
        grade = "FAIL"
        notes.append(f"Answer too short ({len(answer)} chars)")
    elif len(answer) < 150:
        if grade == "PASS":
            grade = "WARN"
        notes.append(f"Answer short ({len(answer)} chars)")

    # 5. Citations
    citations = qra.get("citations") or ""
    if isinstance(citations, list):
        has_citations = len(citations) > 0
    elif isinstance(citations, str):
        has_citations = citations.strip() not in ("", "[]", "null", "None")
    else:
        has_citations = bool(citations)
    if not has_citations:
        if grade == "PASS":
            grade = "WARN"
        notes.append("No citations")

    # 6. Taxonomy tags — Mind system (8 SPARTA tactical tags)
    # get_mind_tags() reads doc.mind with compat fallback to tactical_tags/conceptual_tags
    mind_tags = get_mind_tags(qra)
    valid_mind = [t for t in mind_tags if t in VALID_MIND_TAGS]
    taxonomy_ok = True

    if not valid_mind:
        taxonomy_ok = False
        if grade == "PASS":
            grade = "WARN"
        notes.append("No valid Mind tags (Detect/Evade/Exploit/Harden/Isolate/Model/Persist/Restore)")

    # 6b. Coherence (hints from ArangoDB taxonomy_vocabulary)
    all_hints = _get_answer_hints()
    incoherent_tags = []
    for tag in valid_mind:
        hints = all_hints.get(tag, [])
        if hints and not any(h in answer for h in hints):
            incoherent_tags.append(tag)
    if incoherent_tags and len(incoherent_tags) == len(valid_mind):
        if grade == "PASS":
            grade = "WARN"
        notes.append(f"Taxonomy incoherence: {incoherent_tags}")

    # 7. Reasoning quality
    reasoning = (qra.get("reasoning") or "").strip()
    reasoning_ok = True
    reasoning_diagnosis = ""

    if not reasoning or len(reasoning) < 30:
        reasoning_ok = False
        reasoning_diagnosis = "MISSING"
        if grade == "PASS":
            grade = "WARN"
        notes.append("No reasoning chain")
    elif any(pat in reasoning.lower() for pat in META_PATTERNS):
        reasoning_ok = False
        matched_pat = next(p for p in META_PATTERNS if p in reasoning.lower())
        reasoning_diagnosis = f"META-COMMENTARY: '{matched_pat}...'"
        if grade == "PASS":
            grade = "WARN"
        notes.append(f"Reasoning is meta-commentary ('{matched_pat}...')")
    else:
        reasoning_words = set(reasoning.lower().split())
        answer_words = set(answer.split())
        overlap = len(reasoning_words & answer_words) / max(len(reasoning_words), 1)
        if overlap > 0.8:
            reasoning_ok = False
            reasoning_diagnosis = f"RESTATEMENT ({overlap:.0%} overlap)"

    return {
        "grade": grade,
        "framework": framework,
        "grounding_check": grounding,
        "anchoring_ok": anchoring_ok,
        "space_terms_ok": space_terms_ok,
        "taxonomy_ok": taxonomy_ok,
        "taxonomy_tags": {"mind": valid_mind},
        "reasoning_ok": reasoning_ok,
        "reasoning_diagnosis": reasoning_diagnosis,
        "notes": notes,
        "notes_str": "; ".join(notes) if notes else f"All checks passed ({framework})",
    }
