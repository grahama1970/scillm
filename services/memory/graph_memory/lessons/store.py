"""Single write bottleneck for ALL lesson storage. ENFORCES taxonomy on every write.

No matter which skill or code path stores a lesson, it MUST go through store_lesson().
If bridge_attributes is missing/empty, runs fast-mode keyword extraction before storing.
If extraction yields nothing and doc is not a test fixture, raises ValueError.
"""

from __future__ import annotations

from loguru import logger
from typing import Any, Dict, Optional

# DB-backed bridge keywords cache — domain knowledge lives in ArangoDB.
# See /best-practices-arangodb rule arango-no-hardcoded-domain-lists.
_bridge_keywords_cache: Dict[str, list[str]] | None = None
BRIDGE_VALUES = {"Precision", "Resilience", "Fragility", "Corruption", "Loyalty", "Stealth"}

# --- Heart/Mind compat readers (Task 3, Wave 1) ---
# These let all Python code work with both old and new field names during
# the tactical_tags→mind and collection_tags→heart migration.
# Prefer new field; fall back to legacy. Remove in Wave 4 cleanup.

VALID_MIND_TAGS = {"Detect", "Evade", "Exploit", "Harden", "Isolate", "Model", "Persist", "Restore"}
VALID_HEART_TAGS = {"anger", "fear", "joy", "sadness", "trust"}
VALID_INTENT_TAGS = {"Navigate", "Expand", "Filter", "Analyze", "Compare", "Trace", "Layout", "Persist"}


def get_mind_tags(doc: dict) -> list:
    """Read mind tags from a doc, falling back to tactical_tags (compat)."""
    return doc.get("mind") or doc.get("tactical_tags", [])


def get_heart_tags(doc: dict) -> list:
    """Read heart tags from a doc, falling back to collection_tags (compat)."""
    return doc.get("heart") or doc.get("collection_tags", [])


def get_intent_tags(doc: dict) -> list:
    """Read intent tags from a doc (UI interaction taxonomy)."""
    return doc.get("intent", [])


def _get_bridge_keywords() -> Dict[str, list[str]]:
    """Load bridge keywords from ArangoDB taxonomy_vocabulary."""
    global _bridge_keywords_cache
    if _bridge_keywords_cache is not None:
        return _bridge_keywords_cache
    try:
        from ..arango_client import get_db
        db = get_db()
        cursor = db.aql.execute(
            "FOR t IN taxonomy_vocabulary "
            "FILTER t.category IN ['bridge_keyword', 'bridge_keyword_extended'] "
            "RETURN {bridge: t.bridge_concept, term: t.term}"
        )
        _bridge_keywords_cache = {}
        for row in cursor:
            _bridge_keywords_cache.setdefault(row["bridge"], []).append(row["term"].lower())
    except Exception as exc:
        logger.error("bridge_keywords cache load failed (DB unavailable?): {}", exc)
        _bridge_keywords_cache = {}
    return _bridge_keywords_cache


def _uniq_preserve(items: list[str]) -> list[str]:
    seen = set()
    out: list[str] = []
    for item in items:
        token = str(item).strip()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _sanitize_tag_bridge_fields(doc: dict) -> None:
    """Ensure tags and bridge_attributes remain separate."""
    raw_tags = doc.get("tags") or []
    if not isinstance(raw_tags, list):
        raw_tags = [raw_tags]
    tags = _uniq_preserve([str(t) for t in raw_tags])

    bridge_from_tags = [t for t in tags if t in BRIDGE_VALUES]
    clean_tags = [t for t in tags if t not in BRIDGE_VALUES]

    bridges = doc.get("bridge_attributes") or []
    if not isinstance(bridges, list):
        bridges = [bridges]
    merged_bridges = _uniq_preserve([str(b) for b in bridges] + bridge_from_tags)

    doc["tags"] = clean_tags
    doc["bridge_attributes"] = merged_bridges


def extract_bridges_fast(text: str) -> list[str]:
    """Fast keyword-based bridge attribute extraction (no LLM).

    Matches substring patterns from ArangoDB taxonomy_vocabulary against lowered text.
    Returns list of matched bridge tags (e.g. ["Resilience", "Loyalty"]).
    """
    if not text or not text.strip():
        return []
    text_lower = text.lower()
    tags = []
    for tag, patterns in _get_bridge_keywords().items():
        if any(p in text_lower for p in patterns):
            tags.append(tag)
    return tags


def store_lesson(
    db: Any,
    doc: dict,
    *,
    collection: str = "lessons_v2",
) -> dict:
    """Store a lesson to ArangoDB. ENFORCES taxonomy on every write.

    If bridge_attributes is missing/empty, runs fast-mode keyword extraction
    from problem+solution text before storing. If extraction yields nothing
    and doc is not a test fixture (scope != "sanity_test_*", demo != True),
    raises ValueError.

    Args:
        db: ArangoDB database handle (from get_db()).
        doc: Lesson document dict. Expected fields: problem, solution/playbook, scope.
        collection: Target collection name (default "lessons").

    Returns:
        The stored/updated document dict.

    Raises:
        ValueError: If no taxonomy can be extracted and doc is not a test fixture.
    """
    _sanitize_tag_bridge_fields(doc)

    # 1. Check for existing bridge_attributes
    bridges = doc.get("bridge_attributes")
    if not bridges or (isinstance(bridges, (list, dict)) and len(bridges) == 0):
        # 2. Auto-enrich via fast keyword extraction
        text = _build_extraction_text(doc)
        bridges = extract_bridges_fast(text)
        doc["bridge_attributes"] = bridges
        doc["taxonomy_method"] = "auto-enriched-at-write"
        # bridge_attributes stay in their own field — NOT merged into tags[]

    # 3. Final validation (skip for sanity test fixtures, demo data, and operational data)
    scope = doc.get("scope", "")
    doc_tags = set(doc.get("tags") or [])
    is_exempt = (
        (isinstance(scope, str) and scope.startswith("sanity_test"))
        or doc.get("demo", False)
        or doc.get("code_symbol", False)  # code metadata has no prose to extract from
        # Operational data from datalake pipeline — assessments, extracted content chunks
        or (isinstance(scope, str) and scope.startswith("datalake"))
        or (isinstance(scope, str) and scope.startswith("evidence_case"))
        or bool(doc_tags & {"pdf_assessment", "extracted_content", "extraction_assessment"})
        # Persona QRA batch imports — curated content, don't block on taxonomy
        or doc.get("batch_import", False)
        or bool(doc_tags & {"doc2qra", "persona_qra", "distilled", "evidence_case", "evidence_edge"})
        # Conversation session reviews — graded session summaries
        or bool(doc_tags & {"review_conversation", "sparta_session"})
    )
    if not is_exempt:
        if not doc.get("bridge_attributes") or len(doc["bridge_attributes"]) == 0:
            title = doc.get("title", doc.get("problem", "unknown"))
            raise ValueError(
                f"BLOCKED: lesson has no extractable taxonomy. "
                f"Title: {str(title)[:80]}"
            )

    # 4. Generate embedding if missing (required by vector index on lessons)
    if not doc.get("embedding"):
        embed_text = _build_extraction_text(doc)
        if embed_text.strip():
            try:
                from ..embeddings import encode_texts
                vecs = encode_texts([embed_text])
                if vecs and len(vecs) == 1 and vecs[0]:
                    doc["embedding"] = vecs[0]
                else:
                    logger.warning("Embedding service returned empty for lesson")
            except Exception as exc:
                logger.warning("Embedding generation failed (learn will retry): {}", exc)

    # 5. UPSERT — prefer _key (exact), then problem_hash+scope, then title+scope
    if "_key" in doc:
        upsert_match = "{ _key: @match_key }"
        match_key = doc["_key"]
    elif doc.get("problem_hash"):
        upsert_match = "{ problem_hash: @match_key, scope: @scope }"
        match_key = doc["problem_hash"]
    else:
        upsert_match = "{ title: @match_key, scope: @scope }"
        match_key = doc.get("title", "")

    bind_vars: dict[str, Any] = {
        "match_key": match_key,
        "doc": doc,
        "@coll": collection,
    }
    if "@scope" in upsert_match:
        bind_vars["scope"] = scope

    result = list(
        db.aql.execute(
            f"UPSERT {upsert_match} INSERT @doc UPDATE @doc IN @@coll RETURN NEW",
            bind_vars=bind_vars,
        )
    )
    return result[0] if result else doc


def _build_extraction_text(doc: dict) -> str:
    """Build text for taxonomy extraction from a lesson document."""
    parts = []
    for field in ("problem", "solution", "playbook", "title"):
        val = doc.get(field)
        if val and isinstance(val, str):
            parts.append(val)
    # Also check tags for domain hints
    tags = doc.get("tags") or []
    if tags:
        parts.append(" ".join(str(t) for t in tags))
    return " ".join(parts)
