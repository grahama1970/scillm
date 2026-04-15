"""Recall-based intent grounding, self-correction, and evidence gate.

Provides three capabilities for IntentMapper:
1. recall_grounded_intent: Check learned intent patterns via /recall BEFORE LLM
2. check_self_correction: Detect user rejection and offer graph-grounded alternatives
3. verify_intent: Per-app evidence gate — verify targets exist before executing

All daemon access via /recall and /list endpoints (ENDPOINT FREEZE).
"""

from __future__ import annotations

import json
import os
from typing import Optional

import httpx
from loguru import logger

from ._spec import QuerySpec

SOCK = "/run/user/1000/embry/memory.sock"


def get_daemon_client() -> httpx.Client:
    """Get httpx client for daemon. Uses UDS on host, HTTP in Docker."""
    if os.path.exists(SOCK):
        return httpx.Client(
            transport=httpx.HTTPTransport(uds=SOCK),
            base_url="http://localhost", timeout=10,
        )
    return httpx.Client(
        base_url=os.getenv("MEMORY_SERVICE_URL", "http://localhost:8601"),
        timeout=10,
    )


def recall_grounded_intent(query: str, scope: str = "") -> Optional[dict]:
    """Check learned intent patterns via /recall before hitting LLM.

    Searches intent-training-v2 tagged lessons for a close match.
    If scope is provided (e.g. "binary-explorer"), adds it as a label filter
    so only interactions tagged for that surface are matched.
    Returns a validated QuerySpec dict if confidence >= 0.75, else None.
    """
    try:
        labels = ["intent-training-v2"]
        if scope:
            labels.append(f"scope:{scope}")

        client = get_daemon_client()
        resp = client.post("/recall", json={
            "q": query,
            "k": 3,
            "labels": labels,
        })
        client.close()
        if resp.status_code != 200:
            return None

        data = resp.json()
        confidence = data.get("confidence", 0)
        items = data.get("items", data.get("found", []))

        if not isinstance(items, list) or not items or confidence < 0.75:
            return None

        best = items[0]
        solution = best.get("solution", best.get("answer", ""))
        if not solution:
            return None

        try:
            spec_data = json.loads(solution)
        except (json.JSONDecodeError, TypeError):
            return None

        validated = QuerySpec(**spec_data)
        result = validated.model_dump()
        
        # Don't use recall-grounded results for NO_MATCH - let the 2-way classifier handle OFF_TOPIC
        # This prevents old NO_MATCH training examples from polluting current routing
        if result.get("action") == "NO_MATCH":
            logger.debug(f"Recall-grounded returned NO_MATCH, skipping to let 2-way classifier decide")
            return None
        
        result["classifier_source"] = "recall_grounded"
        result["confidence"] = confidence
        if scope:
            result["scope"] = scope
        logger.info(f"Recall-grounded intent: action={result['action']}, scope={scope or 'any'}, confidence={confidence:.2f}")
        return result

    except Exception as exc:
        logger.error(f"Recall grounding failed (non-fatal): {exc}")
        return None


def check_self_correction(
    query: str,
    previous: Optional[dict],
) -> Optional[dict]:
    """Detect user rejection of previous result and offer grounded alternatives.

    Args:
        query: Current user query
        previous: Previous ThreadTracker entry {"query": ..., "spec": ...} or None

    Returns CLARIFY with alternatives from the graph, or None to continue.
    """
    rejection_signals = (
        "no,", "no ", "wrong", "not that", "not what i", "that's not",
        "thats not", "try again", "incorrect", "nope",
    )
    q_lower = query.lower().strip()
    if not any(q_lower.startswith(s) or s in q_lower for s in rejection_signals):
        return None

    if previous is None:
        return None

    prev_spec = previous["spec"]
    prev_query = previous["query"]
    logger.info(f"Self-correction triggered: user rejected '{prev_query[:60]}' result")

    alternatives = _recall_alternatives(prev_spec, prev_query)

    suggestions = [
        f"Did you mean one of these? {'; '.join(alternatives[:3])}"
    ] if alternatives else [
        f"Could you rephrase what you're looking for? Your previous query was: '{prev_query[:100]}'"
    ]

    return {
        "action": "CLARIFY",
        "scope": "sparta",
        "reason": "Self-correction: previous result rejected by user",
        "suggestions": suggestions,
        "persona_guidance": (
            "I see that wasn't right. "
            + (f"Here are similar items: {', '.join(alternatives[:3])}" if alternatives
               else "Could you be more specific about what you're looking for?")
        ),
        "original_query": query,
        "previous_query": prev_query,
    }


def _recall_alternatives(prev_spec: dict, prev_query: str) -> list[str]:
    """Search for alternative nodes/controls via /recall."""
    prev_entities = prev_spec.get("entities", [])
    prev_target = prev_spec.get("target_node_id", "")
    search_q = prev_target or (prev_entities[0] if prev_entities else prev_query)

    alternatives: list[str] = []
    try:
        client = get_daemon_client()
        resp = client.post("/recall", json={"q": search_q, "k": 5})
        client.close()
        if resp.status_code == 200:
            items = resp.json().get("items", resp.json().get("found", []))
            if isinstance(items, list):
                for item in items[:5]:
                    name = item.get("name", item.get("control_id", item.get("problem", "")))
                    cid = item.get("control_id", item.get("_key", ""))
                    if name and cid:
                        alternatives.append(f"{cid}: {name[:80]}")
    except Exception as exc:
        logger.error(f"Self-correction recall failed: {exc}")

    return alternatives


# ── Per-app evidence gate configs ─────────────────────────────────────────────

# Each app scope maps to: collection to check targets against, and what fields
# constitute "has data" for scene actions.
APP_EVIDENCE_CONFIGS: dict[str, dict] = {
    "binary-explorer": {
        "collection": "binary_features",
        "id_field": "_key",
        "edge_collection": "binary_feature_edges",
    },
    "sparta-explorer": {
        "collection": "sparta_controls",
        "id_field": "control_id",
        "edge_collection": "sparta_relationships",
    },
}


def verify_intent(spec: dict, scope: str = "") -> dict:
    """Per-app evidence gate: verify the intent can actually be executed.

    Checks:
    1. target_node_id exists in the app's collection
    2. For SCENE_ACTIONs, the target has edges (data to act on)
    3. For entities, at least one resolves

    Returns the spec with a `grounding` dict added:
      grounding.target_resolved: bool
      grounding.target_collection: str
      grounding.has_edges: bool
      grounding.closest_match: str | None (if target not found)
      grounding.evidence_source: str

    If target doesn't resolve and no entities anchor the query,
    downgrades action to CLARIFY with suggestions.
    """
    config = APP_EVIDENCE_CONFIGS.get(scope)
    if not config:
        # No evidence config for this scope — pass through
        spec["grounding"] = {"evidence_source": "no_config", "target_resolved": True}
        return spec

    target = spec.get("target_node_id")
    ui_action = spec.get("ui_action")
    entities = spec.get("entities", [])
    grounding: dict = {"evidence_source": "verified", "target_resolved": False, "has_edges": False}

    if not target and not entities:
        # No target to verify — pass through
        grounding["target_resolved"] = True
        spec["grounding"] = grounding
        return spec

    try:
        client = get_daemon_client()

        # Check 1: Does target_node_id exist?
        if target:
            resp = client.post("/recall", json={"q": target, "k": 3})
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("items", data.get("found", []))
                if isinstance(items, list) and items:
                    best = items[0]
                    best_id = best.get(config["id_field"], best.get("_key", ""))
                    best_name = best.get("name", best.get("label", best.get("problem", "")))
                    confidence = data.get("confidence", 0)

                    if confidence >= 0.7:
                        grounding["target_resolved"] = True
                        grounding["resolved_id"] = best_id
                        grounding["resolved_name"] = best_name[:80]
                        grounding["target_collection"] = config["collection"]
                    else:
                        grounding["closest_match"] = f"{best_id}: {best_name[:60]}" if best_id else None

            # Check 2: For SCENE_ACTION, does target have edges?
            if grounding["target_resolved"] and ui_action == "SCENE_ACTION":
                resolved_id = grounding.get("resolved_id", target)
                edge_resp = client.post("/recall", json={
                    "q": resolved_id, "k": 5,
                    "collections": [config.get("edge_collection", "")],
                })
                if edge_resp.status_code == 200:
                    edge_items = edge_resp.json().get("items", edge_resp.json().get("found", []))
                    grounding["has_edges"] = isinstance(edge_items, list) and len(edge_items) > 0
                    grounding["edge_count"] = len(edge_items) if isinstance(edge_items, list) else 0

        # Check 3: Verify entities resolve
        if entities:
            resolved = []
            for eid in entities[:5]:
                er = client.post("/recall", json={"q": eid, "k": 1})
                if er.status_code == 200:
                    ei = er.json().get("items", er.json().get("found", []))
                    if isinstance(ei, list) and ei and er.json().get("confidence", 0) >= 0.7:
                        resolved.append(eid)
            grounding["entities_resolved"] = resolved
            if not target:
                grounding["target_resolved"] = len(resolved) > 0

        client.close()
    except Exception as exc:
        logger.error(f"Evidence gate failed (non-fatal): {exc}")
        grounding["evidence_source"] = "error"
        grounding["target_resolved"] = True  # Fail open

    # Downgrade to CLARIFY if target not found
    if not grounding["target_resolved"] and spec.get("action") != "CLARIFY":
        closest = grounding.get("closest_match")
        spec["action"] = "CLARIFY"
        spec["reason"] = f"target_node_id '{target}' not found in {config['collection']}"
        spec["suggestions"] = [
            f"Did you mean {closest}?" if closest else f"Could not find '{target}'. Try a different name.",
        ]
        logger.info(f"Evidence gate downgraded to CLARIFY: {target} not found")

    spec["grounding"] = grounding
    return spec
