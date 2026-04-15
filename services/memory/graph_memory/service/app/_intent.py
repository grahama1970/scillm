"""Unified intent classification endpoint.

POST /intent — single entry point for NL→structured query translation.

Delegates to IntentMapper (graph_memory.intent) which handles:
- Entity extraction (control IDs, CWE, CAPEC, ATT&CK)
- Ambiguity/safety classification
- LLM QuerySpec generation (via scillm)
- Trained classifier fallback
- Keyword heuristic fallback

Returns a unified response with:
- QuerySpec fields (action, entities, frameworks, ui_action, etc.)
- query_plan: concrete httpx call steps for the caller to execute
- scope/confidence for persona routing

Callers (Binary Explorer, SPARTA Explorer, CLI) use query_plan.steps
to execute the right daemon endpoints without knowing the internals.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from fastapi import APIRouter
from loguru import logger
from pydantic import BaseModel, Field, field_validator

router = APIRouter()


# ── App Context Helpers ───────────────────────────────────────────────────────


def _check_app_actions(query: str, app: str) -> dict | None:
    """Check if query matches registered app_actions for the given app.
    
    Uses BM25 search against app_actions collection filtered by app.
    Returns action details if a strong match is found, None otherwise.
    """
    try:
        from ...arango_client import get_db
        from ...entity_extraction import extract_entities
        
        db = get_db()
        if not db.has_collection("app_actions"):
            return None
        
        # Extract entities from query for target resolution
        entity_result = extract_entities(query)
        entities = entity_result.control_ids
        
        # Search app_actions for this app
        # AQL: search descriptions, match app filter, return best match
        aql = """
        FOR doc IN app_actions_search
            SEARCH ANALYZER(doc.description IN TOKENS(@query, 'text_en'), 'text_en')
            FILTER doc.app == @app
            SORT BM25(doc) DESC
            LIMIT 3
            RETURN {
                _key: doc._key,
                action: doc.action,
                element_id: doc.element_id,
                description: doc.description,
                score: BM25(doc)
            }
        """
        cursor = db.aql.execute(aql, bind_vars={"query": query, "app": app})
        results = list(cursor)
        
        if not results:
            return None
        
        top = results[0]
        score = top.get("score", 0)
        
        # Require reasonable BM25 score (threshold tunable)
        if score < 2.0:
            logger.debug("[APP_ACTIONS] No strong match for '{}' in app={} (best score={})",
                        query[:40], app, score)
            return None
        
        # Return action with target entity if available
        return {
            "action": top.get("action"),
            "element_id": top.get("element_id"),
            "target": entities[0] if entities else None,
            "entities": list(entities),
            "score": score,
        }
        
    except Exception as exc:
        logger.debug("[APP_ACTIONS] Check failed: {}", exc)
        return None


@router.get("/app-actions")
def list_app_actions(app: str | None = None) -> dict:
    """List registered app_actions, optionally filtered by app.
    
    Use this to discover what UI commands are available for an app.
    Apps register their actions on boot; this endpoint lets you inspect them.
    """
    try:
        from ...arango_client import get_db
        db = get_db()
        
        if not db.has_collection("app_actions"):
            return {"items": [], "total": 0}
        
        if app:
            aql = """
            FOR doc IN app_actions
                FILTER doc.app == @app
                SORT doc.action
                RETURN {
                    _key: doc._key,
                    app: doc.app,
                    action: doc.action,
                    element_id: doc.element_id,
                    description: doc.description
                }
            """
            cursor = db.aql.execute(aql, bind_vars={"app": app})
        else:
            aql = """
            FOR doc IN app_actions
                SORT doc.app, doc.action
                RETURN {
                    _key: doc._key,
                    app: doc.app,
                    action: doc.action,
                    element_id: doc.element_id,
                    description: doc.description
                }
            """
            cursor = db.aql.execute(aql)
        
        items = list(cursor)
        return {"items": items, "total": len(items)}
        
    except Exception as exc:
        logger.error("[APP_ACTIONS] List failed: {}", exc)
        return {"items": [], "total": 0, "error": str(exc)}


# ── Request / Response models ─────────────────────────────────────────────────


class IntentRequest(BaseModel):
    """Query to classify."""

    q: str
    scope: str = ""
    session_id: str = Field("api", description="Session ID for self-correction tracking")
    fast: bool = Field(False, description="Skip LLM, use keyword routing only")
    app: str | None = Field(None, description="App context (e.g., 'binary-explorer') for UI command routing")

    @field_validator("q")
    @classmethod
    def q_must_be_nonempty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("query must not be empty or whitespace-only")
        return stripped


class QueryStep(BaseModel):
    """A single httpx call in a query plan."""

    endpoint: str
    params: dict[str, Any] = {}


class QueryPlan(BaseModel):
    """Concrete httpx call plan derived from QuerySpec."""

    strategy: str
    steps: list[QueryStep] = []
    extracted_entities: list[str] = []
    max_hops: int | None = None
    start_node: str | None = None


class IntentResponse(BaseModel):
    """Unified response merging QuerySpec + query_plan.

    Backward compatible: scope, query_type, confidence, rewritten_query still present.
    New: query_plan (httpx steps), action, entities, ui_action, frameworks.
    """

    # Core routing (backward compat)
    scope: str = "brandon_bailey"
    query_type: str = "knowledge"
    confidence: float = 0.5
    classifier_source: str = "keyword_heuristic"
    rewritten_query: str = ""

    # QuerySpec fields (from IntentMapper)
    action: str = "QUERY"
    entities: list[str] = []
    frameworks: list[str] = []
    keywords: list[str] = []
    ui_action: str | None = None
    scene_action_id: str | None = None
    target_node_id: str | None = None
    expand_hops: int = 1
    perspective: str | None = None
    tier1: list[str] = []  # Mind tags
    depth: int = 1
    k: int = 12
    lanes: list[str] = Field(default_factory=lambda: ["bm25", "dense"])

    # CLARIFY fields (when action=CLARIFY)
    reason: str | None = None
    invalid_terms: list[str] = []
    valid_entities: list[str] = []  # Entities that DID resolve (for mixed valid/fabricated)
    suggestions: list[str] = []

    # Evidence gate results (per-app verification)
    grounding: dict | None = None

    # Evidence case (auto-generated when action=COMPLIANCE)
    evidence_case: dict | None = None

    # Concrete httpx call plan
    query_plan: QueryPlan | None = None


# ── Query plan builder ────────────────────────────────────────────────────────


def _build_query_plan(action: str, entities: list[str], query: str, ui_action: str | None, expand_hops: int, depth: int, scope: str = "") -> QueryPlan | None:
    """Build httpx call plan from QuerySpec fields. Scope-aware collections."""
    from ._queryspec_executor import SCOPE_CONFIGS, DEFAULT_CONFIG
    cfg = SCOPE_CONFIGS.get(scope, DEFAULT_CONFIG)
    node_col = cfg["node_collection"]
    edge_col = cfg["edge_collection"]
    id_field = cfg["id_field"]
    q_lower = query.lower()

    # UI commands → graph traversal
    if action == "UI_COMMAND" and ui_action in ("SELECT_NODE", "ZOOM_IN", "TOGGLE_PROGRESSIVE"):
        start = entities[0] if entities else query.split()[-1]
        return QueryPlan(
            strategy="graph_traversal",
            steps=[
                QueryStep(endpoint="/list", params={"collection": node_col, "limit": 1, "filters": {id_field: start}}),
                QueryStep(endpoint="/recall", params={"q": start, "k": expand_hops * 10, "collections": [edge_col]}),
            ],
            extracted_entities=entities,
            max_hops=expand_hops,
            start_node=start,
        )

    if action in ("CLARIFY", "NO_MATCH", "CONTENT_SAFETY"):
        return None

    # Graph traversal keywords
    if any(kw in q_lower for kw in ("hop", "neighbor", "path between", "connected to", "route")):
        hops_match = re.search(r'(\d+)\s*hop', q_lower)
        max_hops = int(hops_match.group(1)) if hops_match else depth or 2
        start = entities[0] if entities else query.split()[-1]
        return QueryPlan(
            strategy="graph_traversal",
            steps=[
                QueryStep(endpoint="/list", params={"collection": node_col, "limit": 1, "filters": {id_field: start}}),
                QueryStep(endpoint="/recall", params={"q": start, "k": max_hops * 10, "collections": [edge_col]}),
            ],
            extracted_entities=entities,
            max_hops=max_hops,
            start_node=start,
        )

    # Relationship lookup: 2+ entities or relationship keywords
    if len(entities) >= 2 or any(kw in q_lower for kw in ("relate", "compare", "connection")):
        steps = [
            QueryStep(endpoint="/list", params={"collection": node_col, "limit": 1, "filters": {id_field: eid}})
            for eid in entities[:2]
        ]
        if entities:
            steps.append(QueryStep(endpoint="/recall", params={"q": " ".join(entities), "k": 10, "collections": [edge_col]}))
        return QueryPlan(strategy="relationship_lookup", steps=steps, extracted_entities=entities)

    # Filtered listing
    if any(kw in q_lower for kw in ("all ", "list ", "filter", "with mind", "with heart")):
        filters: dict[str, str] = {}
        for fw in ("NIST", "SPARTA", "CWE", "CAPEC"):
            if fw.lower() in q_lower:
                filters["source_framework"] = fw
                break
        for tag in ("Detect", "Evade", "Exploit", "Harden", "Isolate", "Model", "Persist", "Restore"):
            if tag.lower() in q_lower:
                filters["mind"] = tag
                break
        collection = node_col if filters else "sparta_qra"
        return QueryPlan(
            strategy="filtered_listing",
            steps=[QueryStep(endpoint="/list", params={"collection": collection, "limit": 50, "filters": filters})],
            extracted_entities=entities,
        )

    # Text search fallback (no explicit plan — caller uses /recall)
    return None


# ── Route ─────────────────────────────────────────────────────────────────────


@router.post("/intent", response_model=IntentResponse)
def classify_intent(body: IntentRequest) -> IntentResponse:
    """Unified NL→structured query translation.

    Delegates to IntentMapper for full QuerySpec generation.
    Falls back to keyword heuristic if mapper unavailable.
    
    App Context Routing:
      When body.app is set (e.g., "binary-explorer"), queries like "show me CWE-79"
      are routed to APP_COMMAND using registered app_actions. Without app context,
      the same query routes to COMPLIANCE (Brandon explains CWE-79).
    """
    logger.info("[INTENT] Received query='{}' scope='{}' app={} fast={}", 
                body.q[:80], body.scope, body.app, body.fast)
    
    # --- App Context Routing ---
    # If app is specified, check app_actions FIRST for UI command routing.
    # "show me CWE-79" in Binary Explorer = navigate to node
    # "show me CWE-79" without app context = COMPLIANCE (explain it)
    if body.app:
        app_action_result = _check_app_actions(body.q, body.app)
        if app_action_result:
            logger.info("[INTENT] APP_COMMAND via app_actions: app={} action={} element={}",
                        body.app, app_action_result.get("action"), app_action_result.get("element_id"))
            return IntentResponse(
                action="APP_COMMAND",
                ui_action=app_action_result.get("action"),
                target_node_id=app_action_result.get("target"),
                entities=app_action_result.get("entities", []),
                confidence=0.95,
                classifier_source="app_actions_context",
                rewritten_query=body.q,
            )
    
    try:
        from ...intent import get_mapper
        mapper = get_mapper(use_llm=not body.fast)
        spec = mapper.infer(body.q, session_id=body.session_id, scope=body.scope)
        logger.info(
            "[INTENT] Mapper result: action={} source={} confidence={:.2f} entities={}",
            spec.get("action", "?"), spec.get("classifier_source", "?"),
            spec.get("confidence", 0), spec.get("entities", [])[:5],
        )
    except Exception as exc:
        logger.error("[INTENT] IntentMapper unavailable, keyword fallback: {}", exc)
        spec = None

    if spec and isinstance(spec, dict):
        # Evidence gate: verify targets exist before the app acts on them
        if body.scope and spec.get("action") in ("UI_COMMAND", "QUERY"):
            from ...intent._grounding import verify_intent
            spec = verify_intent(spec, scope=body.scope)

        action = spec.get("action", "QUERY")
        entities = spec.get("entities", [])
        ui_action = spec.get("ui_action")
        expand_hops = spec.get("expand_hops", 1)
        depth = spec.get("depth", 1)

        # --- COMPLIANCE auto-routing ---
        # When the 4-way classifier routes to COMPLIANCE, build an evidence case
        # using existing daemon endpoints. Entities are already extracted by the
        # mapper (cached from step 3, no redundant /extract-entities call).
        if action == "COMPLIANCE":
            logger.info(
                "[COMPLIANCE] 4-way classifier routed query to evidence case pipeline. "
                "entities={} frameworks={} source={} confidence={:.2f} query='{}'",
                entities, spec.get("frameworks", []),
                spec.get("classifier_source", "?"), spec.get("confidence", 0),
                body.q[:80],
            )
            try:
                from ...intent._grounding import get_daemon_client
                client = get_daemon_client()
                evidence_parts: dict = {"question": body.q, "entities": entities, "steps": []}

                # Step 1: /taxonomy/chain — cross-framework BFS traversal
                if entities:
                    logger.info("[COMPLIANCE] Step 1: /taxonomy/chain for {} entities", len(entities))
                    chain_resp = client.post("/taxonomy/chain", json={
                        "control_id": entities[0],
                        "depth": 2,
                    }, timeout=15)
                    if chain_resp.status_code == 200:
                        chain_data = chain_resp.json()
                        evidence_parts["taxonomy_chain"] = chain_data
                        evidence_parts["steps"].append("taxonomy_chain")
                        chain = chain_data.get("chain", {})
                        total_links = sum(len(v) for v in chain.values() if isinstance(v, list))
                        logger.info(
                            "[COMPLIANCE] Step 1 complete: {} frameworks, {} total links (CWE={}, CAPEC={}, ATT&CK={}, NIST={})",
                            len(chain), total_links,
                            len(chain.get("cwe", [])), len(chain.get("capec", [])),
                            len(chain.get("attack", [])), len(chain.get("nist", [])),
                        )
                    else:
                        logger.error("[COMPLIANCE] /taxonomy/chain returned {}", chain_resp.status_code)

                # Step 2: /recall — get QRA evidence for the cross-framework question
                logger.info("[COMPLIANCE] Step 2: /recall for evidence QRAs")
                recall_resp = client.post("/recall", json={
                    "q": body.q,
                    "k": 10,
                    "scope": "sparta",
                }, timeout=15)
                if recall_resp.status_code == 200:
                    recall_data = recall_resp.json()
                    evidence_parts["recall"] = {
                        "confidence": recall_data.get("confidence", 0),
                        "found": recall_data.get("found", False),
                        "items": recall_data.get("items", [])[:5],  # top 5 for evidence
                    }
                    evidence_parts["steps"].append("recall")
                    logger.info(
                        "[COMPLIANCE] Step 2 complete: confidence={:.2f} items={}",
                        recall_data.get("confidence", 0), len(recall_data.get("items", [])),
                    )
                else:
                    logger.error("[COMPLIANCE] /recall returned {}", recall_resp.status_code)

                client.close()
                spec["evidence_case"] = evidence_parts
                logger.info(
                    "[COMPLIANCE] Evidence case built: {} steps completed for '{}'",
                    len(evidence_parts["steps"]), body.q[:60],
                )
            except Exception as exc:
                logger.error("[COMPLIANCE] Evidence case build failed (non-fatal): {}", exc)

        query_plan = _build_query_plan(action, entities, body.q, ui_action, expand_hops, depth, scope=body.scope)

        return IntentResponse(
            scope=spec.get("scope", "brandon_bailey"),
            query_type="ui_command" if action == "UI_COMMAND" else spec.get("query_type", "knowledge"),
            confidence=spec.get("confidence", 0.7),
            classifier_source=spec.get("classifier_source", "intent_mapper"),
            rewritten_query=spec.get("rewritten_query", body.q),
            action=action,
            entities=entities,
            frameworks=spec.get("frameworks", []),
            keywords=spec.get("keywords", []),
            ui_action=ui_action,
            scene_action_id=spec.get("scene_action_id"),
            target_node_id=spec.get("target_node_id"),
            expand_hops=expand_hops,
            perspective=spec.get("perspective"),
            tier1=spec.get("tier1", []),
            depth=depth,
            k=spec.get("k", 12),
            lanes=spec.get("lanes", ["bm25", "dense"]),
            # CLARIFY fields
            reason=spec.get("reason"),
            invalid_terms=spec.get("invalid_terms", []),
            valid_entities=spec.get("valid_entities", []),
            suggestions=spec.get("suggestions", []),
            grounding=spec.get("grounding"),
            evidence_case=spec.get("evidence_case"),
            query_plan=query_plan,
        )

    # Keyword fallback when IntentMapper unavailable
    entities = _extract_control_ids(body.q)
    query_plan = _build_query_plan("QUERY", entities, body.q, None, 1, 1)

    return IntentResponse(
        scope="brandon_bailey",
        query_type="knowledge",
        confidence=0.5,
        classifier_source="keyword_fallback",
        rewritten_query=body.q,
        action="QUERY",
        entities=entities,
        query_plan=query_plan,
    )


def _extract_control_ids(query: str) -> list[str]:
    """Extract control IDs from query text. Regex for tokenization only."""
    patterns = [
        r'\b(CWE-\d+)\b',
        r'\b(CAPEC-\d+)\b',
        r'\b([A-Z]{2,3}-\d+(?:\.\d+)?(?:\(\d+\))?)\b',
        r'\b(T\d{4}(?:\.\d{3})?)\b',
    ]
    entities: list[str] = []
    for pat in patterns:
        for m in re.finditer(pat, query):
            eid = m.group(1)
            if eid not in entities:
                entities.append(eid)
    return entities
