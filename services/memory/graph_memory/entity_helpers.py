"""Entity extraction helpers: tokenization, corpus matching, spellcheck, annotations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from loguru import logger

# Backward-compatibility: legacy callers import spellcheck helpers from this module.
from .entity_annotations import _is_common_english_word  # re-export


def _strip_trailing_verbs(phrase: str) -> str:
    """Strip trailing verbs from a phrase.

    Deterministic: "sandwiches relate" → "sandwiches"
    Uses a verb suffix heuristic instead of reparsing with spaCy —
    POS tags change without sentence context ("address" tagged NOUN
    in isolation but is VERB in the full sentence).

    Domain nouns that look like verbs (address, access, control,
    process, service, monitor, display, relay, filter, patch, probe)
    are never stripped.
    """
    if not phrase or " " not in phrase:
        return phrase
    # Domain nouns that are also common verbs — never strip these
    _DOMAIN_NOUNS = frozenset({
        "address", "access", "control", "process", "service",
        "monitor", "display", "relay", "filter", "patch", "probe",
        "bypass", "override", "buffer", "register", "interface",
        "trigger", "handle", "exploit", "target", "compromise",
        "attack", "impact", "mandate", "account", "audit",
    })
    # Common trailing verb suffixes (not domain nouns)
    _VERB_ENDINGS = frozenset({
        "relate", "involve", "involve", "require", "include",
        "affect", "enable", "ensure", "prevent", "mitigate",
        "implement", "apply", "use", "describe", "specify",
    })
    words = phrase.split()
    while len(words) > 1:
        last = words[-1].lower()
        if last in _DOMAIN_NOUNS:
            break
        if last in _VERB_ENDINGS:
            words.pop()
        else:
            break
    return " ".join(words)

if TYPE_CHECKING:
    from .entity_extraction import EntityExtractionResult

# Cache for DB-sourced vocabulary (populated on first call)
_domain_vocabulary_cache: set[str] | None = None

# Cache for SPARTA token vocabulary — every token from
# sparta_controls names + descriptions via ArangoDB TOKENS('text_en').
# Used for fast "is this word in SPARTA?" checks without per-word AQL.
_sparta_token_vocab_cache: set[str] | None = None
_aerospace_terms_cache: list[str] | None = None
_compound_boundary_re = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _get_aerospace_terms(db=None) -> list[str]:
    """Cached list of aerospace_terms for RapidFuzz matching."""
    global _aerospace_terms_cache
    if _aerospace_terms_cache is not None:
        return _aerospace_terms_cache
    if db is None:
        from .arango_client import get_db
        db = get_db()
    terms = list(db.aql.execute("FOR d IN aerospace_terms RETURN d.term", batch_size=10000))
    _aerospace_terms_cache = [t.lower() for t in terms if t]
    logger.info("Cached {} aerospace terms for corpus checking", len(_aerospace_terms_cache))
    return _aerospace_terms_cache


def _normalize_compound_spacing(text: str) -> str:
    """Normalize fused and hyphenated compounds to a spaced form."""
    spaced = _compound_boundary_re.sub(" ", text.replace("_", " ").replace("-", " "))
    return re.sub(r"\s+", " ", spaced).strip()


def _compound_variants(text: str) -> list[str]:
    """Return stable spelling variants for fused/hyphenated compound terms."""
    normalized = _normalize_compound_spacing(text)
    candidates = [normalized]
    if normalized:
        candidates.append(normalized.replace(" ", "-"))
        candidates.append(normalized.replace(" ", ""))

    variants: list[str] = []
    for candidate in candidates:
        if candidate and candidate != text and candidate not in variants:
            variants.append(candidate)
    return variants


def _compound_parts(text: str) -> list[str]:
    """Split compound terms like HardwareDriver into searchable word parts."""
    normalized = _normalize_compound_spacing(text)
    return [part.lower() for part in normalized.split() if len(part) >= 3]


def _compound_matches_resolved(text: str, resolved_terms: set[str]) -> bool:
    """Check whether a term or any normalized variant is already resolved."""
    lowered = text.lower()
    if lowered in resolved_terms:
        return True
    for variant in _compound_variants(text):
        if variant.lower() in resolved_terms:
            return True
    return False


def _lookup_compound_control(token: str, db) -> dict[str, str] | None:
    """Resolve fused control-like tokens to canonical control metadata."""
    if not token or db is None:
        return None

    namespace = ""
    suffix = token
    if ":" in token:
        namespace, suffix = token.split(":", 1)

    variants = _compound_variants(suffix)
    if not variants:
        return None

    try:
        rows = list(db.aql.execute(
            """FOR c IN sparta_controls
                   FILTER UPPER(c.control_id) == UPPER(@token)
                      OR LOWER(c.name) IN @variants
                   SORT UPPER(c.control_id) == UPPER(@token) DESC
                   LIMIT 5
                   RETURN {
                       control_id: c.control_id,
                       name: c.name,
                       source_framework: c.source_framework
                   }""",
            bind_vars={
                "token": token,
                "variants": [variant.lower() for variant in variants],
            },
        ))
    except Exception as exc:
        logger.error("compound control lookup failed for '{}': {}", token, exc)
        return None

    if not rows:
        return None
    if not isinstance(rows[0], dict):
        return None

    if namespace:
        prefix = f"{namespace.lower()}:"
        for row in rows:
            control_id = str(row.get("control_id") or "").lower()
            source_framework = str(row.get("source_framework") or "").lower()
            if control_id.startswith(prefix) or source_framework == namespace.lower():
                return row

    return rows[0]


def _get_domain_vocabulary(db=None) -> set[str]:
    """Load domain vocabulary from existing ArangoDB collections.

    Sources (no new collections — uses what /memory already has):
    - sparta_controls: control_id, name, source_framework
    - taxonomy vocabularies: term values from all vocabulary types

    FAILS LOUDLY if ArangoDB is unavailable — no silent fallback.
    A degraded vocabulary produces wrong spellcheck results that are
    impossible to debug.
    """
    global _domain_vocabulary_cache
    if _domain_vocabulary_cache is not None:
        return _domain_vocabulary_cache

    if db is None:
        from .arango_client import get_db
        db = get_db()

    vocab: set[str] = set()

    # Pull unique frameworks
    cursor = db.aql.execute(
        "FOR c IN sparta_controls "
        "COLLECT fw = c.source_framework INTO g "
        "RETURN fw"
    )
    for fw in cursor:
        if fw and isinstance(fw, str) and len(fw) >= 3:
            vocab.add(fw)

    # Pull control names as vocabulary
    cursor = db.aql.execute(
        "FOR c IN sparta_controls "
        "FILTER c.name != null "
        "LIMIT 1000 "
        "RETURN c.name"
    )
    for name in cursor:
        if name and isinstance(name, str):
            for word in name.split():
                clean = word.strip(".,;:!?()[]{}\"'")
                if len(clean) >= 4:
                    vocab.add(clean)

    # Pull taxonomy vocabulary terms
    try:
        cursor = db.aql.execute(
            "FOR t IN taxonomy_vocabulary "
            "FILTER t.term != null "
            "LIMIT 2000 "
            "RETURN t.term"
        )
        for term in cursor:
            if term and isinstance(term, str) and len(term) >= 3:
                vocab.add(term)
    except Exception as exc:
        logger.error("taxonomy_vocabulary collection not available, skipping: {}", exc)

    # Pull domain_terms collection (replaces hardcoded _KNOWN_NON_CONTROL_IDS)
    try:
        cursor = db.aql.execute(
            "FOR t IN domain_terms RETURN t.term"
        )
        for term in cursor:
            if term and isinstance(term, str):
                vocab.add(term)
    except Exception as exc:
        logger.error("domain_terms collection not available, skipping: {}", exc)

    if not vocab:
        raise RuntimeError(
            "Domain vocabulary is empty after querying sparta_controls + taxonomy_vocabulary. "
            "Check ArangoDB connection and collection state."
        )

    _domain_vocabulary_cache = vocab
    logger.info("domain vocabulary loaded: {} terms from ArangoDB", len(vocab))
    return vocab


def _get_sparta_token_vocab(db=None) -> set[str]:
    """Load SPARTA token vocabulary from all 8,979 controls.

    Uses ArangoDB TOKENS(text, 'text_en') to get analyzer-processed forms of
    every word in control names and descriptions. Same analyzer as ArangoSearch
    views, so matching is consistent.

    Result is cached — one AQL call on first use, then instant lookups.
    """
    global _sparta_token_vocab_cache
    if _sparta_token_vocab_cache is not None:
        return _sparta_token_vocab_cache

    if db is None:
        from .arango_client import get_db
        db = get_db()

    # Single AQL query: tokenize all control names and descriptions
    cursor = db.aql.execute(
        """LET all_tokens = UNIQUE(FLATTEN(
               FOR c IN sparta_controls
                   LET name_tokens = TOKENS(c.name || '', 'text_en')
                   LET desc_tokens = TOKENS(c.description || '', 'text_en')
                   RETURN APPEND(name_tokens, desc_tokens)
           ))
           RETURN all_tokens"""
    )
    tokens = set()
    for row in cursor:
        if isinstance(row, list):
            tokens.update(t for t in row if isinstance(t, str) and len(t) >= 2)

    if not tokens:
        raise RuntimeError(
            "SPARTA token vocabulary is empty. Check sparta_controls collection."
        )

    _sparta_token_vocab_cache = tokens
    logger.info("SPARTA token vocabulary loaded: {} tokens from sparta_controls", len(tokens))
    return tokens


# Cache for flashtext KeywordProcessor (Aho-Corasick glossary matcher)
_flashtext_processor_cache = None


def _get_flashtext_processor(db=None):
    """Load a flashtext KeywordProcessor with SPARTA control IDs only.

    Aho-Corasick-based glossary matcher — O(n) in text length, not vocabulary.
    Loaded from sparta_controls via /memory daemon httpx (not AQL).
    Cached after first call (~0.2s load, then 0.0ms per query).

    Keywords shorter than 4 chars are skipped to avoid false positives.
    """
    global _flashtext_processor_cache
    if _flashtext_processor_cache is not None:
        return _flashtext_processor_cache

    from flashtext import KeywordProcessor
    import httpx
    import os

    kp = KeywordProcessor(case_sensitive=False)

    # Load all controls via /memory daemon httpx — no bespoke AQL
    socket_path = os.getenv("MEMORY_SOCKET", f"/run/user/{os.getuid()}/embry/memory.sock")
    count = 0
    try:
        transport = httpx.HTTPTransport(uds=socket_path)
        with httpx.Client(transport=transport, base_url="http://localhost", timeout=30) as client:
            offset = 0
            batch_size = 500
            while True:
                resp = client.post("/list", json={
                    "collection": "sparta_controls",
                    "limit": batch_size,
                    "offset": offset,
                    "return_fields": ["control_id", "name"],
                })
                resp.raise_for_status()
                docs = resp.json().get("documents", [])
                if not docs:
                    break
                for doc in docs:
                    cid = doc.get("control_id", "")
                    name = doc.get("name", "")
                    if not cid:
                        continue
                    if len(cid) > 3:
                        kp.add_keyword(cid, cid)
                        count += 1
                offset += batch_size
    except Exception as exc:
        logger.warning("flashtext httpx load failed, falling back to db: {}", exc)
        # Fallback: direct db only for in-memory-project tests
        if db is None:
            try:
                from .arango_client import get_db
                db = get_db()
            except Exception:
                raise RuntimeError(
                    "flashtext: /memory daemon unavailable and no db handle"
                ) from exc
        cursor = db.aql.execute(
            "FOR c IN sparta_controls "
            "FILTER c.control_id != null AND c.control_id != '' "
            "RETURN {name: c.name, control_id: c.control_id}"
        )
        for row in cursor:
            cid = row.get("control_id", "")
            name = row.get("name", "")
            if cid and len(cid) > 3:
                kp.add_keyword(cid, cid)
                count += 1

    if count == 0:
        raise RuntimeError(
            "flashtext processor loaded 0 keywords from sparta_controls. "
            "Check /memory daemon and ArangoDB connection."
        )

    _flashtext_processor_cache = kp
    logger.info("flashtext processor loaded: {} control IDs from sparta_controls", count)
    return kp


# Cache for known framework names (from domain_terms collection)
_known_frameworks_cache: set[str] | None = None


def _get_known_frameworks() -> set[str]:
    """Load known framework names from ArangoDB domain_terms."""
    global _known_frameworks_cache
    if _known_frameworks_cache is not None:
        return _known_frameworks_cache
    try:
        from .arango_client import get_db
        db = get_db()
        cursor = db.aql.execute(
            "FOR t IN domain_terms FILTER t.category == 'framework_name' RETURN LOWER(t.term)"
        )
        _known_frameworks_cache = {t for t in cursor if t}
    except Exception as exc:
        logger.error("known_frameworks cache load failed (DB unavailable?): %s", exc)
        _known_frameworks_cache = set()
    return _known_frameworks_cache


def _tokenize(text: str) -> list[str]:
    """Split text into content tokens. Tokenization only — no classification.

    No stop word filtering — BM25's text_en analyzer handles that for search.
    For exact match against sparta_controls, stop words simply won't match.

    Preserves NIST-style enhancement numbers like PE-12(1), IA-5(1) — these
    are valid control_ids in sparta_controls with parenthesized suffixes.
    """
    import re
    # Match NIST enhancement pattern: XX-NN(NN) possibly with trailing punctuation
    _ENHANCEMENT_RE = re.compile(r"([A-Z]{2}-\d+\(\d+\))")

    tokens = []
    for word in text.split():
        # Check for NIST enhancement pattern BEFORE stripping parens
        m = _ENHANCEMENT_RE.search(word)
        if m:
            tokens.append(m.group(1))
            continue
        clean = word.strip("?.,!;:'\"()[]{}")
        if clean.endswith("'s") or clean.endswith("\u2019s"):
            clean = clean[:-2]
        if clean and len(clean) >= 3:
            tokens.append(clean)
    return tokens


def _decompose_chunk(chunk, skip: set[str]) -> list[str]:
    """Decompose a spaCy noun chunk into sub-phrases.

    Splits on:
    - Determiners (the, a), possessive markers ('s), prepositions, conjunctions
    - Multi-PROPN → NOUN transitions (2+ consecutive PROPNs then NOUNs split)
      "Windows XP flight management display" → ["Windows XP", "flight management display"]

    DOES NOT strip resolved terms from phrases. "CMMC Level 7 requirements"
    stays intact — the phrase IS the evidence. The resolution map separately
    tells the LLM what resolved and what didn't. Stripping "CMMC" would leave
    "Level 7 requirements" which is meaningless without context.

    Uses spaCy POS tags to respect grammatical boundaries.
    """
    _SPLIT_POS = {"DET", "PART", "ADP", "SCONJ", "AUX", "CCONJ"}

    # First pass: collect tokens with their POS, marking grammar breaks
    kept: list[tuple[str, str]] = []  # (text, pos)
    for token in chunk:
        text = token.text.strip("?.,!;:'\"")
        if not text:
            continue
        # Keep numbers (even single digit) — "Level 7" is important evidence
        if len(text) < 2 and not text.isdigit():
            continue
        # Split on grammar markers only — NOT on resolved terms
        if (token.pos_ in _SPLIT_POS
                or text.lower() in ("what", "how", "which", "whose")):
            kept.append(("__BREAK__", ""))
            continue
        kept.append((text, token.pos_))

    # Second pass: group into sub-phrases, splitting at breaks and
    # at PROPN-run → NOUN boundaries (only when PROPN run is 2+ tokens)
    sub: list[str] = []
    current: list[str] = []
    propn_run = 0

    for text, pos in kept:
        if text == "__BREAK__":
            if current:
                sub.append(" ".join(current))
                current = []
            propn_run = 0
            continue

        is_propn = pos == "PROPN"
        is_noun = pos in ("NOUN", "ADJ", "NUM")

        # Transition: we had 2+ PROPNs and now hit a NOUN → split
        if current and propn_run >= 2 and is_noun:
            sub.append(" ".join(current))
            current = []
            propn_run = 0

        current.append(text)
        if is_propn:
            propn_run += 1
        else:
            propn_run = 0

    if current:
        sub.append(" ".join(current))

    return [s for s in sub if len(s) >= 3]


def _extract_noun_phrases(
    text: str,
    resolution_map: dict[str, Any],
    control_ids: list[str],
    claimed_positions: set[int] | None = None,
) -> list[str]:
    """Extract noun phrases from text using spaCy, filtering already-resolved terms.

    Returns noun phrases that need corpus checking — phrases containing only
    already-resolved tokens (control IDs, domain terms) are excluded.

    claimed_positions: character positions already claimed by Flashtext (SPARTA
    controls + aerospace terms). Any spaCy noun_chunk that overlaps these
    positions is dropped — prior layers have priority over spaCy.
    """
    if claimed_positions is None:
        claimed_positions = set()
    try:
        import spacy
        nlp = _get_spacy_model()
        doc = nlp(text)
        resolved_lower = {t.lower() for t in resolution_map
                          if resolution_map[t].get("exists")}
        control_lower = {c.lower() for c in control_ids}
        skip = resolved_lower | control_lower

        phrases = []
        seen: set[str] = set()
        for chunk in doc.noun_chunks:
            # Drop any chunk that overlaps Flashtext-claimed character positions
            chunk_positions = set(range(chunk.start_char, chunk.end_char))
            if chunk_positions & claimed_positions:
                continue

            sub_phrases = _decompose_chunk(chunk, skip)
            for sp in sub_phrases:
                # Strip trailing verbs: "sandwiches relate" → "sandwiches"
                sp = _strip_trailing_verbs(sp)
                if not sp:
                    continue
                # Skip exact duplicates of resolved single tokens
                # But keep multi-word phrases even if some words resolved —
                # "CMMC Level 7" needs full context for the LLM to judge
                sp_lower = sp.lower()
                if sp_lower in skip or sp_lower in seen:
                    continue
                seen.add(sp_lower)
                phrases.append(sp)
        return phrases[:12]
    except Exception as exc:
        logger.error("spaCy noun_chunks failed, falling back to tokenizer: {}", exc)
        return _extract_noun_phrases_fallback(text, resolution_map, control_ids)


def classify_phrase_type(phrase: str) -> str | None:
    """Classify a phrase using WordNet hypernym chain.

    spaCy finds the noun phrase, WordNet tells us what kind of thing it is.
    Returns the top-level category (e.g. "food", "electronic_warfare",
    "equipment") or None if not in WordNet (proper nouns, jargon).

    Uses the first noun synset's hypernym path — picks the category
    3 levels up from the leaf for useful granularity.
    """
    try:
        from nltk.corpus import wordnet
        # Try the full phrase first, then the head noun (last word)
        for term in [phrase, phrase.split()[-1]]:
            syns = wordnet.synsets(term.lower(), pos=wordnet.NOUN)
            if syns:
                path = syns[0].hypernym_paths()[0]
                # Pick category ~3 levels from leaf for useful granularity
                # e.g. sandwich → snack_food → dish → food → ...
                idx = max(0, len(path) - 3)
                return path[idx].name().split(".")[0].replace("_", " ")
        return None
    except Exception:
        return None


def extract_entity_labels(text: str) -> dict[str, str]:
    """Classify entities in text using spaCy NER + WordNet.

    spaCy NER handles proper nouns (Boeing → ORG, Russia → GPE).
    WordNet handles common nouns (sandwich → food, satellite → equipment).

    Returns {lowercase_text: label} for annotation on ungrounded phrases.
    """
    labels: dict[str, str] = {}
    try:
        nlp = _get_spacy_model()
        doc = nlp(text)
        # spaCy NER for proper nouns
        for ent in doc.ents:
            labels[ent.text.lower()] = ent.label_
        # WordNet for noun chunks not already labeled by NER
        for chunk in doc.noun_chunks:
            key = chunk.text.lower()
            if key not in labels:
                wn_type = classify_phrase_type(chunk.text)
                if wn_type:
                    labels[key] = wn_type
    except Exception:
        pass
    return labels


def _extract_noun_phrases_fallback(
    text: str,
    resolution_map: dict[str, Any],
    control_ids: list[str],
) -> list[str]:
    """Fallback noun phrase extraction when spaCy is unavailable.

    Splits on prepositions/conjunctions to approximate NP boundaries.
    """
    _BREAKERS = {
        # Prepositions
        "from", "for", "with", "against", "between", "through",
        "into", "onto", "upon", "about", "during", "without",
        "to", "of", "in", "on", "at", "by",
        # Conjunctions and articles
        "and", "but", "or", "the", "a", "an", "its", "their", "that", "which",
        # Question words
        "when", "where", "how", "does", "what", "who", "why",
        # Common verbs (not entities)
        "are", "were", "has", "have", "been", "being", "is", "was",
        "do", "did", "can", "could", "would", "should", "may", "might",
        # Connection verbs (not entities)
        "relate", "relates", "related", "relating",
        "connect", "connects", "connected", "connecting",
        "affect", "affects", "affected", "affecting",
        "involve", "involves", "involved", "involving",
        "impact", "impacts", "impacted", "impacting",
        "cause", "causes", "caused", "causing",
        "lead", "leads", "leading",
        "result", "results", "resulting",
        # Domain action verbs (keep phrase boundaries clean)
        "address", "provide", "protect", "ensure", "mitigate",
        "prevent", "detect", "implement", "apply",
    }
    resolved_lower = {t.lower() for t in resolution_map}
    control_lower = {c.lower() for c in control_ids}
    tokens = _tokenize(text)
    groups: list[str] = []
    current: list[str] = []

    def _flush():
        if len(current) >= 2:
            groups.append(" ".join(current))
        elif len(current) == 1 and len(current[0]) >= 7:
            groups.append(current[0])
        current.clear()

    for token in tokens:
        tl = token.lower()
        if tl in resolved_lower or tl in control_lower or tl in _BREAKERS:
            _flush()
        else:
            current.append(token)
    _flush()
    
    # Strip trailing verbs from each group and filter empty/glue-only results
    cleaned = []
    for g in groups:
        g = _strip_trailing_verbs(g)
        if g and g.lower() not in _BREAKERS and len(g) >= 3:
            cleaned.append(g)
    return cleaned[:8]


# Lazy-loaded spaCy model (loaded once, reused)
_spacy_nlp = None


def _get_spacy_model():
    """Load spaCy English model once, cache for reuse.

    Prefers en_core_web_md (better word vectors + NER), falls back to sm.
    """
    global _spacy_nlp
    if _spacy_nlp is None:
        import spacy
        for model in ("en_core_web_md", "en_core_web_sm"):
            try:
                _spacy_nlp = spacy.load(model)
                logger.debug("spaCy loaded model: {}", model)
                break
            except OSError:
                continue
        if _spacy_nlp is None:
            raise ImportError("No spaCy English model found (need en_core_web_md or en_core_web_sm)")
    return _spacy_nlp


# POS tags that indicate dangling boundaries - phrases shouldn't start/end with these
_LEADING_BAD_POS = {"DET", "ADP", "CCONJ", "SCONJ"}  # determiner, preposition, conjunction
_TRAILING_BAD_POS = {"ADP", "CCONJ", "SCONJ", "AUX"}  # preposition, conjunction, auxiliary


def has_dangling_boundary(text: str) -> bool:
    """Check if phrase has dangling words at boundaries using spaCy POS tags.

    Returns True if the phrase starts with a determiner/preposition/conjunction
    or ends with a preposition/conjunction/auxiliary verb.

    Examples of bad spans:
        "that CWE-79" → True (leading SCONJ)
        "the telemetry" → True (leading DET)
        "access control of" → True (trailing ADP)
        "CWE-79 and" → True (trailing CCONJ)
    """
    text = text.strip()
    if not text or len(text) < 3:
        return False

    # Only check multi-word phrases
    if " " not in text:
        return False

    try:
        nlp = _get_spacy_model()
        doc = nlp(text)
        if not doc or len(doc) < 2:
            return False

        # Check first token
        first = doc[0]
        if first.pos_ in _LEADING_BAD_POS:
            logger.debug("Dangling boundary: '{}' starts with {} '{}'", text, first.pos_, first.text)
            return True

        # Check last token
        last = doc[-1]
        if last.pos_ in _TRAILING_BAD_POS:
            logger.debug("Dangling boundary: '{}' ends with {} '{}'", text, last.pos_, last.text)
            return True

        return False
    except Exception:
        # Fallback: simple word check
        words = text.lower().split()
        if words[0] in {'the', 'a', 'an', 'this', 'that', 'of', 'in', 'on', 'at', 'by', 'for', 'with'}:
            return True
        if words[-1] in {'of', 'in', 'on', 'at', 'by', 'for', 'with', 'and', 'or', 'is', 'are'}:
            return True
        return False


# Cached stopword set for is_stopword()
_STOPWORD_SET: set[str] | None = None


def is_stopword(text: str) -> bool:
    """Check if text is a common English stopword that shouldn't be extracted.

    Uses spaCy's comprehensive 326-word stopword list plus domain-specific
    common words that are not meaningful entities when standing alone.

    Args:
        text: Word or phrase to check

    Returns:
        True if the text is a stopword and should not be extracted
    """
    global _STOPWORD_SET
    if _STOPWORD_SET is None:
        from spacy.lang.en import stop_words as spacy_stop_words
        _STOPWORD_SET = spacy_stop_words.STOP_WORDS | {
            # Domain-specific common words that aren't real entities when alone
            'use', 'used', 'uses', 'using', 'user', 'users',
            'risk', 'risks', 'risky',
            'protect', 'protects', 'protection', 'protected',
            'detect', 'detects', 'detection', 'detected',
            'prevent', 'prevents', 'prevention', 'prevented',
            'implement', 'implements', 'implementation', 'implemented',
            'apply', 'applies', 'applied', 'application',
            'ensure', 'ensures', 'ensured', 'ensuring',
            'maintain', 'maintains', 'maintained', 'maintenance',
            'time', 'times', 'way', 'ways', 'case', 'cases',
            'system', 'systems', 'data', 'process', 'processes',
        }

    text_lower = text.strip().lower()

    # Single word: check directly
    if ' ' not in text_lower:
        return text_lower in _STOPWORD_SET

    # Multi-word: not a stopword (may have stopwords inside but phrase is meaningful)
    return False


def _batch_exact_match(tokens: list[str], db) -> dict[str, dict]:
    """Batch exact match tokens against sparta_controls using the control_id index.

    Uses the persistent index on control_id directly (no UPPER() wrapper which
    defeats the index and causes full collection scans). Tokens are tried in
    their original case first, then uppercased — both are index-eligible.
    """
    # Deduplicate and build candidate set (original + uppercased)
    candidates = set()
    for t in tokens:
        t = t.strip()
        if not t or len(t) < 2:
            continue
        candidates.add(t)
        candidates.add(t.upper())
    if not candidates:
        return {}
    try:
        # Single indexed IN-filter — no UPPER() function call, uses persistent index
        results = list(db.aql.execute(
            """FOR c IN sparta_controls
                 FILTER c.control_id IN @cids
                 RETURN {
                   control_id: c.control_id,
                   name: c.name,
                   framework: c.source_framework
                 }""",
            bind_vars={"cids": sorted(candidates)},
        ))
        # Map both original and uppercased keys for lookup
        out = {}
        for r in results:
            cid = r["control_id"]
            entry = {**r, "cid_upper": cid.upper(), "qra_count": 0}
            out[cid.upper()] = entry
            out[cid] = entry
        return out
    except Exception as exc:
        logger.error("batch exact match failed: {}", exc)
        return {}


def _is_in_domain_terms(term: str, db) -> bool:
    """Check if a term is a known domain term (replaces hardcoded frozenset)."""
    try:
        cursor = db.aql.execute(
            "FOR t IN domain_terms FILTER UPPER(t.term) == @term LIMIT 1 RETURN 1",
            bind_vars={"term": term.upper()},
        )
        return len(list(cursor)) > 0
    except Exception as exc:
        logger.error("domain_terms lookup failed for {!r}: {}", term, exc)
        return False


def _lookup_aerospace_term(term: str, db) -> dict | None:
    """Look up a term in the aerospace_terms collection.

    Returns dict with in_sparta_corpus, definition, suggested_sparta_terms
    if found, None otherwise. This enables the plausibility gate to
    distinguish "real term not in SPARTA" from "fabricated term".
    """
    try:
        cursor = db.aql.execute(
            """FOR t IN aerospace_terms
               FILTER UPPER(t.term) == @term
               LIMIT 1
               RETURN {
                   term: t.term,
                   full_name: t.full_name,
                   definition: t.definition,
                   in_sparta_corpus: t.in_sparta_corpus,
                   sparta_control_count: t.sparta_control_count,
                   suggested_sparta_terms: t.suggested_sparta_terms,
                   category: t.category,
                   domain: t.domain
               }""",
            bind_vars={"term": term.upper()},
        )
        results = list(cursor)
        return results[0] if results else None
    except Exception as exc:
        logger.error("aerospace_terms lookup failed for {!r}: {}", term, exc)
        return None


def _fuzzy_match_control_id(candidate: str, db) -> dict | None:
    """Find closest control ID via format normalization + exact lookup.

    Control IDs are precise identifiers — CWE-78 and CWE-788 are different
    controls, NOT misspellings. Only formatting variants are valid fuzzy matches:
      - CWE79 → CWE-79 (missing separator)
      - CWE 79 → CWE-79 (space instead of hyphen)
      - cwe-79 → CWE-79 (case normalization)
      - Prefix truncation: ESA-T2 → ESA-T2031
    """
    import re
    candidate_upper = candidate.upper().strip()

    # Stage 1: Exact match after case normalization
    try:
        exact = list(db.aql.execute(
            """FOR c IN sparta_controls
                 FILTER UPPER(c.control_id) == @candidate
                 LIMIT 1
                 RETURN {control_id: c.control_id}""",
            bind_vars={"candidate": candidate_upper},
        ))
        if exact:
            return {
                "match_type": "fuzzy",
                "closest_match": exact[0]["control_id"],
                "distance": 0.0,
            }
    except Exception as exc:
        logger.error("exact control ID match failed for {!r}: {}", candidate, exc)

    # Stage 2: Format normalization — insert missing separator
    # "CWE79" → "CWE-79", "IA5" → "IA-5", "T1556004" → "T1556.004"
    normalized = re.sub(r'^([A-Z]+)(\d)', r'\1-\2', candidate_upper)
    if normalized != candidate_upper:
        try:
            norm_result = list(db.aql.execute(
                """FOR c IN sparta_controls
                     FILTER UPPER(c.control_id) == @candidate
                     LIMIT 1
                     RETURN {control_id: c.control_id}""",
                bind_vars={"candidate": normalized},
            ))
            if norm_result:
                return {
                    "match_type": "fuzzy",
                    "closest_match": norm_result[0]["control_id"],
                    "distance": 0.05,
                }
        except Exception as exc:
            logger.error("normalized control ID match failed for {!r}: {}", candidate, exc)

    # Stage 3: Prefix match — catches truncated IDs like "ESA-T2" → "ESA-T2031"
    try:
        prefix_results = list(db.aql.execute(
            """FOR c IN sparta_controls
                 FILTER STARTS_WITH(UPPER(c.control_id), @candidate)
                 LIMIT 1
                 RETURN {control_id: c.control_id, distance: 0}""",
            bind_vars={"candidate": candidate_upper},
        ))
        if prefix_results:
            return {
                "match_type": "fuzzy",
                "closest_match": prefix_results[0]["control_id"],
                "distance": 0.0,
            }
    except Exception as exc:
        logger.error("prefix control ID match failed for {!r}: {}", candidate, exc)

    # Stage 4: Restricted typo recovery — same prefix family, edit distance 1
    # Catches "CW-78" → "CWE-78" but not "CWE-78" → "CWE-788" (different control)
    prefix_match = re.match(r'^([A-Z]{2,5})', candidate_upper)
    if prefix_match:
        prefix = prefix_match.group(1)
        try:
            results = list(db.aql.execute(
                """FOR c IN sparta_controls
                     LET cid = UPPER(c.control_id)
                     FILTER STARTS_WITH(cid, @prefix)
                     FILTER ABS(LENGTH(cid) - LENGTH(@candidate)) <= 1
                     LET dist = LEVENSHTEIN_DISTANCE(cid, @candidate)
                     FILTER dist == 1
                     SORT dist ASC
                     LIMIT 1
                     RETURN {control_id: c.control_id, distance: dist}""",
                bind_vars={"candidate": candidate_upper, "prefix": prefix},
            ))
            if results:
                return {
                    "match_type": "fuzzy",
                    "closest_match": results[0]["control_id"],
                    "distance": 0.1,
                }
        except Exception as exc:
            logger.error("typo recovery failed for {!r}: {}", candidate, exc)

    return None


def _check_words_in_corpus(
    phrases: list[str], recall_items: list[dict], db,
    resolved_terms: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Check if phrases exist in SPARTA corpus via BM25 + RapidFuzz.

    For each noun phrase:
    1. BM25 search via ArangoSearch views (text_en analyzer, same as /recall)
    2. RapidFuzz the phrase against returned text fields
    3. If no match survives RapidFuzz, mark as not_in_corpus

    Uses AQL directly because this runs inside the daemon process —
    calling /recall via HTTP would deadlock (single-worker uvicorn).
    The BM25 query uses the same text_en analyzer as /recall.

    Returns (not_found, found) where found includes source collection for color-coding.

    resolved_terms: words already confirmed to exist (F-36, CMMC, etc.)
    These are protected entities that should not be checked.
    """
    from rapidfuzz import fuzz
    from rapidfuzz import process as rfprocess

    not_found: list[dict[str, Any]] = []
    found: list[dict[str, Any]] = []
    if not phrases:
        return not_found, found
    if resolved_terms is None:
        resolved_terms = set()

    # Military platform designators (F-36, B-2, V-22, etc.)
    _platform_re = re.compile(r"^[A-Za-z]{1,2}-\d{1,3}(?:'s)?$")

    for phrase in phrases:
        phrase_stripped = phrase.strip()
        if not phrase_stripped or len(phrase_stripped) < 3:
            continue

        # Skip platform designators
        if _platform_re.match(phrase_stripped):
            continue

        # Skip already-resolved terms
        if phrase_stripped.lower() in resolved_terms:
            continue

        # Skip common English stop words that appear in every document
        # These would match on partial_ratio but are not meaningful entities
        # Use spaCy's comprehensive stop word list (326 words) + domain extras
        from spacy.lang.en import stop_words as spacy_stop_words
        _STOP_WORDS = spacy_stop_words.STOP_WORDS | {
            # Domain-specific common words that aren't real entities when alone
            'use', 'used', 'uses', 'using', 'user', 'users',
            'risk', 'risks', 'risky',
            'protect', 'protects', 'protection', 'protected',
            'detect', 'detects', 'detection', 'detected',
            'prevent', 'prevents', 'prevention', 'prevented',
            'implement', 'implements', 'implementation', 'implemented',
            'apply', 'applies', 'applied', 'application',
            'ensure', 'ensures', 'ensured', 'ensuring',
            'maintain', 'maintains', 'maintained', 'maintenance',
            'time', 'times', 'way', 'ways', 'case', 'cases',
        }
        if phrase_stripped.lower() in _STOP_WORDS:
            continue

        # Layer 2a: BM25 search across SPARTA document collections.
        _SPARTA_COLLECTIONS = ["sparta_qra", "sparta_controls", "sparta_url_knowledge",
                               "controls", "technique_knowledge", "lessons_v2"]
        phrase_lower = phrase_stripped.lower()
        best_match = None

        try:
            hits = []
            if db:
                for coll_name in _SPARTA_COLLECTIONS:
                    if not db.has_collection(coll_name):
                        continue
                    view_name = f"{coll_name}_search"
                    if view_name not in [v["name"] for v in db.views()]:
                        continue
                    cursor = db.aql.execute(
                        f"""FOR doc IN {view_name}
                              SEARCH ANALYZER(doc.question IN TOKENS(@q, 'text_en')
                                  OR doc.problem IN TOKENS(@q, 'text_en')
                                  OR doc.answer IN TOKENS(@q, 'text_en')
                                  OR doc.name IN TOKENS(@q, 'text_en')
                                  OR doc.description IN TOKENS(@q, 'text_en'),
                                  'text_en')
                              SORT BM25(doc) DESC
                              LIMIT 5
                              RETURN doc""",
                        bind_vars={"q": phrase_stripped},
                    )
                    hits.extend(list(cursor))
                    if hits:
                        break  # found matches, no need to check more collections
            if hits:
                for hit in hits:
                    for field in ("title", "question", "problem", "name", "answer",
                                  "solution", "description", "text", "playbook"):
                        val = hit.get(field) or ""
                        if not val:
                            continue
                        val_lower = val.lower()
                        # Word overlap gate: phrase words must appear as whole
                        # words in the matched text. Prevents partial_ratio from
                        # matching "ham sandwich" to docs containing "schema".
                        phrase_words = set(phrase_lower.split())
                        val_words = set(re.findall(r'\b\w+\b', val_lower))
                        overlap = phrase_words & val_words
                        if not overlap:
                            continue
                        score = fuzz.partial_ratio(phrase_lower, val_lower)
                        # Dynamic threshold by token count: short phrases
                        # need higher match quality to avoid false positives
                        n_tokens = len(phrase_lower.split())
                        threshold = 90 if n_tokens <= 2 else (82 if n_tokens == 3 else 78)
                        if score >= threshold:
                            best_match = {
                                "source": hit.get("_source", "unknown"),
                                "category": "sparta",
                                "matched_text": val[:100],
                                "score": score,
                            }
                            break
                    if best_match:
                        break
        except Exception as exc:
            logger.error("corpus BM25 check failed for '{}': {}", phrase_stripped, exc)

        # Layer 2b: RapidFuzz against aerospace_terms vocabulary (no ArangoSearch view)
        # For multi-word phrases, the matched term must cover most of the phrase
        # words — otherwise "fusion controlled space gloves" matches "controlled space"
        # on just 2/4 words, which is insufficient evidence.
        if not best_match and db:
            try:
                aero_terms = _get_aerospace_terms(db)
                match = rfprocess.extractOne(phrase_lower, aero_terms,
                                             scorer=fuzz.token_set_ratio, score_cutoff=90)
                if match:
                    matched_words = set(match[0].lower().split())
                    phrase_words = set(phrase_lower.split())
                    coverage = len(phrase_words & matched_words) / len(phrase_words) if phrase_words else 0
                    if coverage >= 0.5:
                        best_match = {
                            "source": "aerospace_terms",
                            "category": "aerospace",
                            "matched_text": match[0],
                            "score": match[1],
                        }
            except Exception as exc:
                logger.error("aerospace_terms lookup failed for '{}': {}", phrase_stripped, exc)

        if not best_match:
            not_found.append({
                "term": phrase_stripped,
                "exists_in_corpus": False,
                "reason": "not_in_corpus",
            })
        else:
            found.append({
                "term": phrase_stripped,
                "exists_in_corpus": True,
                "category": best_match["category"],
                "source": best_match["source"],
                "matched_text": best_match["matched_text"],
                "score": best_match["score"],
            })
            logger.debug("corpus match '{}' → {} [{}] (score={})",
                         phrase_stripped, best_match["source"],
                         best_match["category"], best_match["score"])

    return not_found, found


# Moved to entity_annotations.py — re-export for backward compatibility
from .entity_annotations import spellcheck_question, get_annotations  # noqa: F401
