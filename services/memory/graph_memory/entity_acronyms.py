"""Acronym expansion via flashtext (Aho-Corasick) from domain_terms collection.

Flashtext handles acronym recognition/expansion — O(n) on text length.
ArangoDB handles everything else (BM25, semantic, multihop).

Acronym expansion ALWAYS triggers needs_clarification — never silent assumption.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

# Cache for acronym flashtext processor (Aho-Corasick acronym expander)
_acronym_processor_cache: dict | None = None


def _get_acronym_processor(db=None):
    """Load a flashtext KeywordProcessor with acronyms from domain_terms.

    Maps acronym → full_name for expansion. Loaded from domain_terms
    collection where category='acronym' and full_name is set.
    Cached after first call.

    Also loads aliases: if GNSS has aliases ['GPS', 'GLONASS'],
    then GPS → 'Global Navigation Satellite System' too.

    Returns dict with:
      - 'processor': KeywordProcessor (acronym → full_name)
      - 'acronym_map': dict mapping acronym → {full_name, aliases, source}
    """
    global _acronym_processor_cache
    if _acronym_processor_cache is not None:
        return _acronym_processor_cache

    from flashtext import KeywordProcessor

    if db is None:
        from .arango_client import get_db
        db = get_db()

    kp = KeywordProcessor(case_sensitive=False)
    acronym_map: dict[str, dict] = {}

    cursor = db.aql.execute(
        "FOR d IN domain_terms "
        "FILTER d.category == 'acronym' AND d.full_name != null AND d.full_name != '' "
        "RETURN {term: d.term, full_name: d.full_name, aliases: d.aliases, source: d.source}"
    )
    count = 0
    for row in cursor:
        term = row["term"]
        full_name = row["full_name"]
        kp.add_keyword(term, full_name)
        acronym_map[term.upper()] = {
            "full_name": full_name,
            "aliases": row.get("aliases", []),
            "source": row.get("source", ""),
        }
        count += 1
        for alias in row.get("aliases") or []:
            if alias and alias.upper() != term.upper():
                kp.add_keyword(alias, full_name)

    control_matches = _build_control_match_index(db, acronym_map)

    _acronym_processor_cache = {
        "processor": kp,
        "acronym_map": acronym_map,
        "control_matches": control_matches,
    }
    logger.info("acronym processor loaded: {} acronyms from domain_terms", count)
    return _acronym_processor_cache


def _build_control_match_index(db, acronym_map: dict[str, dict]) -> dict[str, list[dict[str, str]]]:
    """Build reverse lookup: SPARTA controls that reference a full-name expansion.

    This enables bidirectional matching (SPARTA -> glossary) so we can
    surface concrete control IDs during clarification even when the user
    only supplied the acronym.
    """
    from flashtext import KeywordProcessor

    reverse_kp = KeywordProcessor(case_sensitive=False)
    added_keywords = False
    for acronym, meta in acronym_map.items():
        full_name = (meta.get("full_name") or "").strip()
        if full_name:
            reverse_kp.add_keyword(full_name, acronym)
            added_keywords = True
        for alias in meta.get("aliases") or []:
            alias_text = (alias or "").strip()
            if alias_text and len(alias_text) > 3:
                reverse_kp.add_keyword(alias_text, acronym)
                added_keywords = True

    if not added_keywords:
        return {}

    try:
        cursor = db.aql.execute(
            "FOR c IN sparta_controls "
            "RETURN {control_id: c.control_id, name: c.name, summary: c.summary, description: c.description}"
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.error("Failed loading sparta_controls for acronym matching: {}", exc)
        return {}

    matches: dict[str, list[dict[str, str]]] = {}
    seen_pairs: set[tuple[str, str, str]] = set()

    for row in cursor:
        text_parts = [
            row.get("control_id") or "",
            row.get("name") or "",
            row.get("summary") or "",
            row.get("description") or "",
        ]
        haystack = " ".join(part for part in text_parts if part)
        if not haystack:
            continue

        hits = reverse_kp.extract_keywords(haystack)
        if not hits:
            continue
        for matched in set(hits):
            acronym = matched.upper()
            control_id = (row.get("control_id") or "").strip()
            name = (row.get("name") or "").strip()
            dedupe_key = (acronym, control_id, name)
            if dedupe_key in seen_pairs:
                continue
            seen_pairs.add(dedupe_key)
            matches.setdefault(acronym, [])
            if len(matches[acronym]) >= 5:
                continue
            matches[acronym].append({
                "control_id": control_id,
                "name": name,
            })

    return matches


def expand_acronyms(text: str, db=None) -> list[dict[str, Any]]:
    """Find and expand acronyms in text using flashtext.

    Returns list of expansions found:
      [{"acronym": "ICS", "full_name": "Industrial Control Systems",
        "source": "NIST SP 800-82"}]

    This is DATA COLLECTION — the agent/caller decides what to do with it.
    Acronym expansion always triggers needs_clarification (never silent).
    """
    cache = _get_acronym_processor(db)
    kp = cache["processor"]
    acronym_map = cache["acronym_map"]
    control_matches = cache.get("control_matches") or {}

    keywords_found = kp.extract_keywords(text, span_info=True)

    expansions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for full_name, start, end in keywords_found:
        original = text[start:end]
        original_upper = original.upper()
        if original_upper in seen:
            continue
        seen.add(original_upper)

        info = acronym_map.get(original_upper, {})
        controls = control_matches.get(original_upper, [])
        expansions.append({
            "acronym": original,
            "full_name": full_name,
            "source": info.get("source", ""),
            "related_controls": controls[:5],
        })

    return expansions
