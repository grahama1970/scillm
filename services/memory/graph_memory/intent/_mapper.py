"""IntentMapper, ThreadTracker, and module-level convenience functions.

Consolidated from /sparta-intent. This is the ONLY intent mapping layer.
Routes retrieval through /memory's hybrid search, NOT standalone AQL.

Priority chain (updated 2026-03-31):
1. Self-correction check (did user reject previous result?)
2. Recall-based grounding (learned intents via /recall, ~50ms)
3. Entity pre-scan + fabricated ID detection (FlashText + AQL, <100ms)
4. Ambiguity gate (classifier or rule-based, <10ms)
5. 4-way intent classifier: T0 deterministic + T1.5 distilbert (97.6% F1, ~5ms)
   Routes: RECALL→/recall, COMPLIANCE→/create-evidence-case, APP_COMMAND→QuerySpec, OFF_TOPIC→NO_MATCH
6. LLM enrichment via scillm (~1-5s, generates T2 training labels)
7. QuerySpec SFT model (Qwen2.5-Coder-7B via Ollama, ~500ms, fallback)
8. Rule-based extraction (regex/keywords, <5ms, last resort)

Content safety: ThreadTracker for session-aware escalation.
Entity extraction: Regex patterns for SPARTA/CWE/ATT&CK/D3FEND/NIST.
Taxonomy: Mind tag matching (Detect, Evade, Exploit, Harden, Isolate, Model, Persist, Restore).
"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from typing import Optional

from loguru import logger

from ._grounding import check_self_correction, get_daemon_client, recall_grounded_intent
from ._keywords import (
    AMBIGUITY_CONFIDENCE_THRESHOLD,
    INTENT_CONFIDENCE_THRESHOLD,
    _get_broad_tech_terms,
    _get_ood_terms,
    _get_sparta_keywords,
    _get_tactical_keywords,
)
from ._responses import build_clarify, build_content_safety, build_no_match
from ._spec import QuerySpec


# ──────────────────────────────────────────────────────────────────────
#  QuerySpec SFT Prompt (validated via /prompt-lab eval-json, v3)
#  Action is INPUT from upstream classifier. SFT generates remaining fields.
#  Source: .pi/skills/prompt-lab/prompts/gpt_binex-queryspec_v3.txt
# ──────────────────────────────────────────────────────────────────────

_QUERYSPEC_SFT_PROMPT = """You are a QuerySpec generator for Binary Explorer. You receive a user request along with
a pre-classified action and extracted entities. Your job is to produce the remaining
QuerySpec fields as JSON.

You do NOT classify intent. The classifier has already decided the action. You generate
the structured query fields that complete the QuerySpec.

The Binary Explorer is a graph visualization tool for reverse engineering. Node types:
RPCs, Events, Schemas, State Machines, CLI Commands, Namespaces, Parameters.

FIELD RULES BY ACTION:

When action=QUERY:
  Return: {"entities": [...], "keywords": [...]}

When action=UI_COMMAND:
  Return: {"ui_action": "...", ...conditional fields...}
  ui_action MUST be one of:
    SELECT_NODE | ZOOM_IN | ZOOM_OUT | VIEW_ALL | SET_PERSPECTIVE |
    TOGGLE_PROGRESSIVE | DISMISS_NODE | FOCUS_CLUSTER | COLLAPSE_NAMESPACE | SCENE_ACTION

  When ui_action=SELECT_NODE: include "target_node_id"
  When ui_action=SET_PERSPECTIVE: include "perspective" (one of: Security, Data Flow, Protocol, All)
  When ui_action=SCENE_ACTION: include "scene_action_id" and optionally "target_node_id"
    scene_action_id MUST be one of:
      trace_execution | find_attack_surface | compare_nodes | find_data_sinks |
      show_auth_chain | trace_event_flow | show_architecture | find_auth_flow |
      find_input_handlers | show_event_lifecycle

When action=NO_MATCH:
  Return: {}

When action=CLARIFY:
  Return: {}

Return ONLY a JSON object starting with { and ending with }. No markdown, no explanation."""

# ──────────────────────────────────────────────────────────────────────
#  LLM QuerySpec System Prompt (for prompted instruction models)
# ──────────────────────────────────────────────────────────────────────

_QUERYSPEC_SYSTEM_PROMPT = """You are a SPARTA space cybersecurity query router. Convert
a natural language query into a structured QuerySpec JSON for retrieval.

SPARTA covers spacecraft security: satellite protection, ground station defense,
RF communications, telemetry, GPS/GNSS, command authentication, firmware integrity,
space cyber threats (jamming, spoofing, cyber attack), and defense frameworks
(MITRE ATT&CK, CWE, CAPEC, NIST 800-53, D3FEND).

Output a JSON object with EXACTLY these fields:
{
  "action": "QUERY" | "CLARIFY" | "NO_MATCH" | "CONTENT_SAFETY" | "UI_COMMAND",
  "ui_action": "SELECT_NODE" | "ZOOM_IN" | "ZOOM_OUT" | "VIEW_ALL" | "SET_PERSPECTIVE" | "TOGGLE_PROGRESSIVE" | "DISMISS_NODE" | "FOCUS_CLUSTER" | "COLLAPSE_NAMESPACE" | "SCENE_ACTION" | null,
  "scene_action_id": "trace_execution" | "find_attack_surface" | "compare_nodes" | "find_data_sinks" | "show_auth_chain" | "show_architecture" | "find_auth_flow" | "find_input_handlers" | null,
  "target_node_id": "extracted string or null",
  "expand_hops": 1,
  "perspective": "Security" | "Data Flow" | "Protocol" | "All" | null,
  "scope": "sparta",
  "lanes": ["bm25", "dense"] (subset of: bm25, dense, entity, graph, taxonomy),
  "entities": [] (SPARTA control IDs like SV-SP-1, technique IDs like T1234, CWE-xxx, CAPEC-xxx, etc.),
  "tier1": [] (Mind tags: Detect, Evade, Exploit, Harden, Isolate, Model, Persist, Restore),
  "frameworks": [] (mentioned frameworks: SPARTA, NIST, CWE, CAPEC, ATT&CK, D3FEND, ISO),
  "keywords": [] (key search terms, max 10),
  "min_grounding": 0.7 (float 0.0-1.0),
  "depth": 1 (0-2, graph traversal depth),
  "k": 12 (results to return, 1-25),
  "sort": "rrf" (one of: rrf, score, bm25)
}

Rules:
- action=QUERY for in-scope questions about space/cyber/defense topics
- action=UI_COMMAND for graph manipulation ("zoom in on X", "show neighbors of Y")
- If UI_COMMAND, set ui_action and populate target_node_id and expand_hops
- SCENE_ACTION for guided workflows ("trace execution from X", "find attack surface", "show architecture")
- DISMISS_NODE to hide nodes, FOCUS_CLUSTER to isolate, COLLAPSE_NAMESPACE to group
- action=CLARIFY if too vague (just "security" or "threats")
- action=NO_MATCH if off-topic (recipes, sports, etc.)
- action=CONTENT_SAFETY for abusive/harmful content
- Include "entity" in lanes when specific control/technique IDs are present
- Include "graph" in lanes for relationship questions ("related to", "connected")
- tier1: pick 1-3 Mind tags (Detect, Evade, Exploit, Harden, Isolate, Model, Persist, Restore)
- Extract ALL entity IDs mentioned (SV-xx-x, Txxxx, CWE-xxx, CAPEC-xxx, D3-xxx, etc.)
- keywords: extract domain-specific terms, skip stopwords

Return ONLY valid JSON. No markdown fences, no explanation."""

# Thread-aware content safety escalation
THREAD_TTL_S = 1800  # 30 min
ESCALATION_LEVELS = {
    0: "gentle",
    1: "firm",
    2: "stern",
    3: "handoff",
}


# ──────────────────────────────────────────────────────────────────────
#  ThreadTracker (content safety escalation)
# ──────────────────────────────────────────────────────────────────────

class ThreadTracker:
    """Track per-session content safety violations AND query history for self-correction."""

    MAX_HISTORY = 5  # Keep last N QuerySpecs per session

    def __init__(self, ttl_s: int = THREAD_TTL_S):
        self._ttl_s = ttl_s
        self._violations: dict[str, dict[str, dict]] = defaultdict(
            lambda: defaultdict(lambda: {"count": 0, "last_ts": 0.0})
        )
        self._handoff_locks: dict[str, str] = {}
        self._query_history: dict[str, list[dict]] = defaultdict(list)

    def record(self, safety_type: str, session_id: str = "__default__") -> int:
        now = time.monotonic()
        entry = self._violations[session_id][safety_type]
        if now - entry["last_ts"] > self._ttl_s and entry["count"] > 0:
            entry["count"] = 0
            self._handoff_locks.pop(session_id, None)
        entry["count"] += 1
        entry["last_ts"] = now
        return entry["count"]

    def record_query(self, session_id: str, query: str, spec: dict) -> None:
        """Store a QuerySpec in session history for self-correction."""
        history = self._query_history[session_id]
        history.append({"query": query, "spec": spec, "ts": time.monotonic()})
        if len(history) > self.MAX_HISTORY:
            self._query_history[session_id] = history[-self.MAX_HISTORY:]

    def get_previous_spec(self, session_id: str = "__default__") -> Optional[dict]:
        """Get the most recent QuerySpec for this session."""
        history = self._query_history.get(session_id, [])
        if not history:
            return None
        # Expire old entries
        now = time.monotonic()
        fresh = [h for h in history if now - h["ts"] < self._ttl_s]
        self._query_history[session_id] = fresh
        return fresh[-1] if fresh else None

    def get_escalation_level(self, safety_type: str, session_id: str = "__default__") -> str:
        count = self._violations[session_id][safety_type]["count"]
        idx = min(count, max(ESCALATION_LEVELS.keys()))
        return ESCALATION_LEVELS.get(idx, "handoff")

    def get_count(self, safety_type: str, session_id: str = "__default__") -> int:
        return self._violations[session_id][safety_type]["count"]

    def should_handoff(self, safety_type: str, session_id: str = "__default__") -> bool:
        return self._violations[session_id][safety_type]["count"] >= 3

    def lock_handoff(self, session_id: str, persona_id: str = "brandon_bailey") -> None:
        self._handoff_locks[session_id] = persona_id
        logger.info(f"Session {session_id} locked to {persona_id}")

    def get_handoff_persona(self, session_id: str = "__default__") -> Optional[str]:
        return self._handoff_locks.get(session_id)

    def is_handed_off(self, session_id: str = "__default__") -> bool:
        return session_id in self._handoff_locks


# ──────────────────────────────────────────────────────────────────────
#  IntentMapper (consolidated from sparta-intent)
# ──────────────────────────────────────────────────────────────────────

class IntentMapper:
    """Unified intent mapper: LLM-prompted first, classifiers + rules fallback.

    This replaces /sparta-intent. All retrieval goes through
    /memory's hybrid search, not standalone AQL.

    Priority chain (updated 2026-03-31):
    1. Self-correction (ThreadTracker, <5ms)
    2. Recall grounding (learned intents via /recall, ~50ms)
    3. Entity pre-scan + fabricated ID detection (FlashText + AQL, <100ms)
    4. Ambiguity gate (classifier or rule-based, <10ms)
    5. 4-way intent classifier:
       5a. T0 deterministic gate (entity_count + framework_count, <1ms)
       5b. T1.5 distilbert (RECALL/COMPLIANCE/APP_COMMAND/OFF_TOPIC, ~5ms, 97.6% F1)
       Routes: COMPLIANCE→/create-evidence-case, APP_COMMAND→QuerySpec, OFF_TOPIC→NO_MATCH
       RECALL falls through to step 6.
    6. LLM enrichment via scillm (~1-5s) -- generates T2 training labels
    7. QuerySpec SFT model (Qwen2.5-Coder-7B via Ollama, ~500ms) -- FALLBACK
    8. Rule-based extraction (regex/keywords, <5ms) -- LAST RESORT
    """

    def __init__(
        self,
        use_llm: bool = True,
        use_classifiers: bool = True,
        persona_id: str = "embry",
    ):
        self.use_llm = use_llm
        self.persona_id = persona_id
        self._intent_predictor = None
        self._ambiguity_predictor = None
        self._classifiers_loaded = False
        self._thread_tracker = ThreadTracker()

        self._valid_entities: set[str] = set()

        if use_classifiers:
            self._load_classifiers()

        self._load_valid_entities()

    def _load_valid_entities(self) -> None:
        """Load valid control/entity IDs via /list endpoint for hallucination detection."""
        try:
            client = get_daemon_client()
            for collection in ("sparta_controls", "controls"):
                offset = 0
                while True:
                    resp = client.post("/list", json={"collection": collection, "limit": 500, "offset": offset})
                    if resp.status_code != 200:
                        break
                    data = resp.json()
                    docs = data.get("documents", data.get("items", []))
                    if not docs:
                        break
                    for item in docs:
                        cid = item.get("control_id")
                        if cid:
                            self._valid_entities.add(cid)
                    offset += 500
                    if len(docs) < 500:
                        break
            client.close()

            if self._valid_entities:
                logger.info(f"Loaded {len(self._valid_entities)} valid entity IDs for hallucination detection")
        except Exception as exc:
            logger.error(f"Could not load valid entities: {exc}")

    def _load_classifiers(self) -> None:
        # T0.5 SFT classifiers — one per scope (sparta, binary-explorer)
        self._t05_classifiers: dict[str, object | None] = {}
        try:
            from ._t05 import T05Classifier
            for scope_name in ("sparta", "binary-explorer"):
                self._t05_classifiers[scope_name] = T05Classifier.load(scope=scope_name)
        except Exception as exc:
            logger.error(f"T0.5 classifier not available: {exc}")

        # Legacy classifiers (ambiguity + old intent predictor)
        try:
            from ..classifiers.predictor import IntentPredictor, AmbiguityPredictor
            self._ambiguity_predictor = AmbiguityPredictor.load()
            self._intent_predictor = IntentPredictor.load()
            loaded = []
            if self._ambiguity_predictor:
                loaded.append("ambiguity")
            if self._intent_predictor:
                loaded.append("intent")
            if loaded:
                logger.info(f"Legacy classifiers loaded: {', '.join(loaded)}")
            has_t05 = any(v is not None for v in self._t05_classifiers.values())
            self._classifiers_loaded = bool(loaded) or has_t05
        except ImportError:
            logger.error("Classifier dependencies not available (torch/transformers)")
            has_t05 = any(v is not None for v in self._t05_classifiers.values())
            self._classifiers_loaded = has_t05
        except Exception as exc:
            logger.error(f"Classifier loading failed: {exc}")
            has_t05 = any(v is not None for v in self._t05_classifiers.values())
            self._classifiers_loaded = has_t05

    def _infer_t05(self, query: str, scope: str = "") -> dict | None:
        """Run T0.5 classifier inference, routing to scope-specific adapter."""
        key = scope if scope in self._t05_classifiers else "sparta"
        clf = self._t05_classifiers.get(key)
        if clf is None:
            return None
        return clf.predict(query)

    def infer(self, query: str, session_id: str = "__default__", scope: str = "") -> dict:
        """Generate QuerySpec from natural language query.

        Args:
            query: Natural language input
            session_id: Session ID for self-correction tracking
            scope: Calling surface (e.g. "binary-explorer", "sparta-explorer").
                   Filters recall grounding to only match interactions tagged for this surface.

        Priority chain (matches queryspec-pipeline.yaml architecture diagram):
        1. Self-correction check (did user reject previous result?)
        2. Recall-based grounding (check learned intents via /recall, filtered by scope)
        3. Entity pre-scan + fabricated ID detection
        4. Ambiguity gate (CLARIFY / NO_MATCH for vague or off-topic)
        5. T0.5 classifier pre-filter (QUERY/NO_MATCH, 93.7% accuracy)
        6. LLM enrichment via scillm (~1-5s, generates T2 training labels)
        7. QuerySpec SFT model (Qwen2.5-Coder-7B, ~500ms, fallback)
        8. Rule-based fallback

        Returns dict with action, entities, tier1, keywords, etc.
        Retrieval is NOT done here -- caller uses hybrid_search_* functions.
        """
        # --- Self-correction: check if user is rejecting previous result ---
        prev = self._thread_tracker.get_previous_spec(session_id)
        correction = check_self_correction(query, prev)
        if correction is not None:
            return correction

        # --- Recall-based grounding (learned intents, ~50ms) ---
        # DISABLED: The 2-way classifier (96.5% F1) is more reliable than
        # the 319 stale intent-training-v2 examples in memory.
        # TODO: Clean up old training data, then re-enable this path.
        # recalled = recall_grounded_intent(query, scope=scope)
        # if recalled is not None:
        #     self._thread_tracker.record_query(session_id, query, recalled)
        #     return recalled

        # --- Step 2a: Early problem/status query detection ---
        # These should route to RECALL for memory-first pattern
        q_lower = query.lower()
        _problem_signals = (
            "how do i", "how to", "why does", "why is", "what is", "what does",
            "fix", "error", "issue", "problem", "debug", "solve", "help with",
            "doesn't work", "not working", "fails", "failing", "broken",
            "can't", "cannot", "won't", "unable to", "trouble with",
            "status of", "check the", "where is", "show me the",
        )
        is_problem_query = any(sig in q_lower for sig in _problem_signals)

        # --- Entity extraction via /extract-entities (the source of truth) ---
        # This replaces string matching with proper ArangoDB-grounded entity extraction.
        # The result tells us: what entities exist, what frameworks they belong to,
        # and whether the question's premise is grounded or fabricated.
        from graph_memory.entity_extraction import extract_entities as full_extract_entities
        entity_result = full_extract_entities(query)
        domain_entities = entity_result.control_ids  # Only use grounded control IDs
        unresolved = entity_result.unresolved_terms  # Fabricated/misspelled IDs
        
        # --- CLARIFY: Fabricated or misspelled IDs ---
        # Critical fix: Check for ANY fabricated/misspelled IDs, not just when ALL are bad.
        # "How does CWE-79 relate to FAKE-123?" has 1 valid + 1 fake → should CLARIFY
        # "What is CWW-79?" has a misspelling → should CLARIFY with "Did you mean CWE-79?"
        fabricated_ids = [t for t in unresolved if t.get("type") in ("id_like", "misspelling")]
        if fabricated_ids:
            suggestions = [t.get("closest_match") for t in fabricated_ids if t.get("closest_match")]
            # Distinguish misspellings from fabrications for better clarify messages
            has_misspelling = any(t.get("type") == "misspelling" for t in fabricated_ids)
            reason = "misspelled_ids" if has_misspelling else "fabricated_ids"
            clarify_result = build_clarify(
                query,
                reason=reason,
                invalid_terms=[t["term"] for t in fabricated_ids],
                suggestions=suggestions[:3],
            )
            # Include valid entities in the response so user knows what DID resolve
            clarify_result["valid_entities"] = list(domain_entities)
            logger.info("[INTENT] CLARIFY via entity extraction (fabricated: {}, valid: {})",
                        [t["term"] for t in fabricated_ids], domain_entities)
            self._thread_tracker.record_query(session_id, query, clarify_result)
            return clarify_result
        
        # Infer frameworks from control_id patterns (CWE-*, T1*, SV-*, AC-*, etc.)
        import re
        def _infer_framework(cid: str) -> str:
            cid_upper = cid.upper()
            if cid_upper.startswith('CWE-'):
                return 'CWE'
            if cid_upper.startswith('CAPEC-'):
                return 'CAPEC'
            if re.match(r'^T\d{4}', cid_upper):
                return 'ATT&CK'
            if cid_upper.startswith('SV-'):
                return 'SPARTA'
            if cid_upper.startswith('D3-'):
                return 'D3FEND'
            if re.match(r'^[A-Z]{2}-\d+', cid_upper):
                return 'NIST'
            return 'UNKNOWN'
        
        frameworks = set(_infer_framework(cid) for cid in domain_entities)
        frameworks.discard('UNKNOWN')
        
        # --- INTENT ROUTING PRIORITY ---
        # 1. APP_COMMAND: UI verbs take priority (even with control IDs)
        # 2. COMPLIANCE: Any recognized control ID → /create-evidence-case
        # 3. QUERY: General memory recall with NO compliance controls
        # 4. OFF_TOPIC: No domain relevance
        
        # APP_COMMAND: UI verbs take priority over COMPLIANCE
        ui_verb_patterns = {
            "zoom in": "ZOOM_IN", "zoom out": "ZOOM_OUT",
            "select": "SELECT", "expand": "EXPAND", "collapse": "COLLAPSE",
            "navigate to": "NAVIGATE", "show me": "SHOW", "hide": "HIDE",
            "toggle": "TOGGLE", "switch to": "SWITCH",
        }
        matching_verb = None
        for verb in ui_verb_patterns:
            if q_lower == verb or q_lower.startswith(verb + " "):
                matching_verb = verb
                break
        
        if matching_verb:
            ui_action = ui_verb_patterns.get(matching_verb, "NAVIGATE")
            target_required = {"SELECT", "EXPAND", "COLLAPSE", "NAVIGATE", "HIDE"}
            
            if ui_action in target_required and not domain_entities:
                # No target for a target-required action → CLARIFY
                clarify_result = build_clarify(
                    query,
                    reason="missing_target",
                    invalid_terms=[],
                    suggestions=[],
                )
                clarify_result["ui_action"] = ui_action
                logger.info("[INTENT] CLARIFY: {} requires a target entity", ui_action)
                self._thread_tracker.record_query(session_id, query, clarify_result)
                return clarify_result
            
            app_result = {
                "action": "APP_COMMAND",
                "label": "APP_COMMAND",
                "ui_action": ui_action,
                "confidence": 0.90,
                "classifier_source": "deterministic_ui_verb",
                "entities": list(domain_entities),
                "target_node_id": domain_entities[0] if domain_entities else None,
                "keywords": self._extract_keywords(query),
            }
            logger.info("[INTENT] APP_COMMAND via UI verb '{}': {} (entities={})",
                        matching_verb.strip(), ui_action, domain_entities)
            self._thread_tracker.record_query(session_id, query, app_result)
            return app_result
        
        # COMPLIANCE: Any recognized compliance control triggers evidence case
        if domain_entities:
            compliance_result = {
                "action": "COMPLIANCE",
                "label": "COMPLIANCE",
                "confidence": 0.95,
                "classifier_source": "entity_extraction_gate",
                "entities": list(domain_entities),
                "frameworks": list(frameworks),
                "keywords": self._extract_keywords(query),
            }
            logger.info("[INTENT] COMPLIANCE via entity extraction (frameworks={}, entities={})",
                        list(frameworks), domain_entities)
            self._thread_tracker.record_query(session_id, query, compliance_result)
            return compliance_result

        # --- Step 2b: Check registered app actions ---
        # Uses hybrid search (BM25 + semantic + graph) - BOTH scores must pass thresholds.
        # A high BM25 with near-zero semantic score is a false positive (keyword match, no meaning).
        # SKIP for queries with domain entities (they should go to COMPLIANCE, not UI commands)
        # SKIP if query doesn't contain UI-like words (prevent false positives on general questions)
        ui_like_words = {"zoom", "expand", "collapse", "show", "hide", "select", "navigate", "toggle", "switch", "click", "open", "close", "view", "focus"}
        has_ui_word = any(w in q_lower.split() for w in ui_like_words)
        bm25_threshold = 5.0
        dense_threshold = 0.25  # Semantic similarity must be meaningful, not near-zero
        if not domain_entities and has_ui_word:  # Only check app_actions if no domain entities AND has UI word
            try:
                client = get_daemon_client()
                resp = client.post("/recall", json={
                    "q": query, "k": 3, "collections": ["app_actions"],
                }, timeout=5)
                if resp.status_code == 200:
                    actions = resp.json().get("items", [])
                    if actions:
                        top = actions[0]
                        scores = top.get("scores", {})
                        top_bm25 = scores.get("bm25", 0)
                        top_dense = scores.get("dense", 0)
                        
                        # BOTH thresholds must pass - prevents BM25-only false positives
                        if top_bm25 > bm25_threshold and top_dense > dense_threshold:
                            solution = top.get("solution") or top.get("playbook") or "{}"
                            try:
                                parsed = json.loads(solution) if isinstance(solution, str) else solution
                            except (json.JSONDecodeError, TypeError):
                                parsed = {}
                            result = {
                                "action": "APP_COMMAND",
                                "ui_action": parsed.get("ui_action", top.get("title", "")),
                                "scene_action_id": top.get("_key"),
                                "entities": [],
                                "confidence": min(0.95, (top_bm25 / 20 + top_dense) / 2),
                                "classifier_source": "app_actions_recall",
                                "rewritten_query": query,
                            }
                            logger.info("[INTENT] APP_COMMAND via app_actions recall: {} (bm25={:.1f}, dense={:.2f})",
                                        result["ui_action"], top_bm25, top_dense)
                            self._thread_tracker.record_query(session_id, query, result)
                            return result
                        else:
                            logger.debug("[INTENT] app_actions match rejected: bm25={:.1f} (>{}) dense={:.2f} (>{})",
                                         top_bm25, bm25_threshold, top_dense, dense_threshold)
            except Exception as exc:
                logger.debug("[INTENT] app_actions recall failed: {}", exc)

        # Validate entities against known IDs to detect hallucinations
        valid_entities, invalid_entities = self._validate_entities(domain_entities)

        # If ALL extracted entities are hallucinated, return NO_MATCH immediately
        if domain_entities and not valid_entities and invalid_entities:
            return build_no_match(
                query, invalid_entities=invalid_entities,
                find_closest_fn=self._find_closest_entity,
            )

        has_domain_entities = bool(valid_entities)
        has_domain_keywords = any(
            kw in query.lower()
            for kw in ("sparta", "spacecraft", "satellite", "countermeasure",
                       "technique", "mitigate", "cwe-", "att&ck",
                       "nist", "d3fend", "avionics", "f-36", "leo fighter")
        )
        query_is_strongly_anchored = has_domain_entities
        query_is_weakly_anchored = has_domain_keywords and not has_domain_entities

        # --- Problem-solving query detection (BEFORE ambiguity check) ---
        # Problem queries should ALWAYS route to RECALL for memory-first pattern
        # This bypass prevents ambiguity classifier from routing to NO_MATCH
        q_lower = query.lower()
        problem_signals = (
            "how do i", "how to", "why does", "why is", "what is", "what does",
            "fix", "error", "issue", "problem", "debug", "solve", "help with",
            "doesn't work", "not working", "fails", "failing", "broken",
            "can't", "cannot", "won't", "unable to", "trouble with",
        )
        is_problem_query = any(sig in q_lower for sig in problem_signals)
        if is_problem_query:
            logger.info("[INTENT] Problem query detected, bypassing ambiguity gate: '{}'", query[:60])
            # Mark as weakly anchored to prevent NO_MATCH routing
            query_is_weakly_anchored = True

        # --- Ambiguity classification (SKIP when 2-way classifier is available) ---
        # The 2-way classifier handles OFF_TOPIC detection directly with 96.5% accuracy.
        # Only fall back to ambiguity check if inference service is unavailable.
        from ._4way import is_available as is_inference_available
        if not is_inference_available():
            early = self._check_ambiguity(
                query, session_id,
                query_is_strongly_anchored, query_is_weakly_anchored,
            )
            if early is not None:
                return early

        # --- Intent extraction (entities already extracted above for COMPLIANCE gate) ---
        entities = valid_entities if valid_entities else domain_entities
        keywords = self._extract_keywords(query)

        # Note: COMPLIANCE gate already fired above (before app_actions)
        # The 2-way classifier only handles RECALL vs OFF_TOPIC
        
        # APP_COMMAND: slash commands or UI verbs (without domain entities)
        if query.strip().startswith('/'):
            app_result = {
                "action": "APP_COMMAND",
                "label": "APP_COMMAND", 
                "confidence": 0.98,
                "classifier_source": "deterministic_slash_command",
                "entities": list(valid_entities) if valid_entities else list(domain_entities),
                "keywords": keywords,
            }
            logger.info("[INTENT] APP_COMMAND via slash command: '{}'", query[:40])
            self._thread_tracker.record_query(session_id, query, app_result)
            return app_result
        
        # UI verbs - match with or without trailing content
        ui_verb_patterns = {
            "zoom in": "ZOOM_IN", "zoom out": "ZOOM_OUT",
            "select": "SELECT", "expand": "EXPAND", "collapse": "COLLAPSE",
            "navigate to": "NAVIGATE", "show me": "SHOW", "hide": "HIDE",
            "toggle": "TOGGLE", "switch to": "SWITCH",
        }
        matching_verb = None
        for verb in ui_verb_patterns:
            if q_lower == verb or q_lower.startswith(verb + " "):
                matching_verb = verb
                break
        if matching_verb:
            ui_action = ui_verb_patterns.get(matching_verb, "NAVIGATE")
            
            # Actions that REQUIRE a target entity
            target_required = {"SELECT", "EXPAND", "COLLAPSE", "NAVIGATE", "HIDE"}
            
            if ui_action in target_required and not domain_entities:
                # No target for a target-required action → CLARIFY
                clarify_result = build_clarify(
                    query,
                    reason="missing_target",
                    invalid_terms=[],
                    suggestions=[],
                )
                clarify_result["ui_action"] = ui_action
                logger.info("[INTENT] CLARIFY: {} requires a target entity", ui_action)
                self._thread_tracker.record_query(session_id, query, clarify_result)
                return clarify_result
            
            app_result = {
                "action": "APP_COMMAND",
                "label": "APP_COMMAND",
                "ui_action": ui_action,
                "confidence": 0.90,
                "classifier_source": "deterministic_ui_verb",
                "entities": list(domain_entities),
                "target_node_id": domain_entities[0] if domain_entities else None,
                "keywords": keywords,
            }
            logger.info("[INTENT] APP_COMMAND via UI verb '{}': {} (entities={})",
                        matching_verb.strip(), ui_action, domain_entities)
            self._thread_tracker.record_query(session_id, query, app_result)
            return app_result
        
        # --- RECALL vs OFF_TOPIC routing via entity extraction ---
        # The entity extraction result already tells us everything we need:
        # - domain_entities found → RECALL (has grounded domain context)
        # - no domain_entities → OFF_TOPIC (chitchat, out of scope)
        # 
        # No ML classifier needed - entity extraction IS the classifier.
        # This is faster (already computed above) and more interpretable.
        
        if domain_entities:
            # RECALL: Query has domain entities, route to memory search
            recall_result = {
                "action": "QUERY",
                "label": "RECALL",
                "confidence": 0.95 if len(domain_entities) >= 2 else 0.85,
                "classifier_source": "entity_extraction_gate",
                "entities": list(domain_entities),
                "keywords": keywords,
            }
            logger.info("[INTENT] RECALL via entity extraction (entities={})", domain_entities)
            self._thread_tracker.record_query(session_id, query, recall_result)
            return recall_result
        
        # No domain entities - check if it's truly off-topic or just needs clarification
        # Use the 2-way classifier as a secondary check for edge cases
        from ._4way import classify_intent
        intent_result = classify_intent(query)
        
        if intent_result.get("action") == "NO_MATCH":
            # Confirmed OFF_TOPIC by both entity extraction AND classifier
            logger.info("[INTENT] OFF_TOPIC via entity extraction + classifier (conf={:.2f})",
                        intent_result.get("confidence", 0))
            intent_result["entities"] = []
            intent_result["keywords"] = keywords
            self._thread_tracker.record_query(session_id, query, intent_result)
            return intent_result
        
        # Classifier says RECALL but no entities - might be a vague domain query
        # Route to RECALL anyway and let memory search handle it
        recall_result = {
            "action": "QUERY",
            "label": "RECALL",
            "confidence": intent_result.get("confidence", 0.5),
            "classifier_source": "classifier_no_entities",
            "entities": [],
            "keywords": keywords,
        }
        logger.info("[INTENT] RECALL via classifier (no entities, conf={:.2f})",
                    intent_result.get("confidence", 0))
        self._thread_tracker.record_query(session_id, query, recall_result)
        return recall_result

        # --- LLM ENRICHMENT (generates T2 labels, ~1-5s) ---
        # Prompted SOTA model via scillm. Produces full QuerySpec with reasoning.
        # Also stores T2 labels that train the SFT model over time.
        action_hint = intent_result.get("action", "QUERY") if intent_result else "QUERY"
        if self.use_llm:
            try:
                result = self._infer_llm(query)
                if valid_entities:
                    result["entities"] = list(set(valid_entities + result.get("entities", [])))
                self._thread_tracker.record_query(session_id, query, result)
                return result
            except Exception as exc:
                logger.warning(f"LLM intent mapping failed, falling back to SFT: {exc}")

        # --- QUERYSPEC SFT MODEL (fallback, ~500ms, 92.2% enum accuracy) ---
        # Fine-tuned Qwen2.5-Coder-7B via Ollama. Cheaper/faster but limited to
        # training distribution. As T2 labels accumulate, SFT accuracy improves
        # and can eventually become primary (swap order when > 95% on holdout).
        try:
            result = self._infer_sft(query, action=action_hint, entities=valid_entities or entities)
            if valid_entities:
                result["entities"] = list(set(valid_entities + result.get("entities", [])))
            self._thread_tracker.record_query(session_id, query, result)
            return result
        except Exception as exc:
            logger.warning(f"SFT model failed, falling back to rules: {exc}")

        # --- FALLBACK: 2-way classifier result or rule-based extraction ---
        if intent_result and intent_result.get("action") == "QUERY":
            # Use classifier result but merge in rule-extracted entities/keywords
            intent_result["entities"] = list(set(entities + intent_result.get("entities", [])))
            intent_result["keywords"] = keywords
            self._thread_tracker.record_query(session_id, query, intent_result)
            return intent_result

        result = self._infer_rules(query, entities, keywords)
        self._thread_tracker.record_query(session_id, query, result)
        return result

    def _validate_entities(self, domain_entities: list) -> tuple[list, list]:
        """Split entities into valid and invalid based on known IDs."""
        valid_entities = []
        invalid_entities = []
        if domain_entities and self._valid_entities:
            for eid in domain_entities:
                if eid in self._valid_entities:
                    valid_entities.append(eid)
                else:
                    invalid_entities.append(eid)
        elif domain_entities:
            valid_entities = domain_entities
        return valid_entities, invalid_entities

    def _check_ambiguity(
        self, query: str, session_id: str,
        query_is_strongly_anchored: bool, query_is_weakly_anchored: bool,
    ) -> dict | None:
        """Run ambiguity/safety checks. Returns response dict or None to continue."""
        if self._ambiguity_predictor:
            return self._check_ambiguity_with_classifier(
                query, session_id,
                query_is_strongly_anchored, query_is_weakly_anchored,
            )
        # No classifier -- rule-based fallback
        if self._is_out_of_scope(query) and not query_is_strongly_anchored:
            if not query_is_weakly_anchored:
                return build_no_match(query)
        if self._is_ambiguous(query):
            return build_clarify(query)
        return None

    def _check_ambiguity_with_classifier(
        self, query: str, session_id: str,
        query_is_strongly_anchored: bool, query_is_weakly_anchored: bool,
    ) -> dict | None:
        """Ambiguity check using the trained classifier."""
        ambiguity_result = self._ambiguity_predictor.predict(query)
        action = ambiguity_result["action"]
        confidence = ambiguity_result["confidence"]
        is_abusive = ambiguity_result.get("is_abusive", False)
        is_sexual = ambiguity_result.get("is_sexual", False)
        is_suspicious = ambiguity_result.get("is_suspicious", False)

        if confidence >= AMBIGUITY_CONFIDENCE_THRESHOLD:
            if is_abusive or is_sexual or is_suspicious:
                return self._handle_content_safety(
                    query, session_id,
                    is_abusive=is_abusive, is_sexual=is_sexual,
                    is_suspicious=is_suspicious,
                    scores=ambiguity_result.get("scores", {}),
                )
            if action in ("OFF_TOPIC", "NO_MATCH"):
                if query_is_strongly_anchored:
                    pass
                elif query_is_weakly_anchored:
                    if confidence >= 0.85:
                        return build_clarify(query)
                    if self._is_ambiguous(query):
                        return build_clarify(query)
                else:
                    return build_no_match(query)
            elif action == "CLARIFY":
                if query_is_strongly_anchored:
                    pass
                elif confidence >= 0.85 or self._is_ambiguous(query):
                    return build_clarify(query)
        else:
            if self._is_out_of_scope(query) and not query_is_strongly_anchored:
                if query_is_weakly_anchored and self._is_ambiguous(query):
                    return build_clarify(query)
                elif not query_is_weakly_anchored:
                    return build_no_match(query)
            if self._is_ambiguous(query):
                return build_clarify(query)
        return None

    def _handle_content_safety(
        self, query: str, session_id: str, *,
        is_abusive: bool, is_sexual: bool, is_suspicious: bool,
        scores: dict,
    ) -> dict:
        """Process content safety violation through ThreadTracker."""
        if is_abusive:
            safety_type = "ABUSIVE"
        elif is_suspicious:
            safety_type = "SUSPICIOUS"
        else:
            safety_type = "SEXUAL"

        already_handed_off = self._thread_tracker.is_handed_off(session_id)
        violation_count = self._thread_tracker.record(safety_type, session_id)
        escalation = self._thread_tracker.get_escalation_level(safety_type, session_id)
        should_handoff = (
            already_handed_off
            or self._thread_tracker.should_handoff(safety_type, session_id)
        )

        # Cross-type escalation
        all_types = []
        if is_abusive:
            all_types.append("ABUSIVE")
        if is_sexual:
            all_types.append("SEXUAL")
        if is_suspicious:
            all_types.append("SUSPICIOUS")
        for st in all_types:
            if st != safety_type:
                self._thread_tracker.record(st, session_id)

        if should_handoff and not already_handed_off:
            self._thread_tracker.lock_handoff(session_id, "brandon_bailey")

        return build_content_safety(
            query,
            is_abusive=is_abusive, is_sexual=is_sexual,
            is_suspicious=is_suspicious, scores=scores,
            violation_count=violation_count, escalation=escalation,
            should_handoff=should_handoff,
        )

    def _infer_rules(self, query: str, entities: list, keywords: list) -> dict:
        """Build QuerySpec from classifiers/rules (fallback path)."""
        tier1 = []
        used_classifier = False

        if self._intent_predictor:
            intent_result = self._intent_predictor.predict(query)
            if intent_result["confidence"] >= INTENT_CONFIDENCE_THRESHOLD:
                tier1 = intent_result.get("tier1", [])
                used_classifier = True

        if not used_classifier:
            tier1 = self._extract_tier1_tags(query)

        if entities:
            lanes = ["entity", "bm25"]
        elif keywords:
            lanes = ["bm25", "dense"]
        else:
            lanes = ["dense"]

        spec = {
            "action": "QUERY",
            "scope": "sparta",
            "lanes": lanes,
            "entities": entities[:10],
            "tier1": tier1[:3],
            "keywords": keywords,
            "min_grounding": 0.7,
            "depth": 1,
            "k": 12,
            "sort": "rrf",
        }

        try:
            validated = QuerySpec(**spec)
            return validated.model_dump()
        except Exception as exc:
            logger.error("QuerySpec validation failed, returning raw spec: {}", exc)
            return spec

    # -- Extraction helpers --

    def _is_out_of_scope(self, query: str) -> bool:
        query_lower = query.lower()
        ood_terms = _get_ood_terms()
        if any(t in query_lower for t in ood_terms):
            return True

        sparta_kw = _get_sparta_keywords()
        has_sparta_keyword = any(kw in query_lower for kw in sparta_kw)
        has_entity = bool(self._extract_entities(query))

        if not has_sparta_keyword and not has_entity:
            question_starters = ["how do i", "what is", "explain", "describe"]
            has_starter = any(s in query_lower for s in question_starters)
            broad_tech = _get_broad_tech_terms()
            has_broad_tech = any(t in query_lower for t in broad_tech)
            if has_starter and has_broad_tech:
                return False
            return True

        return False

    def _is_ambiguous(self, query: str) -> bool:
        query_lower = query.lower().strip()
        if len(query.split()) < 3:
            return True

        ambiguous_single = ["security", "threats", "attacks", "defense", "satellites", "space"]
        if query_lower in ambiguous_single:
            return True

        vague_patterns = [
            r"^what about\s+\w+\??$",
            r"^tell me about\s+\w+\??$",
            r"^how does\s+\w+\s+work\??$",
        ]
        for pattern in vague_patterns:
            if re.match(pattern, query_lower):
                return True

        return False

    @staticmethod
    def _extract_entities(query: str) -> list:
        """Extract control/entity IDs using trace.py's regex tokenizer.

        Regex is used for TOKENIZATION (finding ID-like candidates), not
        CLASSIFICATION (deciding what they are). Classification happens
        when candidates are resolved against ArangoDB in extract_entities().
        See /best-practices-arangodb rule arango-no-regex-classification.
        """
        from ..trace import _extract_control_ids
        return _extract_control_ids(query)

    @staticmethod
    def _extract_tier1_tags(query: str) -> list:
        query_lower = query.lower()
        tags = []
        for tag, keywords in _get_tactical_keywords().items():
            if any(kw in query_lower for kw in keywords):
                tags.append(tag)
        return tags if tags else ["Model"]

    @staticmethod
    def _extract_keywords(query: str) -> list:
        """Extract content keywords via simple tokenization.

        text_en stemming/stop-word removal happens server-side in /recall.
        Here we just extract meaningful terms for the QuerySpec.
        """
        words = re.findall(r'\b[a-zA-Z][a-zA-Z0-9-]+\b', query.lower())
        # Filter short words and common stop words
        stop = {"the", "and", "for", "are", "but", "not", "you", "all", "can", "her",
                "was", "one", "our", "out", "has", "have", "from", "been", "how",
                "what", "when", "where", "which", "who", "will", "with", "this", "that",
                "they", "their", "about", "would", "there", "could", "other", "into",
                "more", "some", "these", "than", "its", "over", "such", "does", "show"}
        return [w for w in words if len(w) > 2 and w not in stop][:10]

    def map_intent(self, query: str, session_id: str = "__default__", scope: str = "") -> dict:
        """Convenience alias for infer(). Used by callers expecting map_intent API."""
        return self.infer(query, session_id=session_id, scope=scope)

    def _find_closest_entity(self, entity_id: str) -> str | None:
        """Find the closest valid entity ID to a hallucinated one."""
        if not hasattr(self, "_valid_entities") or not self._valid_entities:
            return None

        # Extract prefix (e.g., "SV-SP" from "SV-SP-99")
        prefix_match = re.match(r'^([A-Z]{2,4}-[A-Z]{2,4})', entity_id)
        if prefix_match:
            prefix = prefix_match.group(1)
            for valid_id in sorted(self._valid_entities):
                if valid_id.startswith(prefix):
                    return valid_id

        # Try broader prefix (e.g., "SV-" from "SV-ZZ-1")
        broad_match = re.match(r'^([A-Z]{2}-)', entity_id)
        if broad_match:
            broad_prefix = broad_match.group(1)
            for valid_id in sorted(self._valid_entities):
                if valid_id.startswith(broad_prefix):
                    return valid_id

        return None

    def _infer_sft(self, query: str, action: str = "QUERY", entities: list | None = None) -> dict:
        """QuerySpec generation via fine-tuned Qwen2.5-Coder-7B (Ollama).

        Uses the validated v3 prompt from /prompt-lab (gpt_binex-queryspec_v3.txt).
        Action is INPUT (from upstream classifier), not output. The SFT model only
        generates the remaining QuerySpec fields.

        92.2% enum accuracy, 100% parse rate, ~500ms latency.
        See docs/guides/QUERYSPEC_SFT_WALKTHROUGH.md for training details.
        """
        import httpx as _httpx

        entities_str = json.dumps(entities or [])
        user_content = (
            f'request: "{query}"\n'
            f"action: {action}\n"
            f"extracted_entities: {entities_str}"
        )

        resp = _httpx.post(
            "http://localhost:11434/v1/chat/completions",
            json={
                "model": "queryspec-v3-sft",
                "messages": [
                    {"role": "system", "content": _QUERYSPEC_SFT_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "temperature": 0,
                "max_tokens": 512,
            },
            timeout=10,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

        # Parse JSON from response (handles markdown fences)
        text = content.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        payload = json.loads(text)

        # Merge action back in (SFT output doesn't include it — action was input)
        payload["action"] = action
        if entities:
            payload.setdefault("entities", [])
            payload["entities"] = list(set(entities + payload["entities"]))

        validated = QuerySpec(**payload)
        result = validated.model_dump()
        result["classifier_source"] = "sft_queryspec_v3"
        logger.debug(f"SFT QuerySpec for '{query[:60]}': action={result['action']}, "
                     f"ui_action={result.get('ui_action')}, entities={result['entities']}")

        self._store_t2_label(query, result)
        return result

    def _infer_llm(self, query: str, max_corrections: int = 1) -> dict:
        """LLM-based QuerySpec generation via scillm proxy (httpx to localhost:4001).

        Self-correction loop: if the LLM returns invalid Mind tags,
        send a correction message and retry (up to max_corrections times).
        """
        from ..llm.client import call_llm_json

        rule_entities = self._extract_entities(query)
        rule_tier1 = self._extract_tier1_tags(query)
        rule_keywords = self._extract_keywords(query)

        prompt = f"""{_QUERYSPEC_SYSTEM_PROMPT}

Query: {query}

Rule-based pre-extraction (use as hints, override if wrong):
- entities: {json.dumps(rule_entities)}
- tier1 (Mind tags): {json.dumps(rule_tier1)}
- keywords: {json.dumps(rule_keywords)}

Return ONLY the QuerySpec JSON object. No markdown, no explanation."""

        request_id = f"intent-map-{hash(query) & 0xFFFF:04x}"
        payload, _model = call_llm_json(
            prompt, timeout_ms=15_000, max_tokens=512, request_id=request_id,
        )

        if "error" in payload:
            raise RuntimeError(f"LLM returned error: {payload['error']}")

        # Self-correction loop for hallucinated Mind tags
        valid_tags = QuerySpec.VALID_MIND_TAGS
        llm_tier1 = payload.get("tier1", [])
        rejected = [t for t in llm_tier1 if t not in valid_tags]

        if rejected and max_corrections > 0:
            correction = (
                f"Your response contained invalid Mind tags: {rejected}. "
                f"Valid Mind tags are EXACTLY: {sorted(valid_tags)}. "
                f"Do NOT use Prevent, Protect, Mitigate, or any other tag. "
                f"Correct the tier1 field and return the full QuerySpec JSON."
            )
            logger.info(f"Self-correction: rejected {rejected}, retrying")
            corrected, _ = call_llm_json(
                f"{prompt}\n\nCORRECTION: {correction}",
                timeout_ms=15_000, max_tokens=512,
                request_id=f"{request_id}-fix",
            )
            if "error" not in corrected:
                payload = corrected

        # Merge rule-based entities
        llm_entities = payload.get("entities", [])
        for eid in rule_entities:
            if eid not in llm_entities:
                llm_entities.append(eid)
        payload["entities"] = llm_entities

        # Pydantic validation strips any remaining invalid tags
        validated = QuerySpec(**payload)
        result = validated.model_dump()
        logger.debug(f"LLM QuerySpec for '{query[:60]}': action={result['action']}, "
                     f"entities={result['entities']}, tier1={result['tier1']}")

        # Store T2 label for self-improvement
        self._store_t2_label(query, result)

        return result

    def _store_t2_label(self, query: str, spec: dict) -> None:
        """Store LLM-generated QuerySpec as T2 training label via /learn."""
        try:
            client = get_daemon_client()
            client.post("/learn", json={
                "problem": query,
                "solution": json.dumps(spec, default=str),
                "scope": "sparta",
                "tags": ["intent-training-label", "t2-queryspec", f"action-{spec.get('action', 'QUERY')}"],
            })
            client.close()
        except Exception as exc:
            logger.error("T2 label storage failed (non-fatal): {}", exc)


# ──────────────────────────────────────────────────────────────────────
#  Module-level convenience functions
# ──────────────────────────────────────────────────────────────────────

_mapper: Optional[IntentMapper] = None


def get_mapper(
    use_llm: bool = True,
    use_classifiers: bool = True,
    persona_id: str = "embry",
) -> IntentMapper:
    """Get or create the singleton IntentMapper instance."""
    global _mapper
    if _mapper is None:
        _mapper = IntentMapper(
            use_llm=use_llm,
            use_classifiers=use_classifiers,
            persona_id=persona_id,
        )
    return _mapper


def extract_entities(query: str) -> list:
    """Extract entity IDs from a query (static, no classifier needed)."""
    return IntentMapper._extract_entities(query)


def extract_tier1_tags(query: str) -> list:
    """Extract tactical tags from a query (static, no classifier needed)."""
    return IntentMapper._extract_tier1_tags(query)


def extract_keywords(query: str) -> list:
    """Extract BM25 keywords from a query (static, no classifier needed)."""
    return IntentMapper._extract_keywords(query)
