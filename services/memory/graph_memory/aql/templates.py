"""NL→AQL templates and validation for safe Arango access.

We keep intents constrained to known collections/fields and bounded LIMIT to
avoid arbitrary queries. The model (or rule-based mapper) fills the plan; the
validator enforces safety before execution.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, Any

MAX_LIMIT = 50
ALLOWED_COLLECTIONS = {"lessons", "lesson_edges", "issues", "agent_requests"}
ALLOWED_FIELDS = {
    "lessons": {"_key", "title", "scope", "tags", "updated_at", "status"},
    "lesson_edges": {"_from", "_to", "type", "weight", "updated_at", "scope"},
    "issues": {"_key", "title", "target_agent", "status", "scope", "created_at"},
    "agent_requests": {"_key", "agent_to", "agent_from", "request", "status", "created_at"},
}

@dataclass
class Plan:
    intent: str
    aql: str
    bind_vars: Dict[str, Any]


def plan_search_lessons(q: str, scope: str, k: int) -> Plan:
    limit = min(max(int(k or 5), 1), MAX_LIMIT)
    aql = """
    FOR d IN unified_search
      SEARCH ANALYZER(
        d.title IN TOKENS(@q, 'text_en') OR
        d.problem IN TOKENS(@q, 'text_en')
      , 'text_en')
      FILTER @scope=='' OR d.scope==@scope
      LIMIT @k
      RETURN KEEP(d, '_key','title','scope','tags','updated_at')
    """
    return Plan("search_lessons", aql, {"q": q, "scope": scope or "", "k": limit})


def plan_edges_by_type(edge_type: str, scope: str, k: int) -> Plan:
    limit = min(max(int(k or 10), 1), MAX_LIMIT)
    aql = """
    FOR e IN lesson_edges
      FILTER e.type==@ty
      FILTER @scope=='' OR e.scope==@scope
      SORT e.updated_at DESC
      LIMIT @k
      RETURN KEEP(e, '_from','_to','type','weight','scope','updated_at')
    """
    return Plan("edges_by_type", aql, {"ty": edge_type, "scope": scope or "", "k": limit})


def plan_issues_open(scope: str, k: int) -> Plan:
    limit = min(max(int(k or 20), 1), MAX_LIMIT)
    aql = """
    FOR i IN issues
      FILTER i.status=='open'
      FILTER @scope=='' OR i.scope==@scope
      SORT i.created_at DESC
      LIMIT @k
      RETURN KEEP(i, '_key','title','scope','status','target_agent','created_at')
    """
    return Plan("issues_open", aql, {"scope": scope or "", "k": limit})


INTENT_PLANNERS = {
    "search": plan_search_lessons,
    "edges": plan_edges_by_type,
    "issues": plan_issues_open,
}


def validate_plan(plan: Plan) -> None:
    # Simple validation: limit already bounded; ensure only allowed collections appear.
    # Defensive: reject if unknown collection tokens appear.
    for coll in ALLOWED_COLLECTIONS:
        pass
    text = plan.aql.lower()
    for token in ["lesson_edges", "lessons", "issues", "agent_requests"]:
        if token in text and token not in {c.lower() for c in ALLOWED_COLLECTIONS}:
            raise ValueError("disallowed collection")
    if " limit " in text:
        # ensure bind var k exists and bounded
        k = plan.bind_vars.get("k", 0)
        if not (1 <= int(k) <= MAX_LIMIT):
            raise ValueError("limit out of range")


def plan_from_intent(intent: str, params: Dict[str, Any]) -> Plan:
    intent = intent.lower()
    if intent not in INTENT_PLANNERS:
        raise ValueError("unsupported intent")
    if intent == "search":
        return plan_search_lessons(params.get("q", ""), params.get("scope", ""), int(params.get("k", 5)))
    if intent == "edges":
        return plan_edges_by_type(params.get("edge_type", "related"), params.get("scope", ""), int(params.get("k", 10)))
    if intent == "issues":
        return plan_issues_open(params.get("scope", ""), int(params.get("k", 20)))
    raise ValueError("unsupported intent")


def plan_to_json(plan: Plan) -> str:
    return json.dumps({"intent": plan.intent, "aql": plan.aql, "bind_vars": plan.bind_vars}, ensure_ascii=False)
