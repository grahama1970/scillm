"""Classify every token in a question as one of four entity types.

Given a natural-language question about space cybersecurity, this module
resolves every meaningful token against ArangoDB and returns typed spans
that downstream gates (/create-evidence-case) use for grounding decisions.

Entity types (mutually exclusive per span):
  control_id      — a real SPARTA/NIST/CWE/ATT&CK/CAPEC control ID
                     (e.g. CM0078, SC-40, CWE-787). Has framework, name,
                     description from sparta_controls.
  aerospace_term  — domain vocabulary from the aerospace_terms collection
                     (e.g. "satellite", "jamming", "countermeasures").
                     Valid context but NOT a control.
  phrase          — a noun phrase discovered via spaCy that may or may not
                     appear in the evidence corpus. Checked against recall
                     items for relevance.
  misspelling     — a token that fuzzy-matches a known control ID or
                     framework name. Triggers the CLARIFY gate.

Resolution pipeline (8 steps, ~6s total):
  1. flashtext Aho-Corasick against sparta_controls (control IDs only)
  2. Batch exact-match all tokens against sparta_controls
  3. Hybrid search (BM25 + semantic) for phrase-discovered controls
  4. Spellcheck via rapidfuzz against domain vocabulary
  5. Word-level corpus check on remaining noun phrases
  6. Build resolution_map (every token → exists/missing/misspelled)
  7. Taxonomy tag extraction (Mind tags for graph traversal)
  8. Build spans list for sentence markup (NVIS colors in UI)

All domain knowledge comes from ArangoDB. No regex for classification.
No hardcoded term lists. Regex only for tokenization.

Example input:
  "What countermeasures does SPARTA define for satellite jamming attacks?"

Example output (spans):
  [
    {"entity": "countermeasures", "type": "aerospace_term", "status": "valid",
     "category": "aerospace", "start": 5, "end": 20},
    {"entity": "SPARTA",          "type": "phrase",          "status": "in_corpus",
     "category": "domain_term", "start": 26, "end": 32},
    {"entity": "satellite",       "type": "aerospace_term", "status": "valid",
     "category": "aerospace", "start": 44, "end": 53},
    {"entity": "jamming",         "type": "aerospace_term", "status": "valid",
     "category": "aerospace", "start": 54, "end": 61}
  ]

Example output (phrase_controls discovered via hybrid search):
  ["CM0078", "CM0082", "EX-0016.01", "SV-AV-1"]

Example output (control_metadata for CM0078):
  {"control_id": "CM0078", "name": "Space-Based Radio Frequency Mapping",
   "framework": "SPARTA", "type": "countermeasure",
   "description": "Space-based RF mapping is the ability to monitor..."}

Example output (resolution_map):
  {"countermeasures": {"exists": true, "match_type": "aerospace_term"},
   "SPARTA":          {"exists": true, "match_type": "domain_term"},
   "CM0078":          {"has_ground_truth": false},
   "EX-0016.01":      {"has_ground_truth": true, "tactic": null}}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

# Re-export helpers for backward compatibility (profiling.py imports these)
from .entity_helpers import (
    _get_domain_vocabulary,
    _domain_vocabulary_cache,
    _get_known_frameworks,
    _get_flashtext_processor,
    _tokenize,
    _extract_noun_phrases,
    extract_entity_labels,
    _batch_exact_match,
    _is_in_domain_terms,
    _lookup_aerospace_term,
    _fuzzy_match_control_id,
    _check_words_in_corpus,
    has_dangling_boundary,
    is_stopword,
    spellcheck_question,
    get_annotations,
)
from .entity_acronyms import expand_acronyms


@dataclass
class EntityExtractionResult:
    """Result of entity extraction from question text."""

    control_ids: list[str] = field(default_factory=list)
    phrases: list[str] = field(default_factory=list)
    phrase_controls: list[str] = field(default_factory=list)
    all_control_ids: list[str] = field(default_factory=list)
    control_metadata: list[dict[str, Any]] = field(default_factory=list)
    taxonomy_tags: dict[str, Any] = field(default_factory=dict)
    recall_items: list[dict[str, Any]] = field(default_factory=list)
    recall_confidence: float = 0.0
    recall_meta: dict[str, Any] = field(default_factory=dict)
    related_pairs: list[dict[str, str]] = field(default_factory=list)

    # --- Span information for sentence markup (NVIS colors) ---
    # Each span: {entity, start, end, type, status, framework, category, name}
    # type: "control_id" | "aerospace_term" | "phrase" | "acronym"
    # status: "valid" | "misspelling" | "not_in_corpus" | "in_corpus" | "not_relevant"
    # framework (control_id only): "NIST" | "CWE" | "ATT&CK" | "CAPEC" | "SPARTA" | "D3FEND"
    # category: "control" | "weakness" | "technique" | "attack_pattern" | "countermeasure" | "aerospace" | "domain_term"
    spans: list[dict[str, Any]] = field(default_factory=list)

    # --- Grounding evidence ---
    unresolved_terms: list[dict[str, Any]] = field(default_factory=list)
    resolution_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    misspellings: list[dict[str, Any]] = field(default_factory=list)
    possible_typos: list[dict[str, Any]] = field(default_factory=list)
    not_in_corpus: list[dict[str, Any]] = field(default_factory=list)
    acronym_expansions: list[dict[str, Any]] = field(default_factory=list)

    # --- Technique coherence (computed post-extraction) ---
    technique_coherent: bool = True
    technique_coherence_detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Output matching EVIDENCE_CASE_CONTRACT.json schema."""
        # Build answerability from entity grounding
        reason_codes: list[str] = []
        has_ungrounded = any(
            not s.get("grounded_to_framework", s.get("grounded", False))
            for s in self.spans if s.get("kind", s.get("type")) == "phrase"
        )
        if has_ungrounded:
            reason_codes.append("NON_SECURITY_ENTITY")
        # Check chain_stop on any control metadata
        for meta in self.control_metadata:
            if meta.get("chain_stop"):
                reason_codes.append("NO_PATH_TO_TARGET_FRAMEWORK")
                break
        can_answer = len(reason_codes) == 0 and len(self.all_control_ids) > 0

        # Build graph_traversal from control_metadata taxonomy edges
        all_edges: list[list[str]] = []
        last_reached = None
        path_found = False
        chain_path = None
        for meta in self.control_metadata:
            taxonomy = meta.get("taxonomy", [])
            if isinstance(taxonomy, list):
                for edge in taxonomy:
                    if isinstance(edge, dict):
                        all_edges.append([
                            edge.get("from", ""), edge.get("to", ""),
                            edge.get("framework", ""), edge.get("method", ""),
                        ])
            if meta.get("chain_stop") and not last_reached:
                # Determine last reached from edges
                fws = {e.get("framework", "") for e in taxonomy} if isinstance(taxonomy, list) else set()
                for layer in ["SPARTA", "ATT&CK", "CAPEC", "CWE"]:
                    if layer in fws:
                        last_reached = layer
                        break
            if meta.get("chain_path"):
                path_found = True
                chain_path = meta["chain_path"]

        # Strip mitre_chain from entity spans (moved to graph_traversal)
        clean_spans = []
        for s in self.spans:
            span = {k: v for k, v in s.items() if k != "mitre_chain"}
            clean_spans.append(span)

        # Clean recall items to contract fields only
        clean_items = []
        _keep = {"question", "answer", "control_id", "scores"}
        for item in self.recall_items[:5]:
            ci: dict[str, Any] = {k: v for k, v in item.items() if k in _keep}
            if "mind" in item:
                ci["tactical_tags"] = item["mind"]
            clean_items.append(ci)

        # Keep only contract-specified meta fields
        recall_meta = {
            "took_ms": self.recall_meta.get("took_ms", 0),
            "supplemental_count": self.recall_meta.get("supplemental_count", 0),
            "corpus": "sparta_qra",
        }

        return {
            "answerability": {
                "can_answer": can_answer,
                "confidence": self.recall_confidence,
                "reason_codes": reason_codes,
            },
            "entities": clean_spans,
            "graph_traversal": {
                "target_framework": "SPARTA",
                "last_reached_framework": last_reached,
                "path_found": path_found,
                "path": chain_path,
                "edges": all_edges,
                "_edge_format": ["from", "to", "to_framework", "mapping_source"],
            },
            "recall": {
                "confidence": self.recall_confidence,
                "meta": recall_meta,
                "items": clean_items,
            },
        }

    def agent_view(self) -> dict[str, Any]:
        """Clean, flat JSON for the project agent to make grounding decisions."""
        controls = []
        seen_controls: set[str] = set()
        for cm in self.control_metadata:
            cid = cm.get("control_id", "")
            if cid and cid not in seen_controls:
                seen_controls.add(cid)
                rm = self.resolution_map.get(cid, {})
                controls.append({
                    "control_id": cid,
                    "name": cm.get("name", ""),
                    "framework": cm.get("framework", ""),
                    "confidence": rm.get("confidence", 0.0),
                    "match_type": rm.get("match_type", "phrase_discovered"),
                    "qra_count": rm.get("qra_count", -1),
                })
        for term, info in self.resolution_map.items():
            if (info.get("exists") and info.get("match_type") in ("exact", "fuzzy")
                    and info.get("control_id") and info["control_id"] not in seen_controls):
                seen_controls.add(info["control_id"])
                controls.append({
                    "control_id": info["control_id"],
                    "name": info.get("name", ""),
                    "framework": "",
                    "confidence": info.get("confidence", 0.0),
                    "match_type": info["match_type"],
                    "qra_count": info.get("qra_count", -1),
                })

        warnings: list[dict[str, Any]] = []

        for item in self.unresolved_terms:
            if item.get("type") == "id_like":
                warnings.append({
                    "term": item["term"],
                    "category": "fabricated_id",
                    "confidence": 0.0,
                    "detail": (
                        f"No match in corpus (closest: {item.get('closest_match', 'none')}, "
                        f"distance: {item.get('distance', 'N/A')})"
                    ),
                })

        for ms in self.misspellings:
            warnings.append({
                "term": ms["word"],
                "category": "misspelling",
                "confidence": 0.0,
                "detail": f"Did you mean '{ms['suggestion']}'? (edit distance {ms['distance']})",
            })

        for typo in self.possible_typos:
            guess = ""
            fuzzy_matches = typo.get("fuzzy_matches") or []
            if fuzzy_matches:
                guess = fuzzy_matches[0].get("match") or ""
            detail = (
                f"Term '{typo['word']}' looks incomplete or misspelled"
                + (f" (closest: {guess})" if guess else "")
            )
            warnings.append({
                "term": typo["word"],
                "category": "possible_typo",
                "confidence": 0.0,
                "detail": detail,
            })

        for nic in self.not_in_corpus:
            term = nic["term"]
            reason = nic.get("reason", "not_in_corpus")
            detail = f"Term '{term}' not found in corpus"
            if nic.get("missing_words"):
                detail += f" (missing: {', '.join(nic['missing_words'][:3])})"
            if nic.get("top_match"):
                detail += f" — closest QRA: {nic['top_match'][:60]}"
            warnings.append({
                "term": term,
                "category": "not_in_corpus",
                "confidence": 0.0,
                "detail": detail,
            })

        n_fabricated = sum(1 for w in warnings if w["category"] == "fabricated_id")
        n_misspelled = sum(1 for w in warnings if w["category"] == "misspelling")
        n_possible_typos = sum(1 for w in warnings if w["category"] == "possible_typo")
        n_not_in_corpus = sum(1 for w in warnings if w["category"] == "not_in_corpus")
        n_controls = len(controls)

        parts = []
        if n_controls:
            parts.append(f"{n_controls} controls resolved")
        if n_fabricated:
            parts.append(f"{n_fabricated} fabricated ID(s)")
        if n_misspelled:
            parts.append(f"{n_misspelled} misspelling(s)")
        if n_possible_typos:
            parts.append(f"{n_possible_typos} possible typo(s)")
        if n_not_in_corpus:
            parts.append(f"{n_not_in_corpus} term(s) not in corpus")
        if not parts:
            parts.append("no entities extracted")

        # Upgrade framework misspellings to higher severity
        # Known frameworks from domain_terms collection (not hardcoded)
        _known_fw = _get_known_frameworks()
        n_framework_misspellings = 0
        for w in warnings:
            if w["category"] == "misspelling" and "'" in w.get("detail", ""):
                try:
                    suggested = w["detail"].split("'")[1].lower()
                    if suggested in _known_fw:
                        n_framework_misspellings += 1
                        w["category"] = "framework_misspelling"
                except (IndexError, AttributeError):
                    pass

        grounding_ok = (
            n_fabricated == 0
            and n_framework_misspellings == 0
            and n_not_in_corpus == 0
        )

        if n_framework_misspellings:
            parts.append(f"{n_framework_misspellings} framework misspelling(s)")

        return {
            "headline": "; ".join(parts),
            "grounding_ok": grounding_ok,
            "controls": controls,
            "warnings": warnings,
            "phrases": self.phrases,
            "taxonomy_tags": self.taxonomy_tags,
            "recall_items_count": len(self.recall_items),
            "n_framework_misspellings": n_framework_misspellings,
        }


def _enrich_from_technique_knowledge(
    result: EntityExtractionResult, db=None,
) -> None:
    """Enrich resolution_map with ground truth from technique_knowledge collection.

    For each control_id in result.all_control_ids, queries technique_knowledge
    in a single AQL round-trip and merges context into the resolution_map.
    """
    if not result.all_control_ids or db is None:
        return

    cids = result.all_control_ids[:20]  # cap to avoid oversized queries
    tk_results = list(db.aql.execute(
        """FOR doc IN technique_knowledge
             FILTER doc.control_id IN @control_ids
             RETURN {
               control_id: doc.control_id,
               control_name: doc.control_name,
               tactic: doc.tactic,
               source_framework: doc.source_framework,
               url_content_text: SUBSTRING(doc.url_content_text, 0, 500),
               url_sources: doc.url_sources,
               sentence_count: doc.sentence_count,
               has_tables: doc.has_tables,
               has_ground_truth: LENGTH(doc.url_content_text) > 100
             }""",
        bind_vars={"control_ids": cids},
    ))

    found_cids: set[str] = set()
    for doc in tk_results:
        cid = doc["control_id"]
        found_cids.add(cid)
        entry = result.resolution_map.setdefault(cid, {})
        entry["technique_context"] = doc.get("url_content_text", "")
        entry["tactic"] = doc.get("tactic", "")
        entry["url_sources"] = doc.get("url_sources") or []
        entry["has_ground_truth"] = bool(doc.get("has_ground_truth"))
        entry["sentence_count"] = doc.get("sentence_count", 0)
        entry["has_tables"] = bool(doc.get("has_tables"))

    # Mark controls NOT found in technique_knowledge
    for cid in cids:
        if cid not in found_cids:
            entry = result.resolution_map.setdefault(cid, {})
            entry.setdefault("has_ground_truth", False)


def _build_spans(
    text: str,
    control_id_spans: list[tuple[str, int, int]],
    control_metadata: list[dict[str, Any]],
    misspellings: list[dict[str, Any]],
    not_in_corpus: list[dict[str, Any]],
    resolution_map: dict[str, dict[str, Any]],
    phrases: list[str] | None = None,
    recall_items: list[dict[str, Any]] | None = None,
    query_control_ids: list[str] | None = None,
    aerospace_terms: list[tuple[str, int, int]] | None = None,
    spacy_labels: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Build spans for sentence markup with NVIS colors.
    
    Each span includes position, type, status, and metadata for UI rendering.
    Phrases are included even if they pass corpus check — the LLM needs to see
    ALL entities to verify they appear in the evidence for the specific control.
    
    For phrases, we check if they appear in recall_items for the queried controls.
    A phrase might be "in_corpus" (exists somewhere) but "not_relevant_to_query"
    (doesn't appear in evidence for the specific control being asked about).
    """
    spans: list[dict[str, Any]] = []
    text_lower = text.lower()
    
    # Build evidence text from recall_items for relevance checking
    evidence_text = ""
    if recall_items:
        evidence_parts = []
        for item in recall_items:
            evidence_parts.append(item.get("question") or "")
            evidence_parts.append(item.get("answer") or "")
            evidence_parts.append(item.get("reasoning") or "")
        evidence_text = " ".join(evidence_parts).lower()
    
    # Track which positions are already covered (to avoid duplicates)
    covered_ranges: list[tuple[int, int]] = []
    
    def is_covered(start: int, end: int) -> bool:
        for cs, ce in covered_ranges:
            if start < ce and end > cs:  # Overlap
                return True
        return False
    
    def phrase_in_evidence(phrase: str) -> bool:
        """Check if phrase appears in the evidence for queried controls."""
        if not evidence_text:
            return False
        return phrase.lower() in evidence_text
    
    # Build lookup for control metadata
    meta_lookup: dict[str, dict[str, Any]] = {}
    for m in control_metadata:
        cid = m.get("control_id", "")
        if cid:
            meta_lookup[cid] = m
    
    # Add spans for control IDs (from flashtext) - highest priority
    for control_id, start, end in control_id_spans:
        meta = meta_lookup.get(control_id, {})
        resolution = resolution_map.get(control_id, {})
        
        # Determine framework from control_id pattern or metadata
        framework = meta.get("framework") or _infer_framework(control_id)
        category = meta.get("type") or _infer_category(control_id)
        
        # MITRE chain as edges: [{from, to, framework, method}, ...]
        mitre_chain = meta.get("taxonomy", [])

        span_dict: dict[str, Any] = {
            "text": control_id,
            "span": [start, end],
            "kind": "control_id",
            "grounded_to_framework": True,
            "grounded_to_corpus": True,
            "framework": framework,
            "semantic_class": category,
            "name": meta.get("name", ""),
            "description": (meta.get("description") or "")[:200],
        }
        if mitre_chain:
            span_dict["mitre_chain"] = mitre_chain
        spans.append(span_dict)
        covered_ranges.append((start, end))

    # Add spans for aerospace terms (domain vocabulary, NOT control IDs)
    for term, start, end in (aerospace_terms or []):
        if not is_covered(start, end):
            spans.append({
                "text": term,
                "span": [start, end],
                "kind": "aerospace_term",
                "grounded_to_framework": True,
                "grounded_to_corpus": True,
                "semantic_class": "aerospace",
                "name": term,
            })
            covered_ranges.append((start, end))

    # Add spans for phrases (domain terms that may or may not be in corpus)
    # These need to be visible to the LLM to check against evidence
    if phrases:
        for phrase in phrases:
            # Skip phrases with dangling boundaries (e.g., "that CWE-79", "access of")
            if has_dangling_boundary(phrase):
                continue

            # Skip single-word stopwords (e.g., "that", "this", "them")
            if is_stopword(phrase):
                continue

            phrase_lower = phrase.lower()
            pos = text_lower.find(phrase_lower)
            if pos >= 0 and not is_covered(pos, pos + len(phrase)):
                # Check if this phrase is in not_in_corpus
                nic_entry = next(
                    (n for n in not_in_corpus if n.get("term", "").lower() == phrase_lower),
                    None
                )
                
                # Determine status and relevance:
                # - not_in_corpus: words don't exist in SPARTA vocab
                # - in_corpus: words exist AND phrase appears in evidence
                # - not_relevant: words exist but phrase NOT in evidence for this control
                if nic_entry:
                    status = "not_in_corpus"
                    relevant = False
                else:
                    relevant = phrase_in_evidence(phrase)
                    status = "in_corpus" if relevant else "not_relevant"
                
                phrase_span: dict[str, Any] = {
                    "text": phrase,
                    "span": [pos, pos + len(phrase)],
                    "kind": "phrase",
                    "grounded_to_framework": False,
                    "grounded_to_corpus": relevant,
                    "corpus_scope": "space_cybersecurity",
                }
                if spacy_labels and phrase_lower in spacy_labels:
                    phrase_span["semantic_class"] = spacy_labels[phrase_lower]
                    phrase_span["classification_source"] = "wordnet"
                spans.append(phrase_span)
                covered_ranges.append((pos, pos + len(phrase)))
    
    # Add spans for misspellings
    for ms in misspellings:
        word = ms.get("word", "")
        if not word:
            continue
        word_lower = word.lower()
        pos = text_lower.find(word_lower)
        if pos >= 0 and not is_covered(pos, pos + len(word)):
            spans.append({
                "text": word,
                "span": [pos, pos + len(word)],
                "kind": "misspelling",
                "grounded_to_framework": False,
                "grounded_to_corpus": False,
                "suggestion": ms.get("suggestion", ""),
            })
            covered_ranges.append((pos, pos + len(word)))

    # Add spans for not_in_corpus terms (that weren't already added as phrases)
    for nic in not_in_corpus:
        term = nic.get("term", "")
        if not term:
            continue
        # Skip terms with dangling boundaries
        if has_dangling_boundary(term):
            continue
        # Skip single-word stopwords
        if is_stopword(term):
            continue
        text_lower = text.lower()
        term_lower = term.lower()
        pos = text_lower.find(term_lower)
        if pos >= 0 and not is_covered(pos, pos + len(term)):
            nic_span: dict[str, Any] = {
                "text": term,
                "span": [pos, pos + len(term)],
                "kind": "phrase",
                "grounded_to_framework": False,
                "grounded_to_corpus": False,
                "corpus_scope": "space_cybersecurity",
            }
            if spacy_labels and term_lower in spacy_labels:
                nic_span["semantic_class"] = spacy_labels[term_lower]
                nic_span["classification_source"] = "wordnet"
            spans.append(nic_span)
            covered_ranges.append((pos, pos + len(term)))

    # Sort spans by start position
    spans.sort(key=lambda s: s["span"][0])
    
    return spans


def _infer_framework(control_id: str) -> str | None:
    """Infer framework from control ID pattern."""
    cid_upper = control_id.upper()
    if cid_upper.startswith("CWE-"):
        return "CWE"
    if cid_upper.startswith("T1") or cid_upper.startswith("TA0"):
        return "ATT&CK"
    if cid_upper.startswith("CAPEC-"):
        return "CAPEC"
    if cid_upper.startswith("D3-") or cid_upper.startswith("D3F"):
        return "D3FEND"
    if cid_upper.startswith("S") and cid_upper[1:].isdigit():
        return "SPARTA"
    if cid_upper.startswith("CM") and cid_upper[2:].replace("-", "").isdigit():
        return "SPARTA"
    if cid_upper.startswith("EX-") or cid_upper.startswith("EXF-"):
        return "SPARTA"
    if cid_upper.startswith("ESA-"):
        return "SPARTA"
    # NIST families: AC, AT, AU, CA, CM, CP, IA, IR, MA, MP, PE, PL, PM, PS, PT, RA, SA, SC, SI, SR
    if len(control_id) >= 2 and control_id[:2].upper() in {
        "AC", "AT", "AU", "CA", "CM", "CP", "IA", "IR", "MA", "MP",
        "PE", "PL", "PM", "PS", "PT", "RA", "SA", "SC", "SI", "SR"
    }:
        return "NIST"
    return None


def _infer_category(control_id: str) -> str | None:
    """Infer category from control ID pattern."""
    cid_upper = control_id.upper()
    if cid_upper.startswith("CWE-"):
        return "weakness"
    if cid_upper.startswith("T1") or cid_upper.startswith("TA0"):
        return "technique"
    if cid_upper.startswith("CAPEC-"):
        return "attack_pattern"
    if cid_upper.startswith("D3-") or cid_upper.startswith("D3F"):
        return "countermeasure"
    if cid_upper.startswith("CM"):
        return "countermeasure"
    if cid_upper.startswith("S") and len(cid_upper) > 1 and cid_upper[1:].replace("-", "").isdigit():
        return "technique"
    # NIST controls
    if _infer_framework(control_id) == "NIST":
        return "control"
    return None


def extract_entities(
    text: str,
    db=None,
    include_taxonomy: bool = False,
    skip_daemon_recall: bool = False,
) -> EntityExtractionResult:
    """Extract and classify every entity in question text.

    Three entity sources, in priority order:
      1. SPARTA control IDs — flashtext against sparta_controls.
         Returns control_id spans with framework, name, description.
      2. Aerospace terms — found by spaCy noun phrases + corpus check
         against aerospace_terms collection (domain vocabulary, not controls).
      3. spaCy noun phrases — whatever remains gets phrase-checked
         against recall evidence for relevance.

    Steps:
      1. flashtext Aho-Corasick → SPARTA control IDs
      1b. Acronym expansion (e.g. "EW" → "Electronic Warfare")
      2. Batch exact-match tokens against sparta_controls
      3. Hybrid search question text (BM25 + semantic) → phrase_controls
         (skipped when skip_daemon_recall=True)
      4. Spellcheck via rapidfuzz → misspellings list
      5. Word-level corpus check on noun phrases
      6. Build resolution_map (token → exists/missing/misspelled)
      7. Taxonomy extraction (Mind tags)
      8. Build spans list for sentence markup

    Args:
        text: Question text to extract entities from
        db: ArangoDB handle (optional, will be created if not provided)
        include_taxonomy: If True, extract taxonomy tags
        skip_daemon_recall: If True, skip hybrid search via daemon (use when
            running inside the memory service to avoid circular socket calls)
    """
    result = EntityExtractionResult()
    if not text or not text.strip():
        return result

    # Get DB handle (needed for flashtext loading and later steps)
    if db is None:
        try:
            from .arango_client import get_db
            db = get_db()
        except Exception as exc:
            logger.error("ArangoDB unavailable for entity extraction: {}", exc)

    # Step 1: Extract control IDs via flashtext (Aho-Corasick against ArangoDB vocabulary)
    # No regex — flashtext matches against actual control IDs and names in sparta_controls.
    control_id_spans: list[tuple[str, int, int]] = []
    try:
        kp = _get_flashtext_processor(db)
        found = kp.extract_keywords(text, span_info=True)
        # Flashtext returns only control IDs (no names, no aerospace terms)
        result.control_ids = sorted(set(cid for cid, _, _ in found))
        control_id_spans = list(found)
    except Exception as exc:
        logger.warning("flashtext entity extraction failed: {}", exc)
        result.control_ids = []

    if db:
        # Step 1b: Acronym expansion (flashtext, 0ms) — runs before search
        # so expanded full names can augment hybrid search query.
        try:
            result.acronym_expansions = expand_acronyms(text, db)
            for exp in result.acronym_expansions:
                result.resolution_map[exp["acronym"]] = {
                    "exists": True,
                    "match_type": "acronym_expansion",
                    "confidence": 0.6,
                    "control_id": "",
                    "name": exp["full_name"],
                    "needs_clarification": True,
                    "source": exp.get("source", ""),
                }
        except Exception as exc:
            logger.error("acronym expansion failed: {}", exc)

        # Step 4: spellcheck (runs after batch match, ~50ms)
        spellcheck_result = None
        try:
            spellcheck_result = spellcheck_question(text, db=db)
        except Exception as exc:
            logger.error("spellcheck failed early: {}", exc)

        # Step 2: Batch exact match ALL tokens against sparta_controls
        tokens = _tokenize(text)
        exact_map = _batch_exact_match(tokens + result.control_ids, db)

        # Record exact matches in resolution_map
        for token in tokens + result.control_ids:
            upper = token.upper().strip()
            if upper in exact_map:
                em = exact_map[upper]
                result.resolution_map[token] = {
                    "exists": True,
                    "match_type": "exact",
                    "confidence": 1.0 if em["qra_count"] > 0 else 0.9,
                    "control_id": em["control_id"],
                    "name": em["name"],
                    "qra_count": em["qra_count"],
                }

        # Step 3: Hybrid search — augment query with expanded acronyms
        # Skip when running inside memory service (avoids circular socket call)
        if not skip_daemon_recall:
            expanded_terms = " ".join(
                exp["full_name"] for exp in result.acronym_expansions
            )
            search_query = text[:300]
            if expanded_terms:
                search_query = f"{search_query} {expanded_terms}"[:500]
            try:
                import httpx as _httpx
                import os as _os
                _socket = _os.getenv("MEMORY_SOCKET", "/run/user/1000/embry/memory.sock")
                _transport = _httpx.HTTPTransport(uds=_socket)
                _client = _httpx.Client(transport=_transport, base_url="http://localhost", timeout=15)
                try:
                    _resp = _client.post("/recall", json={
                        "q": search_query, "k": 5,
                        "collections": ["sparta_controls"],
                    })
                    _data = _resp.json()
                    # Map problem/solution back to question/answer for QRAs
                    raw_items = _data.get("items", [])
                    for item in raw_items:
                        if "problem" in item and "question" not in item:
                            item["question"] = item.pop("problem")
                        if "solution" in item and "answer" not in item:
                            item["answer"] = item.pop("solution")
                    result.recall_items = raw_items
                    result.recall_confidence = _data.get("confidence", 0.0)
                    result.recall_meta = _data.get("meta", {})
                    for item in result.recall_items:
                        cid = item.get("control_id")
                        if cid:
                            cid_str = str(cid).upper()
                            if cid_str not in result.control_ids:
                                result.phrase_controls.append(cid_str)
                finally:
                    _client.close()
            except Exception as exc:
                logger.error("recall via daemon failed: {}", exc)

        # Deduplicate - keep control_ids first (explicitly mentioned), then phrase_controls
        result.phrase_controls = sorted(set(result.phrase_controls) - set(result.control_ids))
        result.all_control_ids = result.control_ids + sorted(set(result.phrase_controls) - set(result.control_ids))

        # Step 5: Check unmatched tokens — are they fabricated IDs or domain terms?
        for token in tokens:
            upper = token.upper().strip()
            if upper in exact_map or token in result.resolution_map:
                continue

            # Is it a known domain term? (from ArangoDB, not a hardcoded list)
            if _is_in_domain_terms(token, db):
                # Check aerospace_terms for SPARTA corpus coverage
                aero = _lookup_aerospace_term(token, db)
                if aero and not aero.get("in_sparta_corpus", True):
                    # Real aerospace term but NOT in SPARTA corpus
                    result.resolution_map[token] = {
                        "exists": True, "match_type": "domain_term",
                        "in_sparta_corpus": False,
                        "confidence": 0.8, "control_id": "", "name": token,
                        "full_name": aero.get("full_name", ""),
                        "definition": aero.get("definition", ""),
                        "suggested_sparta_terms": aero.get("suggested_sparta_terms", []),
                    }
                    # NOTE: real aerospace terms NOT in SPARTA are informational
                    # annotations, NOT evidence of a fabricated question. They stay
                    # in resolution_map only — not in not_in_corpus (which triggers
                    # the plausibility LLM to falsely reject answerable questions).
                else:
                    result.resolution_map[token] = {
                        "exists": True, "match_type": "domain_term",
                        "confidence": 0.8, "control_id": "", "name": token,
                    }
                continue

            # Strip common adjective suffixes before ID classification.
            # "ARP4761-compliant" → check "ARP4761" as the base term.
            _adj_suffixes = (
                "-compliant", "-based", "-defined", "-certified", "-approved",
                "-compatible", "-ready", "-capable", "-like", "-specific",
                "-driven", "-enabled", "-aware", "-grade", "-rated",
            )
            base_token = token
            for _suf in _adj_suffixes:
                if token.lower().endswith(_suf):
                    base_token = token[: -len(_suf)]
                    break

            # If suffix was stripped and base is a known domain term, resolve it
            if base_token != token and _is_in_domain_terms(base_token, db):
                aero = _lookup_aerospace_term(base_token, db)
                if aero and not aero.get("in_sparta_corpus", True):
                    result.resolution_map[token] = {
                        "exists": True, "match_type": "domain_term",
                        "in_sparta_corpus": False,
                        "confidence": 0.75, "control_id": "", "name": base_token,
                        "suggested_sparta_terms": aero.get("suggested_sparta_terms", []),
                    }
                    # NOTE: real aerospace terms NOT in SPARTA are informational
                    # annotations only — kept in resolution_map, NOT in not_in_corpus
                    # (which triggers plausibility LLM to falsely reject).
                else:
                    result.resolution_map[token] = {
                        "exists": True, "match_type": "domain_term",
                        "confidence": 0.75, "control_id": "", "name": base_token,
                    }
                continue

            # Strip trailing single-letter version suffixes from standard names.
            # "MIL-STD-1553B" → check "MIL-STD-1553", "MIL-STD-882E" → "MIL-STD-882"
            ver_base = base_token
            if len(ver_base) > 4 and ver_base[-1].isalpha() and ver_base[-2].isdigit():
                ver_base = ver_base[:-1]
            if ver_base != base_token and _is_in_domain_terms(ver_base, db):
                aero = _lookup_aerospace_term(ver_base, db)
                if aero and not aero.get("in_sparta_corpus", True):
                    result.resolution_map[token] = {
                        "exists": True, "match_type": "domain_term",
                        "in_sparta_corpus": False,
                        "confidence": 0.7, "control_id": "", "name": ver_base,
                        "suggested_sparta_terms": aero.get("suggested_sparta_terms", []),
                    }
                    # NOTE: real aerospace terms NOT in SPARTA are informational
                    # annotations only — kept in resolution_map, NOT in not_in_corpus.
                else:
                    result.resolution_map[token] = {
                        "exists": True, "match_type": "domain_term",
                        "confidence": 0.7, "control_id": "", "name": ver_base,
                    }
                continue

            # Does it look like an ID? (has digits mixed with letters, or uppercase with dashes)
            check_token = base_token if base_token != token else token
            has_digits = any(c.isdigit() for c in check_token)
            has_letters = any(c.isalpha() for c in check_token)
            is_uppercase = check_token == check_token.upper() and len(check_token) >= 3
            # Both branches require has_letters to avoid treating pure number
            # patterns like "800-171" (NIST framework ref) as control IDs.
            looks_like_id = (has_digits and has_letters and len(check_token) >= 5) or (
                has_letters and is_uppercase and "-" in check_token and len(check_token) >= 5
            )

            if looks_like_id:
                # Try fuzzy match against known control IDs
                fuzzy = _fuzzy_match_control_id(token, db)
                if fuzzy and fuzzy["match_type"] == "fuzzy":
                    # Fuzzy match found - user likely misspelled a real control
                    # Mark as resolved for downstream use, BUT also flag as unresolved
                    # so /memory intent routes to CLARIFY with "Did you mean X?"
                    result.resolution_map[token] = {
                        "exists": True,
                        "match_type": "fuzzy",
                        "confidence": round(0.85 - fuzzy["distance"] * 0.5, 3),
                        "control_id": fuzzy["closest_match"],
                        "name": "",
                        "distance": fuzzy["distance"],
                    }
                    # Add to unresolved_terms so CLARIFY triggers
                    result.unresolved_terms.append({
                        "term": token,
                        "type": "misspelling",
                        "exists": False,  # The TYPED term doesn't exist
                        "reason": "fuzzy_match_found",
                        "closest_match": fuzzy["closest_match"],
                        "distance": fuzzy["distance"],
                    })
                else:
                    # Fabricated ID — not in sparta_controls (no fuzzy match either)
                    result.unresolved_terms.append({
                        "term": token,
                        "type": "id_like",
                        "exists": False,
                        "reason": "no_match_in_sparta_controls",
                        "closest_match": fuzzy["closest_match"] if fuzzy else "",
                        "distance": fuzzy["distance"] if fuzzy else -1,
                    })
                    result.resolution_map[token] = {
                        "exists": False, "confidence": 0.0,
                        "reason": "no_match_in_sparta_controls",
                        "closest_match": fuzzy["closest_match"] if fuzzy else "",
                        "distance": fuzzy["distance"] if fuzzy else -1,
                    }

        # Step 5b: Extract noun phrases via spaCy and check against corpus.
        # spaCy's noun_chunks gives linguistically correct NP boundaries:
        #   "Bitcoin mining ASIC from electromagnetic pulse attacks"
        #   → ["Bitcoin mining ASIC", "electromagnetic pulse attacks"]
        # Each noun phrase is checked independently against the corpus.
        # Phrases that already resolved (control IDs, domain terms) are skipped.
        # CRITICAL: spaCy cannot claim any character span already claimed by
        # Flashtext. BUT only structured control IDs (CWE-79, T1546.005,
        # IA-10) should claim positions. Common English words that happen
        # to be control names ("Trap", "Authentication") must NOT block
        # spaCy from extracting the full noun chunk ("cosmic lint trap").
        import re as _re
        _CONTROL_ID_PATTERN = _re.compile(
            r'^[A-Z]{1,5}[-.]?\d|^\d{1,3}\.\d|^CAPEC-|^CVE-|^CWE-|^AC-|^AT-|^AU-|'
            r'^CA-|^CM-|^CP-|^IA-|^IR-|^MA-|^MP-|^PE-|^PL-|^PM-|^PS-|^PT-|'
            r'^RA-|^SA-|^SC-|^SI-|^SR-|^SV-|^T\d|^TA\d|^M\d',
            _re.IGNORECASE,
        )
        claimed_positions = set()
        for _cid, start, end in control_id_spans:
            # Only claim positions for structured IDs, not plain-word names
            span_text = text[start:end]
            if _CONTROL_ID_PATTERN.match(span_text):
                for i in range(start, end):
                    claimed_positions.add(i)

        phrase_groups = _extract_noun_phrases(
            text, result.resolution_map, result.all_control_ids,
            claimed_positions=claimed_positions,
        )

        if phrase_groups:
            # Build set of resolved terms so protected entities (F-36, CMMC, etc.)
            # are not counted as "missing" inside multi-word phrases
            resolved_lower = {
                t.lower() for t, info in result.resolution_map.items()
                if info.get("exists")
            }
            missing, corpus_found = _check_words_in_corpus(
                phrase_groups, result.recall_items, db,
                resolved_terms=resolved_lower,
            )
            result.not_in_corpus.extend(missing)
            for entry in missing:
                term = entry["term"]
                result.unresolved_terms.append({
                    "term": term, "type": "not_in_corpus",
                    "exists": False, "reason": "term not found in corpus",
                })
                result.resolution_map[term] = {
                    "exists": False, "confidence": 0.0,
                    "reason": "not_in_corpus",
                }
            for entry in corpus_found:
                term = entry["term"]
                result.resolution_map[term] = {
                    "exists": True,
                    "match_type": "corpus_recall",
                    "category": entry["category"],
                    "source": entry["source"],
                    "matched_text": entry["matched_text"],
                    "confidence": entry["score"] / 100.0,
                }

        # Build phrases list from question text
        result.phrases = phrase_groups[:5] if phrase_groups else []

    # Finalize control IDs
    if not result.all_control_ids:
        result.phrase_controls = sorted(set(result.phrase_controls) - set(result.control_ids))
        result.all_control_ids = sorted(set(result.control_ids + result.phrase_controls))

    # Step 4 (metadata): Lookup control metadata
    if db and result.all_control_ids:
        try:
            meta_results = list(db.aql.execute(
                """FOR cid IN @cids
                     LET match = FIRST(
                       FOR c IN sparta_controls
                         FILTER c.control_id == cid
                         RETURN {
                           control_id: c.control_id,
                           name: c.name,
                           description: SUBSTRING(c.description || '', 0, 500),
                           framework: c.source_framework,
                           domain: c.domain,
                           type: c.control_type
                         }
                     )
                     FILTER match != null
                     RETURN match""",
                bind_vars={"cids": result.all_control_ids[:10]},
            ))
            result.control_metadata = meta_results
        except Exception as exc:
            logger.error("control metadata lookup failed: {}", exc)

    # Step 4 (relationships): Fetch taxonomy context for each control
    # This gives the LLM full context: tactics, techniques, CAPEC mappings
    if db and result.all_control_ids:
        try:
            # Get relationships from each control for the MITRE chain frameworks.
            # Only fetch CWE, CAPEC, ATT&CK, SPARTA — skip NIST, D3FEND, NVD etc.
            # which are irrelevant for chain traversal and would dominate results.
            rel_results = list(db.aql.execute(
                """FOR cid IN @cids
                     FOR r IN sparta_relationships
                       FILTER r.source_control_id == cid OR r.target_control_id == cid
                       LET other = r.source_control_id == cid ? r.target_control_id : r.source_control_id
                       LET other_ctrl = FIRST(FOR c IN sparta_controls FILTER c.control_id == other RETURN c)
                       FILTER other_ctrl != null
                       FILTER other_ctrl.source_framework IN ['CWE', 'CAPEC', 'SPARTA']
                          OR other_ctrl.source_framework LIKE 'ATT_CK%'
                       RETURN DISTINCT {
                         control_id: cid,
                         related_id: other,
                         related_name: other_ctrl.name,
                         related_framework: other_ctrl.source_framework,
                         relationship: r.method
                       }""",
                bind_vars={"cids": result.all_control_ids[:10]},
            ))
            result.related_pairs = rel_results
            
            # Bidirectional MITRE chain as edges.
            #
            # Walk: CWE ↔ CAPEC ↔ ATT&CK ↔ SPARTA
            # Each edge is {from, to, framework, method} — the actual path.
            # 2 hops max from the seed control. Bidirectional.
            _CHAIN_FRAMEWORKS = {"CWE", "CAPEC", "SPARTA"}
            _ATTACK_PREFIXES = ("ATT_CK", "ATT&CK")

            def _is_chain_fw(fw: str | None) -> bool:
                if not fw:
                    return False
                if fw in _CHAIN_FRAMEWORKS:
                    return True
                return any(fw.startswith(p) for p in _ATTACK_PREFIXES)

            def _normalize_fw(fw: str | None) -> str:
                if not fw:
                    return ""
                if fw == "CWE": return "CWE"
                if "CAPEC" in fw: return "CAPEC"
                if any(fw.startswith(p) for p in _ATTACK_PREFIXES): return "ATT&CK"
                if fw == "SPARTA": return "SPARTA"
                return fw

            for meta in result.control_metadata:
                cid = meta.get("control_id")
                cid_fw = _normalize_fw(meta.get("framework"))
                edges: list[dict[str, str]] = []
                seen_edges: set[tuple[str, str]] = set()

                # Hop 1: only cross-framework edges from this control
                # CWE→CAPEC, CAPEC→ATT&CK, ATT&CK→SPARTA, etc.
                hop1_ids: list[str] = []
                for r in rel_results:
                    if r.get("control_id") != cid:
                        continue
                    rid = r["related_id"]
                    rfw_raw = r.get("related_framework", "")
                    rfw = _normalize_fw(rfw_raw)
                    if not rfw or rfw == cid_fw:
                        continue  # skip same-framework (CWE→CWE noise)
                    edge_key = (cid, rid)
                    if edge_key not in seen_edges:
                        seen_edges.add(edge_key)
                        edges.append({
                            "from": cid, "to": rid,
                            "framework": rfw,
                            "method": r.get("relationship", ""),
                        })
                        hop1_ids.append(rid)

                # Hop 2: from hop1 nodes, find the NEXT framework layer
                # E.g., if hop1 found CAPECs, hop2 finds their ATT&CK links
                if hop1_ids:
                    try:
                        hop2 = list(db.aql.execute(
                            """FOR seed IN @seeds
                                 LET seed_ctrl = FIRST(FOR c IN sparta_controls FILTER c.control_id == seed RETURN c)
                                 LET seed_fw = seed_ctrl.source_framework
                                 FOR r IN sparta_relationships
                                   FILTER r.source_control_id == seed OR r.target_control_id == seed
                                   LET other = r.source_control_id == seed ? r.target_control_id : r.source_control_id
                                   LET ctrl = FIRST(FOR c IN sparta_controls FILTER c.control_id == other RETURN c)
                                   FILTER ctrl != null
                                   FILTER ctrl.source_framework != seed_fw
                                   FILTER ctrl.source_framework IN ['CWE', 'CAPEC', 'SPARTA']
                                      OR ctrl.source_framework LIKE 'ATT_CK%'
                                   LIMIT 30
                                   RETURN DISTINCT {seed: seed, other: other, framework: ctrl.source_framework, method: r.method}""",
                            bind_vars={"seeds": hop1_ids[:10]},
                        ))
                        for h in hop2:
                            edge_key = (h["seed"], h["other"])
                            if edge_key not in seen_edges and h["other"] != cid:
                                seen_edges.add(edge_key)
                                edges.append({
                                    "from": h["seed"], "to": h["other"],
                                    "framework": _normalize_fw(h["framework"]),
                                    "method": h.get("method", ""),
                                })
                    except Exception as exc:
                        logger.debug("MITRE chain hop2 failed: {}", exc)

                meta["taxonomy"] = edges

                # chain_stop: first expected layer not reached
                layers_found = {e["framework"] for e in edges}
                chain_order = ["CWE", "CAPEC", "ATT&CK", "SPARTA"]
                chain_stop = None
                past_seed = False
                for layer in chain_order:
                    if layer == cid_fw:
                        past_seed = True
                        continue
                    if past_seed and layer not in layers_found:
                        chain_stop = layer
                        break
                meta["chain_stop"] = chain_stop

                # chain_path: build partial or full path through layers
                # Always build — partial chains are valuable for crosswalk questions
                path = [cid]
                edge_by_from: dict[str, list[dict]] = {}
                for e in edges:
                    edge_by_from.setdefault(e["from"], []).append(e)
                current = cid
                for target_layer in chain_order:
                    if target_layer == cid_fw:
                        continue
                    found_next = None
                    for e in edge_by_from.get(current, []):
                        if e["framework"] == target_layer:
                            found_next = e["to"]
                            break
                    if not found_next:
                        for prev in path:
                            for e in edge_by_from.get(prev, []):
                                if e["framework"] == target_layer:
                                    found_next = e["to"]
                                    break
                            if found_next:
                                break
                    if found_next:
                        path.append(found_next)
                        current = found_next
                    else:
                        break  # stop at first missing layer
                meta["chain_path"] = path if len(path) > 1 else None
        except Exception as exc:
            logger.error("relationship lookup failed: {}", exc)

    # Collect spellcheck results
    try:
        misspellings = spellcheck_result if spellcheck_result is not None else []
        result.misspellings = misspellings
        for ms in misspellings:
            word = ms["word"]
            result.unresolved_terms.append({
                "term": word, "type": "misspelling", "exists": False,
                "suggestion": ms["suggestion"], "distance": ms["distance"],
                "reason": f"Did you mean '{ms['suggestion']}'? (edit distance {ms['distance']})",
                "action": "clarify",
            })
            result.resolution_map[word] = {
                "exists": False, "confidence": 0.0, "reason": "misspelling",
                "suggestion": ms["suggestion"], "distance": ms["distance"],
                "action": "clarify",
            }
    except Exception as exc:
        logger.error("spellcheck failed: {}", exc)

    # Promote single-token corpus misses to explicit typo candidates and
    # suppress duplicate phrase-level warnings when spellcheck already caught them.
    known_misspellings = {entry["word"].lower() for entry in result.misspellings}
    filtered_not_in_corpus: list[dict[str, Any]] = []
    suppressed_terms: set[str] = set()
    for entry in result.not_in_corpus:
        term = str(entry.get("term") or "").strip()
        if not term:
            continue
        lower_term = term.lower()
        if lower_term in known_misspellings:
            suppressed_terms.add(term)
            continue
        if " " not in term:
            suppressed_terms.add(term)
            typo = {
                "word": term,
                "fuzzy_matches": entry.get("fuzzy_matches") or [],
            }
            result.possible_typos.append(typo)
            result.unresolved_terms.append({
                "term": term,
                "type": "possible_typo",
                "exists": False,
                "reason": "possible_typo",
                "action": "clarify",
                "fuzzy_matches": typo["fuzzy_matches"],
            })
            result.resolution_map[term] = {
                "exists": False,
                "confidence": 0.0,
                "reason": "possible_typo",
                "action": "clarify",
                "fuzzy_matches": typo["fuzzy_matches"],
            }
            continue

        missing_words = [str(word).lower() for word in entry.get("words_not_in_corpus") or []]
        if any(word in known_misspellings for word in missing_words):
            suppressed_terms.add(term)
            continue
        if len(missing_words) == 1:
            suppressed_terms.add(term)
            typo = {
                "word": missing_words[0],
                "fuzzy_matches": entry.get("fuzzy_matches") or [],
            }
            result.possible_typos.append(typo)
            result.unresolved_terms.append({
                "term": typo["word"],
                "type": "possible_typo",
                "exists": False,
                "reason": "possible_typo",
                "action": "clarify",
                "fuzzy_matches": typo["fuzzy_matches"],
            })
            result.resolution_map[typo["word"]] = {
                "exists": False,
                "confidence": 0.0,
                "reason": "possible_typo",
                "action": "clarify",
                "fuzzy_matches": typo["fuzzy_matches"],
            }
            continue
        filtered_not_in_corpus.append(entry)
    result.not_in_corpus = filtered_not_in_corpus
    if suppressed_terms:
        result.unresolved_terms = [
            entry for entry in result.unresolved_terms
            if not (
                entry.get("type") == "not_in_corpus"
                and str(entry.get("term") or "") in suppressed_terms
            )
        ]
        for term in suppressed_terms:
            info = result.resolution_map.get(term)
            if info and info.get("reason") == "not_in_corpus":
                result.resolution_map.pop(term, None)

    # Step 6b: Enrich resolved controls with technique_knowledge ground truth
    if db and result.all_control_ids:
        try:
            _enrich_from_technique_knowledge(result, db)
        except Exception as exc:
            logger.error("technique_knowledge enrichment failed: {}", exc)

    # Step 7: Taxonomy extraction (optional)
    if include_taxonomy:
        try:
            from .lessons.store import extract_bridges_fast
            bridges = extract_bridges_fast(text[:500])
            if bridges:
                result.taxonomy_tags = bridges
        except Exception as exc:
            logger.error("taxonomy extraction failed: {}", exc)

    # Step 8: Build spans for sentence markup (NVIS colors)
    # Combines control IDs, phrases, misspellings, and not_in_corpus terms with positions
    # Phrases are checked against recall_items to determine if they're relevant to query
    # Classify ungrounded phrases via spaCy NER (proper nouns) + WordNet (common nouns)
    entity_labels = extract_entity_labels(text) if result.not_in_corpus else {}

    result.spans = _build_spans(
        text=text,
        control_id_spans=control_id_spans,
        control_metadata=result.control_metadata,
        misspellings=result.misspellings,
        not_in_corpus=result.not_in_corpus,
        resolution_map=result.resolution_map,
        phrases=result.phrases,
        recall_items=result.recall_items,
        query_control_ids=result.control_ids,
        aerospace_terms=getattr(result, "aerospace_terms", []),
        spacy_labels=entity_labels,
    )

    return result


def refine_entities(
    result: EntityExtractionResult,
    original_text: str,
    db=None,
    max_rounds: int = 2,
) -> EntityExtractionResult:
    """Self-correct extraction when entities don't cohere.

    If extracted controls have no shared relationship edges, try:
    1. Per-control relationship expansion
    2. Phrase + control co-occurrence search in QRAs
    3. Generate "did you mean?" suggestions
    """
    if len(result.all_control_ids) <= 1 or result.related_pairs:
        return result

    if db is None:
        try:
            from .arango_client import get_db
            db = get_db()
        except Exception as exc:
            logger.error("refine_entities failed to get db: {}", exc)
            return result

    for round_num in range(max_rounds):
        logger.debug("refine_entities round {}: {} controls, {} related",
                      round_num, len(result.all_control_ids), len(result.related_pairs))

        try:
            rel_results = list(db.aql.execute(
                """FOR r IN sparta_relationships
                     FILTER r.source_control_id IN @cids
                        AND r.target_control_id IN @cids
                     LIMIT 20
                     RETURN {
                       source: r.source_control_id,
                       target: r.target_control_id,
                       method: r.method,
                       discovered: CONCAT("round_", @round)
                     }""",
                bind_vars={"cids": result.all_control_ids, "round": str(round_num)},
            ))
            result.related_pairs.extend(rel_results)
        except Exception as exc:
            logger.error("refine relationship expansion failed: {}", exc)

        if result.related_pairs:
            return result

        if result.phrases:
            try:
                from .hybrid_search import hybrid_search_sparta_qra
                for phrase in result.phrases[:2]:
                    for cid in result.all_control_ids[:3]:
                        hits = hybrid_search_sparta_qra(db, query=f"{phrase} {cid}", k=3)
                        for hit in hits:
                            hit_text = hit.get("solution", hit.get("text", hit.get("answer", "")))
                            try:
                                hit_kp = _get_flashtext_processor(db)
                                hit_ids = sorted({cid for cid, _, _ in hit_kp.extract_keywords(hit_text, span_info=True)})
                            except Exception as exc:
                                logger.error("keyword extraction from search hits failed: {}", exc)
                                hit_ids = []
                            overlap = set(hit_ids) & set(result.all_control_ids)
                            if len(overlap) >= 2:
                                ids = list(overlap)
                                result.related_pairs.append({
                                    "source": ids[0], "target": ids[1],
                                    "method": "qra_co_occurrence",
                                    "discovered": f"round_{round_num}_phrase",
                                })
            except Exception as exc:
                logger.error("refine phrase search failed: {}", exc)

        if result.related_pairs:
            return result

    # Build "did you mean?" suggestions via neighbor lookup
    try:
        neighbors = list(db.aql.execute(
            "FOR cid IN @cids LET nb = (FOR r IN sparta_relationships "
            "FILTER r.source_control_id == cid OR r.target_control_id == cid LIMIT 5 "
            "RETURN DISTINCT (r.source_control_id == cid ? r.target_control_id : r.source_control_id)) "
            "FILTER LENGTH(nb) > 0 RETURN {control_id: cid, neighbors: nb}",
            bind_vars={"cids": result.all_control_ids},
        ))
        our_ids = set(result.all_control_ids)
        suggestions = []
        for item in neighbors:
            cid, ext = item["control_id"], [n for n in item["neighbors"] if n not in our_ids]
            if ext:
                meta = next((m for m in result.control_metadata if m.get("control_id") == cid), {})
                name = meta.get("name", cid)
                suggestions.append({"control_id": cid, "name": name, "related_to": ext[:3],
                    "clarify_question": f"'{name}' ({cid}) is related to {', '.join(ext[:3])}, "
                    f"not to the other controls you mentioned. Did you mean one of those instead?"})
        if suggestions:
            result.taxonomy_tags["_refinement_suggestions"] = suggestions
    except Exception as exc:
        logger.error("refinement suggestions failed: {}", exc)

    return result
