"""Spellcheck and NVIS annotation utilities for entity extraction."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from .entity_extraction import EntityExtractionResult

_spellchecker_cache = None


_SPELLCHECK_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "do", "does",
    "for", "from", "how", "if", "in", "into", "is", "it", "of", "on", "or",
    "so", "than", "that", "the", "their", "then", "there", "these", "this",
    "those", "to", "was", "what", "when", "where", "which", "while", "who",
    "why", "with", "provide", "provides", "protect", "protects", "target",
    "targets", "bridge", "bridges", "relate", "relates", "exist", "exists",
    # Common English words falsely flagged as typos in stress test (2026-03-15)
    "helmet", "warfare", "hardware", "software", "effectiveness", "defense",
    "between", "balance", "should", "computer", "computing", "appropriate",
    "requirements", "recommended", "restrictions", "collaboration",
    "implementation", "protections", "vendor", "coverage", "supply",
}


def _is_common_english_word(word: str) -> bool:
    """Check if a word is too common/generic for domain spellcheck."""
    if not word:
        return True
    if word in _SPELLCHECK_STOPWORDS:
        return True
    if len(word) <= 2 and word.isalpha():
        return True
    return False


def _is_simple_inflection(word: str, candidate: str) -> bool:
    """Suppress mundane inflection variants without hiding truncations/typos."""
    suffixes = ("s", "es", "ed", "ing", "er", "ers", "ion", "ions", "al", "ally")
    for suffix in suffixes:
        if candidate.endswith(suffix) and candidate[: -len(suffix)] == word:
            return True
        if word.endswith(suffix) and word[: -len(suffix)] == candidate:
            return True
    return False


def _is_truncation_candidate(word: str, candidate: str) -> bool:
    """Allow long typo/truncation prompts without over-triggering on short words."""
    if len(word) < 7 or len(candidate) < 7:
        return False
    prefix = 0
    for a, b in zip(word, candidate):
        if a != b:
            break
        prefix += 1
    return prefix >= 5


def _get_general_spellchecker():
    global _spellchecker_cache
    if _spellchecker_cache is not None:
        return _spellchecker_cache
    from spellchecker import SpellChecker

    _spellchecker_cache = SpellChecker(distance=2)
    return _spellchecker_cache


def spellcheck_question(text: str, db=None) -> list[dict[str, Any]]:
    """Check words against domain vocabulary from ArangoDB using rapidfuzz."""
    from rapidfuzz import fuzz, process as rfprocess
    from rapidfuzz.distance import Levenshtein
    from .entity_helpers import (
        _compound_parts,
        _compound_variants,
        _lookup_compound_control,
        _get_domain_vocabulary,
        _get_flashtext_processor,
        _tokenize,
    )

    misspellings: list[dict[str, Any]] = []
    seen: set[str] = set()
    domain_vocab = _get_domain_vocabulary(db)
    vocab_list = sorted(domain_vocab)
    vocab_lower = {v.lower() for v in domain_vocab}

    for token in _tokenize(text):
        word_lower = token.lower()
        if word_lower in seen or word_lower in vocab_lower:
            continue
        seen.add(word_lower)
        has_digits = any(ch.isdigit() for ch in token)
        compound_variants = _compound_variants(token)
        namespace_suffix = token.split(":", 1)[1] if ":" in token else token
        namespace_compound = ":" in token and len(_compound_parts(namespace_suffix)) >= 2
        compound_parts = _compound_parts(token)
        has_compound_alias = len(compound_parts) >= 2
        allow_compound_lookup = namespace_compound or (has_compound_alias and not has_digits)
        control_match = _lookup_compound_control(token, db) if allow_compound_lookup else None
        if has_digits and not namespace_compound:
            continue
        if _is_common_english_word(word_lower) and not compound_variants:
            continue

        # Normalize fused or hyphenated compounds before escalating to fuzzy
        # matching so "HardwareDriver" becomes a clarify suggestion instead of
        # a hard corpus miss.
        for variant in compound_variants if has_compound_alias else []:
            variant_lower = variant.lower()
            variant_match = variant_lower in vocab_lower
            if not variant_match:
                try:
                    kp = _get_flashtext_processor(db)
                    variant_match = bool(kp.extract_keywords(variant))
                except Exception as exc:
                    logger.error("compound variant flashtext lookup failed for '{}': {}", token, exc)
            if variant_match or control_match:
                suggestion = variant
                payload: dict[str, Any] = {
                    "word": token,
                    "suggestion": suggestion,
                    "distance": 1,
                    "similarity": 0.99,
                }
                if control_match:
                    payload["suggestion"] = control_match.get("name") or suggestion
                    payload["control_id"] = control_match.get("control_id", "")
                    payload["control_name"] = control_match.get("name", "")
                    payload["source_framework"] = control_match.get("source_framework", "")
                misspellings.append(payload)
                break
        else:
            accepted = False
            if token.isalpha() and not token.islower():
                result = rfprocess.extractOne(
                    token, vocab_list,
                    scorer=fuzz.ratio,
                    score_cutoff=85,
                )
                if result:
                    match_word, score, _ = result
                    dist = Levenshtein.distance(word_lower, match_word.lower())
                    match_lower = match_word.lower()
                    is_inflection = _is_simple_inflection(word_lower, match_lower)
                    shares_prefix = (
                        word_lower.startswith(match_lower) or
                        match_lower.startswith(word_lower)
                    )
                    is_short_prefix = len(word_lower) < 7 and shares_prefix
                    is_truncation = shares_prefix and _is_truncation_candidate(word_lower, match_lower)
                    if 0 < dist <= 2 and not is_inflection and (not is_short_prefix or is_truncation):
                        misspellings.append({
                            "word": token,
                            "suggestion": match_word,
                            "distance": dist,
                            "similarity": round(score / 100.0, 3),
                        })
                        accepted = True
            if not accepted and token.isalpha() and len(token) >= 6:
                try:
                    spellchecker = _get_general_spellchecker()
                    correction = spellchecker.correction(word_lower)
                except Exception as exc:
                    logger.error("general spellcheck failed for '{}': {}", token, exc)
                    correction = None
                if correction and correction != word_lower:
                    score = fuzz.ratio(word_lower, correction)
                    dist = Levenshtein.distance(word_lower, correction)
                    if score >= 85 and 0 < dist <= 3:
                        misspellings.append({
                            "word": token,
                            "suggestion": correction,
                            "distance": dist,
                            "similarity": round(score / 100.0, 3),
                        })

    return misspellings


def get_annotations(result: "EntityExtractionResult") -> list[dict[str, Any]]:
    """Extract NVIS-colored annotations from entity extraction results."""
    annotations: list[dict[str, Any]] = []
    seen: set[str] = set()

    for ms in result.misspellings:
        word = ms["word"]
        if word.lower() not in seen:
            annotations.append({
                "term": word, "level": "AMBER",
                "label": f"Did you mean {ms['suggestion']}?",
                "action": "clarify", "suggestion": ms["suggestion"],
                "distance": ms["distance"],
            })
            seen.add(word.lower())

    for u in result.unresolved_terms:
        if u.get("type") == "id_like" and u["term"].lower() not in seen:
            annotations.append({
                "term": u["term"], "level": "RED",
                "label": "fabricated ID — not in corpus",
                "action": "reject",
                "closest_match": u.get("closest_match", ""),
            })
            seen.add(u["term"].lower())

    for n in result.not_in_corpus:
        if n["term"].lower() not in seen:
            annotations.append({
                "term": n["term"], "level": "YELLOW",
                "label": "not found in corpus", "action": None,
            })
            seen.add(n["term"].lower())

    for term, info in result.resolution_map.items():
        if term.lower() in seen:
            continue
        if info.get("exists") and info.get("match_type") == "exact":
            annotations.append({
                "term": term, "level": "GREEN",
                "label": info.get("name", "confirmed"),
                "action": None,
                "qra_count": info.get("qra_count", -1),
            })
            seen.add(term.lower())
        elif info.get("exists") and info.get("match_type") in ("fuzzy", "bm25_name"):
            annotations.append({
                "term": term, "level": "AMBER",
                "label": f"{info.get('match_type', 'fuzzy')} match",
                "action": "clarify",
            })
            seen.add(term.lower())

    return annotations
