"""Entity extraction endpoint.

POST /extract-entities — resolve mentions in question text to canonical
control IDs. Returns a flat agent-friendly contract.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter
from loguru import logger
from pydantic import BaseModel, Field, field_validator

router = APIRouter()


class ExtractEntitiesRequest(BaseModel):
    text: str
    include_taxonomy: bool = Field(False)

    @field_validator("text")
    @classmethod
    def text_must_be_nonempty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("text must not be empty")
        return stripped


# --- Response models ---

class CrosswalkPath(BaseModel):
    exists: bool = False
    from_framework: str | None = None
    to_framework: str | None = None
    ids: list[str] = []
    terminal_framework: str | None = None
    terminal_id: str | None = None


class ResolvedEntity(BaseModel):
    mention: str
    span: list[int]
    canonical_id: str
    canonical_name: str
    framework: str
    entity_type: str
    crosswalk_path: CrosswalkPath


class ExternalEntity(BaseModel):
    mention: str
    span: list[int]
    normalized_text: str
    source: str = "nltk_wordnet"
    entity_type: str = "noun_phrase"
    wordnet_category: str | None = None
    in_security_corpus: bool = False
    routing_effect: str = "out_of_domain"


class UnresolvedEntity(BaseModel):
    mention: str
    span: list[int] | None = None
    normalized_text: str | None = None
    reason: str
    detail: str = ""


class DomainTerm(BaseModel):
    text: str
    span: list[int] | None = None
    kind: str = "domain_term"


class AgentDecision(BaseModel):
    safe_to_answer: bool = False
    needs_clarification: bool = False
    needs_retry: bool = False
    suggested_action: str | None = None
    reason: str | None = None


class Summary(BaseModel):
    resolved_count: int = 0
    external_count: int = 0
    unresolved_count: int = 0
    domain_term_count: int = 0


class TechniqueCoherence(BaseModel):
    """Whether resolved entities share a common SPARTA technique parent."""
    ok: bool = True
    detail: str = ""


class ExtractEntitiesResponse(BaseModel):
    ok: bool = True
    grounding_ok: bool = True
    query_text: str = ""
    resolved_entities: list[ResolvedEntity] = []
    external_entities: list[ExternalEntity] = []
    unresolved_entities: list[UnresolvedEntity] = []
    domain_terms: list[DomainTerm] = []
    agent_decision: AgentDecision = AgentDecision()
    technique_coherence: TechniqueCoherence = TechniqueCoherence()
    summary: Summary = Summary()


# --- Helpers ---

# WordNet categories that could plausibly be security-related
_SECURITY_WORDNET = frozenset({
    "act", "communication", "cognition", "event",
    "group", "process", "state", "attribute",
})


def _wordnet_category(term: str) -> str | None:
    """Look up WordNet lexical category. Returns e.g. 'food', 'artifact'."""
    try:
        from nltk.corpus import wordnet
        words = term.lower().split()
        for candidate in [words[-1], "_".join(words)]:
            synsets = wordnet.synsets(candidate)
            if synsets:
                lexname = synsets[0].lexname()
                return lexname.split(".")[-1] if "." in lexname else lexname
    except Exception:
        pass
    return None


def _normalize_text(term: str) -> str:
    """Normalize via WordNet lemmatizer."""
    try:
        from nltk.stem import WordNetLemmatizer
        wnl = WordNetLemmatizer()
        return " ".join(wnl.lemmatize(w) for w in term.lower().split())
    except Exception:
        return term.lower()


def _find_spans_for_mention(text: str, spans: list[dict], name: str, cid: str) -> list[tuple[str, list[int]]]:
    """Find all mention texts and span positions for a control."""
    results = []
    # First: check daemon spans
    for span in spans:
        if span.get("name") == name or span.get("text") == cid:
            sp = span.get("span", [])
            if len(sp) == 2 and sp[0] < len(text):
                results.append((text[sp[0]:sp[1]], sp))
    if results:
        return results
    # Fallback: find all occurrences of the ID in the text
    search = cid
    start = 0
    while True:
        idx = text.find(search, start)
        if idx < 0:
            break
        results.append((search, [idx, idx + len(search)]))
        start = idx + len(search)
    return results or [(cid, [])]


# --- Build response ---

def _extract_sparta_parent(cid: str) -> str:
    """Extract SPARTA parent technique from a control ID.

    SV-AC-2 → SV-AC, EX-0016.01 → EX-0016, CM0078 → CM0078
    """
    parts = cid.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    parts2 = cid.rsplit(".", 1)
    return parts2[0] if len(parts2) > 1 else cid


def _check_technique_coherence(resolved: list[ResolvedEntity]) -> TechniqueCoherence:
    """Check if resolved entities share a SPARTA technique.

    Software/malware matches (entity_type in malware/tool/software) don't count —
    they're ATT&CK software entries that match common English words.

    For SPARTA entities: extract parent technique directly from canonical_id.
    For non-SPARTA entities: use crosswalk_path.terminal_id if it reaches SPARTA.
    If no crosswalk exists (CWE→CAPEC→ATT&CK→SPARTA chain has gaps), entities
    from the same framework are still coherent; mixed frameworks without
    SPARTA crosswalk → INCONCLUSIVE.
    """
    _SOFTWARE_TYPES = {"malware", "tool", "software"}
    techniques = [e for e in resolved if e.entity_type not in _SOFTWARE_TYPES]

    if not techniques:
        return TechniqueCoherence(ok=False, detail="software_only_no_techniques")
    if len(techniques) < 2:
        return TechniqueCoherence(ok=True, detail="single_entity")

    # Collect SPARTA parent technique IDs from ALL entities
    sparta_parents: dict[str, list[str]] = {}
    no_crosswalk: list[str] = []

    for e in techniques:
        fw = e.framework or ""
        cid = e.canonical_id or ""

        if fw == "SPARTA":
            parent = _extract_sparta_parent(cid)
            sparta_parents.setdefault(parent, []).append(cid)
        elif e.crosswalk_path.exists and e.crosswalk_path.terminal_framework == "SPARTA":
            terminal = e.crosswalk_path.terminal_id or ""
            parent = _extract_sparta_parent(terminal)
            sparta_parents.setdefault(parent, []).append(f"{cid}->{terminal}")
        else:
            no_crosswalk.append(cid)

    # No entities reached SPARTA
    if not sparta_parents:
        frameworks = set(e.framework for e in techniques)
        if len(frameworks) == 1:
            return TechniqueCoherence(ok=True, detail=f"same_framework:{frameworks.pop()}")
        return TechniqueCoherence(ok=False, detail=f"no_sparta_crosswalk:{list(frameworks)}")

    # Check if all SPARTA parents converge
    if len(sparta_parents) <= 1:
        parent = list(sparta_parents.keys())[0]
        if no_crosswalk:
            return TechniqueCoherence(ok=True, detail=f"sparta_coherent:{parent},unmapped:{no_crosswalk}")
        return TechniqueCoherence(ok=True, detail=f"sparta_coherent:{parent}")

    return TechniqueCoherence(ok=False, detail=f"disjoint_sparta_parents:{list(sparta_parents.keys())}")


def _build_response(raw: dict, agent: dict, text: str) -> ExtractEntitiesResponse:
    resolved: list[ResolvedEntity] = []
    seen_ids: set[str] = set()
    spans = raw.get("spans", [])

    # Track fabricated IDs so we don't dual-classify them as external
    fabricated_mentions: set[str] = set()
    for w in agent.get("warnings", []):
        if w.get("category") == "fabricated_id":
            fabricated_mentions.add(w.get("term", "").lower())

    # Resolved entities from control_metadata
    for cm in raw.get("control_metadata", []):
        cid = cm.get("control_id", "")
        if not cid or cid in seen_ids:
            continue
        seen_ids.add(cid)

        found_spans = _find_spans_for_mention(text, spans, cm.get("name", ""), cid)
        mention, mention_span = found_spans[0]

        # Crosswalk path — derive terminal framework from taxonomy edges
        chain_path = cm.get("chain_path") or []
        from_fw = cm.get("framework", "")
        taxonomy = cm.get("taxonomy", [])
        # Find the framework of the last edge target (terminal)
        terminal_fw = None
        terminal_id = chain_path[-1] if chain_path else None
        if terminal_id and isinstance(taxonomy, list):
            for edge in taxonomy:
                if isinstance(edge, dict) and edge.get("to") == terminal_id:
                    terminal_fw = edge.get("framework")
                    break

        # Partial chains count — exists=True if chain reached at least one
        # layer beyond the seed framework (len > 1)
        if chain_path and len(chain_path) > 1:
            # Terminal is the last node in the chain
            terminal_id = chain_path[-1] if chain_path else None
            # Find terminal framework from taxonomy edges
            terminal_fw = None
            if terminal_id and isinstance(taxonomy, list):
                for edge in taxonomy:
                    if isinstance(edge, dict) and edge.get("to") == terminal_id:
                        terminal_fw = edge.get("framework")
                        break
            crosswalk = CrosswalkPath(
                exists=True,
                from_framework=from_fw,
                to_framework=terminal_fw or cm.get("chain_stop"),
                ids=chain_path,
                terminal_framework=terminal_fw,
                terminal_id=terminal_id,
            )
        else:
            crosswalk = CrosswalkPath(
                exists=False,
                from_framework=from_fw,
                to_framework=None,
                ids=chain_path,
            )

        resolved.append(ResolvedEntity(
            mention=mention,
            span=mention_span,
            canonical_id=cid,
            canonical_name=cm.get("name", cid),
            framework=from_fw,
            entity_type=cm.get("type", ""),
            crosswalk_path=crosswalk,
        ))

    # Sort by span position (earliest first)
    resolved.sort(key=lambda e: e.span[0] if e.span else 999)

    # External entities and unresolved from warnings
    external: list[ExternalEntity] = []
    unresolved: list[UnresolvedEntity] = []

    for w in agent.get("warnings", []):
        term = w.get("term", "")
        cat = w.get("category", "")

        if cat == "fabricated_id":
            # Find span in text
            idx = text.find(term)
            unresolved.append(UnresolvedEntity(
                mention=term,
                span=[idx, idx + len(term)] if idx >= 0 else None,
                normalized_text=term.lower(),
                reason="fabricated_id",
                detail=w.get("detail", "No match in corpus"),
            ))
        elif cat == "not_in_corpus":
            # Skip if this is a normalized form of a fabricated ID
            if term.lower() in fabricated_mentions or term.replace(" ", "-").lower() in fabricated_mentions:
                continue

            wn_cat = _wordnet_category(term)
            if wn_cat and wn_cat not in _SECURITY_WORDNET:
                idx = text.lower().find(term.lower())
                external.append(ExternalEntity(
                    mention=term,
                    span=[idx, idx + len(term)] if idx >= 0 else [0, 0],
                    normalized_text=_normalize_text(term),
                    wordnet_category=wn_cat,
                    routing_effect="out_of_domain",
                ))
            else:
                idx = text.find(term)
                unresolved.append(UnresolvedEntity(
                    mention=term,
                    span=[idx, idx + len(term)] if idx >= 0 else None,
                    normalized_text=term.lower(),
                    reason="not_in_corpus",
                    detail=w.get("detail", ""),
                ))

    # Domain terms — only terms NOT already covered by a resolved entity span
    resolved_spans = set()
    for ent in resolved:
        if ent.span and len(ent.span) == 2:
            for i in range(ent.span[0], ent.span[1]):
                resolved_spans.add(i)

    domain_terms: list[DomainTerm] = []
    dt_seen: set[str] = set()
    for span in spans:
        kind = span.get("kind", "")
        if kind in ("aerospace_term",) and span.get("grounded_to_corpus"):
            sp = span.get("span", [])
            # Skip if this span overlaps with a resolved entity
            if len(sp) == 2 and any(i in resolved_spans for i in range(sp[0], sp[1])):
                continue
            dt_text = text[sp[0]:sp[1]] if len(sp) == 2 and sp[0] < len(text) else span.get("text", "")
            if dt_text.lower() not in dt_seen:
                dt_seen.add(dt_text.lower())
                domain_terms.append(DomainTerm(text=dt_text, span=sp, kind="domain_term"))
    for token, info in raw.get("resolution_map", {}).items():
        if info.get("match_type") in ("domain_term", "aerospace_term") and token.lower() not in dt_seen:
            # Skip if token text is inside a resolved entity's mention
            tok_lower = token.lower()
            if any(tok_lower in ent.mention.lower() for ent in resolved):
                continue
            dt_seen.add(tok_lower)
            idx = text.lower().find(tok_lower)
            domain_terms.append(DomainTerm(
                text=token,
                span=[idx, idx + len(token)] if idx >= 0 else None,
                kind="domain_term",
            ))

    # Agent decision
    has_fabricated = any(u.reason == "fabricated_id" for u in unresolved)
    has_external = len(external) > 0
    grounding_ok = agent.get("grounding_ok", True) and not has_external

    if has_fabricated:
        decision = AgentDecision(
            safe_to_answer=False, needs_retry=True,
            suggested_action="reject_fabricated_entity", reason="fabricated_id",
        )
    elif has_external and len(resolved) > 0:
        decision = AgentDecision(
            safe_to_answer=False, needs_clarification=True,
            suggested_action="ask_clarifying_question", reason="mixed_domain_query",
        )
    elif has_external:
        decision = AgentDecision(
            safe_to_answer=False,
            suggested_action="reject_off_topic", reason="no_security_entities",
        )
    elif len(resolved) > 0:
        decision = AgentDecision(safe_to_answer=True)
    else:
        decision = AgentDecision(
            safe_to_answer=False, needs_retry=True,
            suggested_action="retry_with_more_context", reason="no_entities_found",
        )

    # Technique coherence: do resolved entities share a SPARTA parent?
    technique_coherence = _check_technique_coherence(resolved)

    return ExtractEntitiesResponse(
        ok=True,
        grounding_ok=grounding_ok,
        query_text=text,
        resolved_entities=resolved,
        external_entities=external,
        unresolved_entities=unresolved,
        domain_terms=domain_terms,
        agent_decision=decision,
        technique_coherence=technique_coherence,
        summary=Summary(
            resolved_count=len(resolved),
            external_count=len(external),
            unresolved_count=len(unresolved),
            domain_term_count=len(domain_terms),
        ),
    )


@router.post("/extract-entities", response_model=ExtractEntitiesResponse)
def extract_entities_endpoint(body: ExtractEntitiesRequest) -> ExtractEntitiesResponse:
    """Extract entities from question text using ArangoDB grounding."""
    try:
        from ...entity_extraction import extract_entities
        from ...arango_client import get_db

        db = get_db()
        result = extract_entities(body.text, db=db, include_taxonomy=body.include_taxonomy)
        raw = asdict(result)
        agent = result.agent_view() if hasattr(result, "agent_view") else {}
        return _build_response(raw, agent, body.text)
    except Exception as exc:
        logger.warning("Entity extraction failed, returning partial: {}", exc)
        try:
            from ...entity_extraction import extract_entities
            result = extract_entities(body.text, db=None, include_taxonomy=False)
            raw = asdict(result)
            agent = result.agent_view() if hasattr(result, "agent_view") else {}
            return _build_response(raw, agent, body.text)
        except Exception as exc2:
            logger.error("Entity extraction fallback failed: {}", exc2)
            return ExtractEntitiesResponse(ok=False, query_text=body.text)


# ── /create-evidence-case endpoint ──────────────────────────────────


class BuildEvidenceCaseRequest(BaseModel):
    question: str
    source_id: str | None = None  # Optional: explicit source (CWE-287, CAPEC-114)
    skip_qra_recall: bool = False  # Skip hybrid QRA search (batch mode - flashtext + graph only)
    enable_llm: bool = False  # Enable LLM phase: filter_related_qras + answer/deflect/clarify
    llm_model: str = "text"  # Model for answer/clarify/deflect (text-claude-opus for Opus)
    max_related_qras: int = 30  # Max related QRAs to fetch via lineage
    include_cae_tree: bool = False  # Include CAE tree in response (v1.3 architecture)

    @field_validator("question")
    @classmethod
    def question_must_be_nonempty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("question must not be empty")
        return stripped


def _build_evidence_fast(db, question: str, source_id: str, include_cae_tree: bool = False) -> dict:
    """Fast path for batch mode: skip entity extraction, build evidence from known control.

    When we already know the source control ID, we skip all the expensive
    entity extraction (flashtext, spellcheck, domain lookup, etc.) and
    directly look up the control + build crosswalk chains.

    Crosswalk priority (per SKILL.md):
    1. Direct CWE→SPARTA edges (SPARTA v3.1 cwe_class_ids) - 2.8K edges
    2. CWE→NIST→SPARTA (Heimdall nist_control_ids + NIST→SPARTA) - 122K edges
    3. MITRE chain CWE→CAPEC→ATT&CK→SPARTA (fallback) - sparse
    """
    cid = source_id.upper().strip()

    # Look up the source control
    aql = "FOR doc IN sparta_controls FILTER doc.control_id == @cid LIMIT 1 RETURN doc"
    cursor = db.aql.execute(aql, bind_vars={"cid": cid})
    source_docs = list(cursor)
    if not source_docs:
        return {"error": f"source_id not found: {cid}", "question": question}
    source_doc = source_docs[0]

    # Determine framework from control ID prefix
    cid_fw = None
    if cid.startswith("CWE-"):
        cid_fw = "CWE"
    elif cid.startswith("CAPEC-"):
        cid_fw = "CAPEC"
    elif cid.startswith("T") and any(c.isdigit() for c in cid):
        cid_fw = "ATT&CK"
    else:
        cid_fw = source_doc.get("source_framework", "")

    crosswalk_chains: list[dict] = []
    all_ids = {cid}

    # Priority 1: Direct CWE→SPARTA edges (check both case variants)
    direct_aql = """
    FOR e IN sparta_relationships
        FILTER e.source_control_id == @cid
        FILTER e.target_framework IN ["SPARTA", "sparta"]
        RETURN {target_id: e.target_control_id, target_fw: e.target_framework}
    """
    direct_edges = list(db.aql.execute(direct_aql, bind_vars={"cid": cid}))
    for edge in direct_edges:
        target_id = edge["target_id"]
        all_ids.add(target_id)
        crosswalk_chains.append({
            "source_id": cid,
            "source_framework": cid_fw,
            "target_id": target_id,
            "target_framework": "SPARTA",
            "method": "direct",
            "hops": [],
        })

    # Priority 2: NIST 2-hop (CWE doc has nist_control_ids → NIST→SPARTA edges)
    nist_ids = source_doc.get("nist_control_ids") or []
    if nist_ids and not crosswalk_chains:
        # Query NIST→SPARTA edges for all NIST control IDs
        nist_aql = """
        FOR e IN sparta_relationships
            FILTER e.source_control_id IN @nist_ids
            FILTER e.target_framework IN ["sparta", "SPARTA"]
            RETURN {nist_id: e.source_control_id, target_id: e.target_control_id}
        """
        nist_edges = list(db.aql.execute(nist_aql, bind_vars={"nist_ids": nist_ids}))
        for edge in nist_edges:
            nist_id = edge["nist_id"]
            target_id = edge["target_id"]
            all_ids.add(nist_id)
            all_ids.add(target_id)
            crosswalk_chains.append({
                "source_id": cid,
                "source_framework": cid_fw,
                "target_id": target_id,
                "target_framework": "SPARTA",
                "method": "nist_nvd",
                "hops": [{"id": nist_id, "framework": "NIST"}],
            })

    # Priority 3: MITRE chain fallback (CWE→CAPEC→ATT&CK→SPARTA)
    if not crosswalk_chains:
        # Multi-hop traversal up to depth 3
        mitre_aql = """
        FOR v, e, p IN 1..3 OUTBOUND @start_id sparta_relationships
            FILTER LAST(p.vertices).source_framework IN ["SPARTA", "sparta"]
               OR LAST(p.edges).target_framework IN ["SPARTA", "sparta"]
            LIMIT 10
            RETURN {
                path: p.vertices[*].control_id,
                frameworks: p.vertices[*].source_framework,
                terminal_id: LAST(p.vertices).control_id,
                terminal_fw: LAST(p.vertices).source_framework
            }
        """
        try:
            start_doc_id = f"sparta_controls/{source_doc['_key']}"
            mitre_paths = list(db.aql.execute(mitre_aql, bind_vars={"start_id": start_doc_id}))
            for mp in mitre_paths:
                path_ids = mp.get("path") or []
                frameworks = mp.get("frameworks") or []
                terminal_id = mp.get("terminal_id")
                if terminal_id and len(path_ids) > 1:
                    hops = []
                    for i, hop_id in enumerate(path_ids[1:-1], start=1):
                        fw = frameworks[i] if i < len(frameworks) else ""
                        hops.append({"id": hop_id, "framework": fw})
                        all_ids.add(hop_id)
                    all_ids.add(terminal_id)
                    crosswalk_chains.append({
                        "source_id": cid,
                        "source_framework": cid_fw,
                        "target_id": terminal_id,
                        "target_framework": "SPARTA",
                        "method": "mitre_chain",
                        "hops": hops,
                    })
        except Exception as exc:
            logger.warning("MITRE chain traversal failed for {}: {}", cid, exc)

    # Batch fetch descriptions for all control IDs
    descriptions: dict[str, dict] = {}
    if all_ids:
        desc_aql = "FOR doc IN sparta_controls FILTER doc.control_id IN @keys RETURN doc"
        desc_cursor = db.aql.execute(desc_aql, bind_vars={"keys": sorted(all_ids)})
        descriptions = {d["control_id"]: d for d in desc_cursor if d.get("control_id")}

    # Build glossary
    glossary = [{
        "id": cid,
        "name": source_doc.get("name", ""),
        "framework": cid_fw,
        "type": source_doc.get("control_type", ""),
        "description": source_doc.get("description", ""),
        "consequences": source_doc.get("consequences", ""),
    }]
    glossary_ids = {cid}

    # Add crosswalk targets to glossary
    for chain in crosswalk_chains:
        for hop in chain.get("hops", []):
            hop_id = hop.get("id")
            if hop_id and hop_id not in glossary_ids:
                hop_desc = descriptions.get(hop_id, {})
                glossary.append({
                    "id": hop_id,
                    "name": hop_desc.get("name", ""),
                    "framework": hop_desc.get("source_framework", hop.get("framework", "")),
                    "type": hop_desc.get("control_type", ""),
                    "description": hop_desc.get("description", ""),
                })
                glossary_ids.add(hop_id)
        target_id = chain.get("target_id")
        if target_id and target_id not in glossary_ids:
            target_desc = descriptions.get(target_id, {})
            glossary.append({
                "id": target_id,
                "name": target_desc.get("name", ""),
                "framework": "SPARTA",
                "type": target_desc.get("control_type", ""),
                "description": target_desc.get("description", ""),
            })
            glossary_ids.add(target_id)

    # Enrich crosswalk chain hops with descriptions
    for chain in crosswalk_chains:
        for hop in chain.get("hops", []):
            hop_id = hop.get("id")
            if hop_id and hop_id in descriptions:
                hop["name"] = descriptions[hop_id].get("name", "")
                hop["description"] = descriptions[hop_id].get("description", "")

    # Extract unique methods for top-level filtering
    methods = sorted(set(c.get("method", "") for c in crosswalk_chains if c.get("method")))

    response = {
        "question_text": question,
        "glossary": glossary,
        "crosswalk_chains": crosswalk_chains,
        "crosswalk_methods": methods,  # Top-level for /recall filtering
        "prior_qra_evidence": [],
        "cwe_record": source_doc if cid_fw == "CWE" else None,
        "target_records": [descriptions.get(c["target_id"], {}) for c in crosswalk_chains[:3]],
        "review_status": "pending",
    }

    # Build CAE tree if requested (v1.3 architecture)
    if include_cae_tree:
        try:
            from graph_memory.inference.cae_builder import build_cae_tree

            cae_tree = build_cae_tree(
                question=question,
                glossary=glossary,
                crosswalk_chains=crosswalk_chains,
                prior_qra_evidence=[],
                related_qra_evidence=[],
                shared_techniques_summary={},
            )
            response["cae_tree"] = cae_tree.model_dump()
            logger.info("create-evidence-case (fast): CAE tree built with {} arguments, traceability={}",
                       len(cae_tree.claim.arguments), cae_tree.claim.traceability.value)
        except Exception as exc:
            logger.warning("create-evidence-case (fast): CAE tree build failed: {}", exc)
            response["cae_tree"] = None
    else:
        response["cae_tree"] = None

    return response


@router.post("/create-evidence-case")
def build_evidence_case_endpoint(body: BuildEvidenceCaseRequest) -> dict:
    """Build an enriched evidence case payload from a question.

    Runs: extract-entities → recall → assemble glossary + crosswalk chains +
    QRA evidence. Returns the evidence case JSON suitable for LLM prompts.
    """
    from ...entity_extraction import extract_entities
    from ...arango_client import get_db

    db = get_db()
    question = body.question

    # FAST PATH: When source_id + skip_qra_recall are both provided (batch mode
    # with known control), skip expensive entity extraction and build evidence
    # case directly from the known control ID.
    if body.source_id and body.skip_qra_recall:
        return _build_evidence_fast(db, question, body.source_id, body.include_cae_tree)

    # Step 1: Extract entities
    # skip_daemon_recall=True because we're inside the memory service
    # (can't call Unix socket from Docker container)
    try:
        result = extract_entities(question, db=db, include_taxonomy=False, skip_daemon_recall=True)
        raw = asdict(result)
    except Exception as exc:
        logger.error("create-evidence-case: entity extraction failed: {}", exc)
        return {"error": str(exc), "question": question}

    agent = result.agent_view() if hasattr(result, "agent_view") else {}
    resp = _build_response(raw, agent, question)

    if not resp.ok:
        return {"error": "extract-entities returned ok=false", "question": question}

    resolved = [e.model_dump() if hasattr(e, "model_dump") else e for e in (resp.resolved_entities or [])]
    unresolved = [e.model_dump() if hasattr(e, "model_dump") else e for e in (resp.unresolved_entities or [])]

    if not resolved:
        return {"error": "no entities resolved", "question": question}

    # Step 2: Recall QRA evidence via existing hybrid_search_sparta_qra
    # Uses single unified view (sparta_unified_search) — no bespoke AQL
    # SKIP for batch mode (skip_qra_recall=True) — only flashtext + graph needed
    qra_items = []
    if not body.skip_qra_recall:
        try:
            from ...hybrid_search._sparta import hybrid_search_sparta_qra

            # Get control IDs from resolved entities for seed expansion
            seed_ids = [
                e.get("canonical_id", "")
                for e in resolved
                if e.get("canonical_id")
            ]

            qra_items = hybrid_search_sparta_qra(
                db,
                question[:400],
                k=12,
                include_graph=True,
                seed_control_ids=seed_ids[:5] if seed_ids else None,
            )
            # Normalize to expected schema (include lineage for related QRA lookup)
            # Filter to only include actual QRAs (have lineage with entity_ids)
            # The unified view also returns url_knowledge which don't have lineage
            qra_items = [
                {
                    "_key": r.get("_key", ""),
                    "_source": "sparta_qra",
                    "question": r.get("question", ""),
                    "answer": r.get("answer", ""),
                    "control_id": r.get("control_id"),
                    "scope": r.get("scope"),
                    "tags": r.get("tags", []),
                    "scores": {"bm25": r.get("bm25_score", 0), "combined": r.get("score", 0)},
                    "lineage": r.get("lineage", {}),
                    "evidence_case": r.get("evidence_case"),  # Cached glossary (if backfilled)
                }
                for r in qra_items
                if r.get("lineage") and r.get("lineage", {}).get("entity_ids")
            ]
        except Exception as exc:
            logger.warning("create-evidence-case: QRA recall failed: {}", exc)
            qra_items = []

    # Step 2b: Fetch related QRAs via lineage.related_qra_keys
    # These are QRAs that share SPARTA techniques with the primary QRAs (Pass 2 data)
    related_qra_evidence = []
    shared_techniques_summary = {}
    if qra_items and not body.skip_qra_recall:
        try:
            # Collect all related_qra_keys from primary QRAs
            all_related_keys = []
            key_to_techniques = {}  # Track which techniques each key shares
            for qra in qra_items:
                lineage = qra.get("lineage") or {}
                related_keys = lineage.get("related_qra_keys", [])
                techniques_map = lineage.get("shared_techniques_map", {})
                for key in related_keys[:body.max_related_qras]:
                    if key not in key_to_techniques:
                        all_related_keys.append(key)
                        key_to_techniques[key] = techniques_map.get(key, [])

            # Fetch related QRA documents
            if all_related_keys:
                related_docs = list(db.aql.execute(
                    "FOR doc IN sparta_qra FILTER doc._key IN @keys RETURN doc",
                    bind_vars={"keys": all_related_keys[:body.max_related_qras]},
                ))
                for doc in related_docs:
                    key = doc.get("_key", "")
                    related_qra_evidence.append({
                        "_key": key,
                        "question": (doc.get("question") or "")[:400],
                        "answer": (doc.get("answer") or "")[:800],
                        "control_id": doc.get("control_id"),
                        "shared_techniques": key_to_techniques.get(key, []),
                        "lineage": doc.get("lineage", {}),
                        "evidence_case": doc.get("evidence_case"),  # Cached glossary (if backfilled)
                    })
                    # Build summary of technique overlap
                    for tech in key_to_techniques.get(key, []):
                        shared_techniques_summary.setdefault(tech, []).append(key)

            logger.info("create-evidence-case: Found {} related QRAs via lineage", len(related_qra_evidence))
        except Exception as exc:
            logger.warning("create-evidence-case: Related QRA fetch failed: {}", exc)

    # Step 2c: LLM filtering of related QRAs (if enabled)
    # Uses 4 hard gates: aids_user_query, addresses_same_concern,
    # complements_not_duplicates, shares_technique_meaningfully
    llm_filter_results = None
    if body.enable_llm and qra_items and related_qra_evidence:
        try:
            from graph_memory.inference.filter_related_qras import filter_related_qras

            filter_result = filter_related_qras(
                query=question,
                primary_qra=qra_items[0],  # Use top-scoring QRA as primary
                candidates=related_qra_evidence,
                timeout_s=30.0,
            )
            # Replace with filtered results
            related_qra_evidence = [
                {**item, "gate_passed": True}
                for item in filter_result.filtered
            ]
            llm_filter_results = {
                "candidates_evaluated": filter_result.candidates_evaluated,
                "candidates_passed": len(filter_result.filtered),
                "model": filter_result.model,
            }
            logger.info("create-evidence-case: LLM filter passed {}/{} candidates",
                       len(filter_result.filtered), filter_result.candidates_evaluated)
        except Exception as exc:
            logger.warning("create-evidence-case: LLM filter failed (keeping unfiltered): {}", exc)

    # Step 3: Fetch descriptions for all control IDs
    all_ids = set()
    for e in resolved:
        cid = e.get("canonical_id", "")
        if cid:
            all_ids.add(cid)
        cp = e.get("crosswalk_path") or {}
        for hop_id in cp.get("ids", []):
            if hop_id:
                all_ids.add(hop_id)
        terminal = cp.get("terminal_id", "")
        if terminal:
            all_ids.add(terminal)

    # Step 3b: Expand glossary from QRA evidence_case (cached) or lineage (fallback)
    # Priority: 1) Use cached evidence_case.glossary if available
    #           2) Fall back to extract_entities for remaining entity_ids
    qra_lineage_ids = set()
    cached_glossary_entries = {}  # id -> glossary entry from evidence_case

    # First pass: collect from cached evidence_case.glossary
    all_qras = qra_items + related_qra_evidence
    for qra in all_qras:
        # Try cached evidence_case first
        ec = qra.get("evidence_case") or {}
        ec_glossary = ec.get("glossary") or []
        for entry in ec_glossary:
            eid = entry.get("id")
            if eid and eid not in cached_glossary_entries:
                cached_glossary_entries[eid] = entry
                all_ids.add(eid)

        # Collect lineage entity_ids for fallback
        lineage = qra.get("lineage") or {}
        for eid in lineage.get("entity_ids") or []:
            if eid:
                qra_lineage_ids.add(eid)

    # Second pass: resolve remaining entity_ids via extract_entities
    # Only for IDs not already in cached glossary
    new_ids = qra_lineage_ids - all_ids - set(cached_glossary_entries.keys())
    resolved_lineage_ids = set()
    if new_ids:
        lineage_text = " ".join(sorted(new_ids))
        try:
            lineage_result = extract_entities(lineage_text, db=db, include_taxonomy=False, skip_daemon_recall=True)
            resolved_lineage_ids = set(lineage_result.control_ids or [])
            all_ids.update(resolved_lineage_ids)
            logger.info("create-evidence-case: Resolved {} entity IDs via extract_entities (had {} cached)",
                       len(resolved_lineage_ids), len(cached_glossary_entries))
        except Exception as exc:
            logger.warning("create-evidence-case: QRA lineage entity extraction failed: {}", exc)
    elif cached_glossary_entries:
        logger.info("create-evidence-case: Using {} cached glossary entries from evidence_case",
                   len(cached_glossary_entries))

    # Threat→CM edges
    threat_ids = [
        e.get("canonical_id", "")
        for e in resolved
        if e.get("entity_type") == "space_threat" and e.get("canonical_id")
    ]
    threat_cms: dict[str, list[str]] = {}
    if threat_ids:
        try:
            aql = "FOR doc IN @@coll FILTER doc.source_control_id IN @keys RETURN doc"
            cm_docs = list(db.aql.execute(aql, bind_vars={"@coll": "sparta_relationships", "keys": threat_ids}))
            for doc in cm_docs:
                src = doc.get("source_control_id", "")
                tgt = doc.get("target_control_id", "")
                if src and tgt and tgt.startswith("CM"):
                    threat_cms.setdefault(src, []).append(tgt)
                    all_ids.add(tgt)
        except Exception as exc:
            logger.warning("create-evidence-case: CM lookup failed: {}", exc)

    # Batch fetch descriptions
    descriptions: dict[str, dict] = {}
    if all_ids:
        try:
            aql = "FOR doc IN @@coll FILTER doc.control_id IN @keys RETURN doc"
            desc_docs = list(db.aql.execute(aql, bind_vars={"@coll": "sparta_controls", "keys": sorted(all_ids)}))
            descriptions = {d["control_id"]: d for d in desc_docs if d.get("control_id")}
        except Exception as exc:
            logger.warning("create-evidence-case: description fetch failed: {}", exc)

    # Step 4: Assemble glossary
    glossary = []
    glossary_ids: set[str] = set()
    for e in resolved:
        cid = e.get("canonical_id", "?")
        desc = descriptions.get(cid, {})
        glossary.append({
            "id": cid,
            "name": e.get("canonical_name", e.get("name", "")) or desc.get("name", ""),
            "framework": e.get("framework", "") or desc.get("framework", ""),
            "type": e.get("entity_type", ""),
            "description": desc.get("description", ""),
            "consequences": desc.get("consequences", ""),
        })
        glossary_ids.add(cid)

    # Step 5: Crosswalk chains + add hop entities to glossary
    crosswalk_chains = []
    for e in resolved:
        cp = e.get("crosswalk_path") or {}
        if not cp.get("exists"):
            continue
        chain_ids = cp.get("ids", [])
        terminal_id = cp.get("terminal_id", "")
        hops = []
        for hop_id in chain_ids:
            hop_desc = descriptions.get(hop_id, {})
            hops.append({
                "id": hop_id,
                "name": hop_desc.get("name", ""),
                "framework": hop_desc.get("framework", ""),
                "description": hop_desc.get("description", ""),
            })
            if hop_id not in glossary_ids:
                glossary_ids.add(hop_id)
                glossary.append({
                    "id": hop_id,
                    "name": hop_desc.get("name", ""),
                    "framework": hop_desc.get("framework", ""),
                    "type": hop_desc.get("control_type", ""),
                    "description": hop_desc.get("description", ""),
                    "consequences": hop_desc.get("consequences", ""),
                })
        if terminal_id and terminal_id not in chain_ids:
            t_desc = descriptions.get(terminal_id, {})
            hops.append({
                "id": terminal_id,
                "name": t_desc.get("name", ""),
                "framework": t_desc.get("framework", ""),
                "description": t_desc.get("description", ""),
            })
            if terminal_id not in glossary_ids:
                glossary_ids.add(terminal_id)
                glossary.append({
                    "id": terminal_id,
                    "name": t_desc.get("name", ""),
                    "framework": t_desc.get("framework", ""),
                    "type": t_desc.get("control_type", ""),
                    "description": t_desc.get("description", ""),
                    "consequences": t_desc.get("consequences", ""),
                })
        crosswalk_chains.append({
            "from": e.get("canonical_id", "?"),
            "from_framework": cp.get("from_framework", e.get("framework", "")),
            "to_framework": cp.get("to_framework", cp.get("terminal_framework", "")),
            "hops": hops,
        })

    # Threat→CM chains
    for tid, cm_ids in threat_cms.items():
        cm_hops = []
        for cm_id in cm_ids:
            cm_desc = descriptions.get(cm_id, {})
            cm_hops.append({
                "id": cm_id, "name": cm_desc.get("name", ""),
                "framework": "SPARTA", "description": cm_desc.get("description", ""),
            })
            if cm_id not in glossary_ids:
                glossary_ids.add(cm_id)
                glossary.append({
                    "id": cm_id, "name": cm_desc.get("name", ""),
                    "framework": "SPARTA", "type": "countermeasure",
                    "description": cm_desc.get("description", ""), "consequences": "",
                })
        crosswalk_chains.append({
            "from": tid, "from_framework": "SPARTA", "to_framework": "SPARTA",
            "relationship": "threat_to_countermeasure", "hops": cm_hops,
        })

    # Step 5b: Add QRA evidence to glossary
    # Priority: 1) Cached evidence_case.glossary entries (pre-computed)
    #           2) Resolved via extract_entities (fallback for uncached QRAs)
    # Add cached glossary entries first (already have full metadata)
    for eid, entry in cached_glossary_entries.items():
        if eid not in glossary_ids:
            glossary.append({
                "id": eid,
                "name": entry.get("name", ""),
                "framework": entry.get("framework", "SPARTA"),
                "type": entry.get("type", "control"),
                "description": entry.get("description", ""),
                "consequences": entry.get("consequences", ""),
                "authority_class": "normative",
            })
            glossary_ids.add(eid)

    # Add resolved lineage IDs (from extract_entities fallback)
    for eid in resolved_lineage_ids:
        if eid not in glossary_ids:
            desc = descriptions.get(eid, {})
            if desc:
                glossary.append({
                    "id": eid,
                    "name": desc.get("name", ""),
                    "framework": desc.get("framework", "SPARTA"),
                    "type": desc.get("control_type", "control"),
                    "description": desc.get("description", ""),
                    "consequences": desc.get("consequences", ""),
                    "authority_class": "normative",
                })
                glossary_ids.add(eid)

    # CWE→SV bridge chains: when a CWE's crosswalk path ends at a SPARTA
    # threat (DE-*, LM-*, etc.) and that threat maps TO an SV-* control
    # in the glossary, add an explicit bridge chain connecting CWE→SV.
    sv_glossary_ids = {g["id"] for g in glossary if g.get("id", "").startswith("SV-")}
    if sv_glossary_ids:
        for chain in list(crosswalk_chains):
            if chain.get("from_framework") != "CWE" or chain.get("relationship"):
                continue
            # Find the terminal hop (last in chain that's a SPARTA threat)
            terminal_threats = [
                h["id"] for h in chain.get("hops", [])
                if not h["id"].startswith("CWE-") and not h["id"].startswith("T")
                and not h["id"].startswith("CAPEC-") and not h["id"].startswith("CM")
                and not h["id"].startswith("SV-")
            ]
            if not terminal_threats:
                continue
            # Look up which SV-* controls these threats map to
            try:
                aql = "FOR doc IN @@coll FILTER doc.source_control_id IN @keys RETURN doc"
                sv_docs = list(db.aql.execute(aql, bind_vars={
                    "@coll": "sparta_relationships", "keys": terminal_threats
                }))
                for doc in sv_docs:
                    tgt = doc.get("target_control_id", "")
                    if tgt in sv_glossary_ids:
                        sv_desc = descriptions.get(tgt, {})
                        crosswalk_chains.append({
                            "from": chain["from"],
                            "from_framework": "CWE",
                            "to_framework": "SPARTA",
                            "relationship": "cwe_to_countermeasure",
                            "hops": chain["hops"] + [{
                                "id": tgt,
                                "name": sv_desc.get("name", ""),
                                "framework": "SPARTA",
                                "description": sv_desc.get("description", ""),
                            }],
                        })
            except Exception as exc:
                logger.warning("create-evidence-case: CWE→SV bridge failed: {}", exc)

    # Cross-framework co-occurrence chains: if glossary has entities from
    # different frameworks but no crosswalk chain connects them, add a
    # co_occurrence chain. They were extracted from the same question,
    # which is evidence of a relationship.
    chain_frameworks = set()
    for c in crosswalk_chains:
        chain_frameworks.add((c.get("from_framework", ""), c.get("to_framework", "")))
    glossary_frameworks = {}
    for g in glossary:
        fw = g.get("framework", "")
        if fw:
            glossary_frameworks.setdefault(fw, []).append(g)
    fw_list = list(glossary_frameworks.keys())
    for i, fw_a in enumerate(fw_list):
        for fw_b in fw_list[i + 1:]:
            # Check if any existing chain connects these two frameworks
            has_chain = any(
                (f_a == fw_a and f_b == fw_b) or (f_a == fw_b and f_b == fw_a)
                for f_a, f_b in chain_frameworks
            )
            if not has_chain:
                # Pick first entity from each framework
                e_a = glossary_frameworks[fw_a][0]
                e_b = glossary_frameworks[fw_b][0]
                crosswalk_chains.append({
                    "from": e_a["id"],
                    "from_framework": fw_a,
                    "to_framework": fw_b,
                    "relationship": "co_occurrence",
                    "hops": [{
                        "id": e_b["id"],
                        "name": e_b.get("name", ""),
                        "framework": fw_b,
                        "description": e_b.get("description", ""),
                    }],
                })

    # QRA evidence - include lineage for CAE tree building
    prior_qra_evidence = []
    for q in qra_items[:12]:
        q_text = q.get("question") or q.get("problem") or ""
        a_text = q.get("answer") or q.get("solution") or ""
        prior_qra_evidence.append({
            "_key": q.get("_key", ""),
            "citation_id": q.get("control_id", "?"),
            "question": q_text[:400],
            "answer": a_text[:800],
            "lineage": q.get("lineage", {}),  # Include lineage for CAE tree
        })

    # ── Two-Stage Prompt Fields ────────────────────────────────────
    # Extract source_record (CWE/CAPEC) and target_records (SPARTA/ATT&CK/CAPEC)
    # from glossary for direct use in two-stage QRA prompts.

    source_frameworks = {"CWE", "CAPEC"}
    target_frameworks = {"SPARTA", "MITRE_ATT&CK", "ATT&CK"}  # CAPEC can be target if source is CWE

    # Find source record - use explicit source_id if provided, else first CWE/CAPEC in glossary
    source_record = None
    source_framework = None
    explicit_source = body.source_id.upper() if body.source_id else None

    for g in glossary:
        gid = g.get("id", "")
        gfw = g.get("framework", "")
        if explicit_source and gid.upper() == explicit_source:
            source_record = {
                "control_id": gid,
                "description": g.get("description", ""),
                "extended_description": g.get("consequences", ""),  # CWE consequences as extended
            }
            source_framework = gfw
            break
        elif gfw in source_frameworks and source_record is None:
            source_record = {
                "control_id": gid,
                "description": g.get("description", ""),
                "extended_description": g.get("consequences", ""),
            }
            source_framework = gfw

    # Extract target_records - all non-source entities from target frameworks
    target_records = []
    seen_target_ids = set()
    source_id_upper = source_record["control_id"].upper() if source_record else ""

    for g in glossary:
        gid = g.get("id", "")
        gfw = g.get("framework", "")
        # Normalize ATT&CK variants
        if gfw == "ATT&CK":
            gfw = "MITRE_ATT&CK"
        # CAPEC can be target if source is CWE
        allowed_targets = target_frameworks.copy()
        if source_framework == "CWE":
            allowed_targets.add("CAPEC")

        if gfw in allowed_targets and gid.upper() != source_id_upper and gid not in seen_target_ids:
            seen_target_ids.add(gid)
            target_records.append({
                "control_id": gid,
                "framework": gfw,
                "description": g.get("description", ""),
            })

    # Build response with two-stage prompt fields
    response = {
        "question_text": question,
        "review_status": "passed" if qra_items else "inconclusive",
        "glossary": glossary,
        "crosswalk_chains": crosswalk_chains,
        "prior_qra_evidence": prior_qra_evidence,
        # Lineage-based evidence (Pass 2 data)
        "related_qra_evidence": related_qra_evidence,
        "shared_techniques_summary": shared_techniques_summary,
    }

    # Add LLM filter metadata if LLM phase was enabled
    if llm_filter_results:
        response["llm_filter_results"] = llm_filter_results

    # Add two-stage prompt fields if source was found
    if source_record:
        if source_framework == "CWE":
            response["cwe_record"] = source_record
        elif source_framework == "CAPEC":
            response["capec_record"] = source_record
        response["target_records"] = target_records

    # LLM response decision (if enabled)
    if body.enable_llm:
        # Determine action based on evidence quality
        has_grounding = bool(crosswalk_chains) or bool(prior_qra_evidence)
        has_related = bool(related_qra_evidence)

        if not has_grounding and not has_related:
            llm_action = "CLARIFY"
            llm_reason = "No grounding evidence found - need clarifying question"
        elif not crosswalk_chains and prior_qra_evidence:
            llm_action = "ANSWER"
            llm_reason = "QRA evidence available but no framework crosswalk"
        elif crosswalk_chains and has_related:
            llm_action = "ANSWER"
            llm_reason = "Strong evidence: crosswalk chains + related QRAs with shared techniques"
        else:
            llm_action = "ANSWER"
            llm_reason = "Partial evidence available"

        response["llm_decision"] = {
            "action": llm_action,
            "reason": llm_reason,
            "evidence_strength": {
                "has_crosswalk_chains": bool(crosswalk_chains),
                "has_prior_qra": bool(prior_qra_evidence),
                "has_related_qra": has_related,
                "shared_technique_count": len(shared_techniques_summary),
            },
        }

        # Generate LLM response (answer/clarify/deflect)
        try:
            from graph_memory.inference.evidence_case_response import generate_evidence_case_response

            llm_response = generate_evidence_case_response(
                action=llm_action,
                question=question,
                glossary=glossary,
                crosswalk_chains=crosswalk_chains,
                prior_qra_evidence=prior_qra_evidence,
                related_qra_evidence=related_qra_evidence,
                shared_techniques_summary=shared_techniques_summary,
                timeout_s=45.0,
                model=body.llm_model,
            )
            response["llm_response"] = llm_response.model_dump(exclude_none=True)
            logger.info("create-evidence-case: LLM response generated (action={}, latency={}ms)",
                       llm_action, llm_response.latency_ms)
        except Exception as exc:
            logger.warning("create-evidence-case: LLM response generation failed: {}", exc)
            response["llm_response"] = {"action": llm_action, "error": str(exc)}

    # Build CAE tree if requested (v1.3 architecture)
    if body.include_cae_tree:
        try:
            from graph_memory.inference.cae_builder import build_cae_tree

            # Build CAE tree from the evidence case data
            # prior_qra_evidence needs to be converted back to full QRA format
            full_qra_items = qra_items if qra_items else []
            cae_tree = build_cae_tree(
                question=question,
                glossary=glossary,
                crosswalk_chains=crosswalk_chains,
                prior_qra_evidence=full_qra_items,
                related_qra_evidence=related_qra_evidence,
                shared_techniques_summary=shared_techniques_summary,
            )
            response["cae_tree"] = cae_tree.model_dump()
            logger.info("create-evidence-case: CAE tree built with {} arguments, traceability={}",
                       len(cae_tree.claim.arguments), cae_tree.claim.traceability.value)
        except Exception as exc:
            logger.warning("create-evidence-case: CAE tree build failed: {}", exc)
            response["cae_tree"] = None
    else:
        response["cae_tree"] = None

    return response


# ---------------------------------------------------------------------------
# Batch Evidence Case Endpoint (for lineage backfill)
# ---------------------------------------------------------------------------


class BatchEvidenceCaseItem(BaseModel):
    question: str
    source_id: str = ""
    skip_qra_recall: bool = True  # Default True for batch mode


class BatchEvidenceCaseRequest(BaseModel):
    items: list[BatchEvidenceCaseItem]
    max_workers: int = Field(8, ge=1, le=32)


@router.post("/create-evidence-case-batch")
def build_evidence_case_batch(body: BatchEvidenceCaseRequest) -> dict:
    """Process multiple evidence case requests in parallel.

    Optimized for lineage backfill where source_id is known.
    Uses ThreadPoolExecutor for parallel processing within the daemon.

    Returns: {results: [{question, evidence, error?}, ...], processed, errors}
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ...arango_client import get_db

    db = get_db()
    results = []
    errors = 0

    def process_one(item: BatchEvidenceCaseItem) -> dict:
        try:
            if item.source_id and item.skip_qra_recall:
                # Fast path
                evidence = _build_evidence_fast(db, item.question, item.source_id)
            else:
                # Full path (expensive)
                req = BuildEvidenceCaseRequest(
                    question=item.question,
                    source_id=item.source_id,
                    skip_qra_recall=item.skip_qra_recall,
                )
                evidence = build_evidence_case_endpoint(req)
            return {"question": item.question, "source_id": item.source_id, "evidence": evidence}
        except Exception as exc:
            logger.warning("batch evidence case failed for {}: {}", item.source_id, exc)
            return {"question": item.question, "source_id": item.source_id, "error": str(exc)}

    with ThreadPoolExecutor(max_workers=body.max_workers) as executor:
        futures = {executor.submit(process_one, item): item for item in body.items}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if "error" in result:
                errors += 1

    return {
        "results": results,
        "processed": len(results),
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Bulk Lineage Backfill (batched AQL, not per-doc queries)
# ---------------------------------------------------------------------------

class BulkLineageRequest(BaseModel):
    """Request for bulk lineage backfill - batched graph queries."""
    control_ids: list[str] = Field(..., description="List of control IDs to process")


@router.post("/build-lineage-bulk")
def build_lineage_bulk(body: BulkLineageRequest) -> dict:
    """Build crosswalk chains for many control IDs using batched AQL.

    Instead of N queries per control (slow), this does ~4 total queries:
    1. Batch fetch all source controls
    2. Batch fetch all direct edges (IN + COLLECT)
    3. Batch fetch all NIST edges (IN + COLLECT)
    4. Assemble chains in Python

    Returns: {results: {control_id: {chains: [...], glossary: [...]}}, processed, errors}
    """
    from ...arango_client import get_db
    import time

    db = get_db()
    cids = [cid.upper().strip() for cid in body.control_ids if cid]
    if not cids:
        return {"results": {}, "processed": 0, "errors": 0}

    results: dict[str, dict] = {}
    errors = 0

    # --- Query 1: Batch fetch all source controls ---
    source_docs: dict[str, dict] = {}
    try:
        cursor = db.aql.execute(
            "FOR doc IN sparta_controls FILTER doc.control_id IN @cids RETURN doc",
            bind_vars={"cids": cids},
        )
        for doc in cursor:
            cid = doc.get("control_id")
            if cid:
                source_docs[cid] = doc
    except Exception as exc:
        logger.error("Bulk lineage: source fetch failed: {}", exc)
        return {"results": {}, "processed": 0, "errors": len(cids), "error": str(exc)}

    # --- Query 2: Batch fetch all direct edges (CWE/CAPEC→SPARTA) ---
    direct_edges: dict[str, list] = {cid: [] for cid in cids}
    try:
        cursor = db.aql.execute("""
            FOR e IN sparta_relationships
                FILTER e.source_control_id IN @cids
                FILTER e.target_framework IN ["SPARTA", "sparta"]
                COLLECT source_id = e.source_control_id INTO edges = e
                RETURN {source_id, edges}
        """, bind_vars={"cids": cids})
        for row in cursor:
            sid = row.get("source_id")
            if sid:
                direct_edges[sid] = row.get("edges", [])
    except Exception as exc:
        logger.warning("Bulk lineage: direct edges query failed: {}", exc)

    # --- Query 3: Batch fetch all NIST edges (for CWEs with nist_control_ids) ---
    # First collect all NIST IDs from source docs
    nist_to_cwe: dict[str, list[str]] = {}  # nist_id -> [cwe_ids that reference it]
    for cid, doc in source_docs.items():
        for nist_id in (doc.get("nist_control_ids") or []):
            nist_to_cwe.setdefault(nist_id, []).append(cid)

    nist_edges: dict[str, list] = {}  # nist_id -> edges
    if nist_to_cwe:
        try:
            nist_ids = list(nist_to_cwe.keys())
            cursor = db.aql.execute("""
                FOR e IN sparta_relationships
                    FILTER e.source_control_id IN @nist_ids
                    FILTER e.target_framework IN ["SPARTA", "sparta"]
                    COLLECT source_id = e.source_control_id INTO edges = e
                    RETURN {source_id, edges}
            """, bind_vars={"nist_ids": nist_ids})
            for row in cursor:
                sid = row.get("source_id")
                if sid:
                    nist_edges[sid] = row.get("edges", [])
        except Exception as exc:
            logger.warning("Bulk lineage: NIST edges query failed: {}", exc)

    # --- Assemble chains for each control ID ---
    all_target_ids: set[str] = set()
    for cid in cids:
        if cid not in source_docs:
            results[cid] = {"error": "not_found", "chains": [], "glossary": []}
            errors += 1
            continue

        source_doc = source_docs[cid]
        cid_fw = "CWE" if cid.startswith("CWE-") else (
            "CAPEC" if cid.startswith("CAPEC-") else source_doc.get("source_framework", "")
        )

        chains: list[dict] = []

        # Priority 1: Direct edges
        for edge in direct_edges.get(cid, []):
            target_id = edge.get("target_control_id")
            if target_id:
                all_target_ids.add(target_id)
                chains.append({
                    "source_id": cid,
                    "source_framework": cid_fw,
                    "target_id": target_id,
                    "target_framework": "SPARTA",
                    "method": "direct",
                    "hops": [],
                })

        # Priority 2: NIST 2-hop (if no direct edges)
        if not chains:
            for nist_id in (source_doc.get("nist_control_ids") or []):
                for edge in nist_edges.get(nist_id, []):
                    target_id = edge.get("target_control_id")
                    if target_id:
                        all_target_ids.add(target_id)
                        chains.append({
                            "source_id": cid,
                            "source_framework": cid_fw,
                            "target_id": target_id,
                            "target_framework": "SPARTA",
                            "method": "nist_nvd",
                            "hops": [{"id": nist_id, "framework": "NIST"}],
                        })

        results[cid] = {
            "chains": chains,
            "source_framework": cid_fw,
            "source_name": source_doc.get("name", ""),
        }

    # --- Query 4: Batch fetch target control descriptions ---
    if all_target_ids:
        try:
            cursor = db.aql.execute(
                "FOR doc IN sparta_controls FILTER doc.control_id IN @ids RETURN doc",
                bind_vars={"ids": list(all_target_ids)},
            )
            target_docs = {d["control_id"]: d for d in cursor if d.get("control_id")}

            # Enrich results with target info
            for cid, res in results.items():
                if "error" in res:
                    continue
                for chain in res.get("chains", []):
                    tid = chain.get("target_id")
                    if tid and tid in target_docs:
                        chain["target_name"] = target_docs[tid].get("name", "")
        except Exception as exc:
            logger.warning("Bulk lineage: target fetch failed: {}", exc)

    return {
        "results": results,
        "processed": len(cids),
        "with_chains": sum(1 for r in results.values() if r.get("chains")),
        "errors": errors,
    }
