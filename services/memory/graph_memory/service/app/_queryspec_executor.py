"""Deterministic QuerySpec executor — converts QuerySpec to daemon calls and runs them.

POST /execute-queryspec — takes a classified QuerySpec and executes it.

NO LLM involved. Pure Python switch/case on action + ui_action + scene_action_id.
This avoids the "two LLM calls = hallucination squared" problem:
  LLM generates QuerySpec → deterministic executor runs it.

Scope-aware: binary-explorer uses binary_features collection,
sparta-explorer uses sparta_controls collection.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter
from loguru import logger
from pydantic import BaseModel, Field

router = APIRouter()

SOCK = "/run/user/1000/embry/memory.sock"


# ── Scope configs ────────────────────────────────────────────────────────────

SCOPE_CONFIGS: dict[str, dict[str, str]] = {
    "binary-explorer": {
        "node_collection": "binary_features",
        "edge_collection": "binary_feature_edges",
        "id_field": "_key",
    },
    "sparta-explorer": {
        "node_collection": "sparta_controls",
        "edge_collection": "sparta_relationships",
        "id_field": "control_id",
    },
    "sparta": {
        "node_collection": "sparta_controls",
        "edge_collection": "sparta_relationships",
        "id_field": "control_id",
    },
}

DEFAULT_CONFIG = SCOPE_CONFIGS["sparta"]


# ── Request / Response models ────────────────────────────────────────────────

class ExecuteRequest(BaseModel):
    """QuerySpec to execute."""

    action: str = "QUERY"
    ui_action: str | None = None
    scene_action_id: str | None = None
    target_node_id: str | None = None
    expand_hops: int = 1
    perspective: str | None = None
    scope: str = ""
    entities: list[str] = []
    keywords: list[str] = []
    k: int = 12
    depth: int = 1
    lanes: list[str] = Field(default_factory=lambda: ["bm25", "dense"])
    tier1: list[str] = []
    frameworks: list[str] = []
    min_grounding: float = 0.7
    sort: str = "rrf"


class ExecuteResult(BaseModel):
    """Execution results from the deterministic executor."""

    strategy: str
    items: list[dict[str, Any]] = []
    total: int = 0
    edges: list[dict[str, Any]] = []
    node: dict[str, Any] | None = None
    metadata: dict[str, Any] = {}


# ── Daemon client ────────────────────────────────────────────────────────────

def _get_client():
    import httpx
    if os.path.exists(SOCK):
        return httpx.Client(
            transport=httpx.HTTPTransport(uds=SOCK),
            base_url="http://localhost", timeout=15,
        )
    return httpx.Client(
        base_url=os.getenv("MEMORY_SERVICE_URL", "http://localhost:8601"),
        timeout=15,
    )


def _call(client, endpoint: str, params: dict) -> dict:
    """Call a daemon endpoint and return parsed JSON."""
    resp = client.post(endpoint, json=params)
    if resp.status_code == 200:
        return resp.json()
    logger.debug(f"Daemon call {endpoint} failed: {resp.status_code}")
    return {}


# ── Scene action executors ───────────────────────────────────────────────────

def _scene_trace_execution(client, target: str, cfg: dict) -> ExecuteResult:
    """Trace execution path from a node — follow outgoing edges."""
    node_data = _call(client, "/recall", {"q": target, "k": 1})
    edges = _call(client, "/recall", {
        "q": target, "k": 20,
        "collections": [cfg["edge_collection"]],
    })
    items = edges.get("items", edges.get("found", []))
    return ExecuteResult(
        strategy="scene:trace_execution",
        node=_first_item(node_data),
        edges=[i for i in (items if isinstance(items, list) else [])],
        total=len(items) if isinstance(items, list) else 0,
        metadata={"start_node": target},
    )


def _scene_find_attack_surface(client, cfg: dict) -> ExecuteResult:
    """Find externally-accessible nodes (RPCs, CLI commands, input handlers)."""
    results = _call(client, "/recall", {
        "q": "external RPC CLI command input handler entry point",
        "k": 25,
    })
    items = results.get("items", results.get("found", []))
    return ExecuteResult(
        strategy="scene:find_attack_surface",
        items=items if isinstance(items, list) else [],
        total=len(items) if isinstance(items, list) else 0,
    )


def _scene_compare_nodes(client, entities: list[str], cfg: dict) -> ExecuteResult:
    """Compare two nodes side by side."""
    nodes = []
    for eid in entities[:2]:
        data = _call(client, "/recall", {"q": eid, "k": 1})
        node = _first_item(data)
        if node:
            nodes.append(node)
    return ExecuteResult(
        strategy="scene:compare_nodes",
        items=nodes,
        total=len(nodes),
        metadata={"compared": entities[:2]},
    )


def _scene_find_data_sinks(client, target: str, cfg: dict) -> ExecuteResult:
    """Trace where data/user input ends up."""
    results = _call(client, "/recall", {
        "q": f"{target} data sink output write store persist",
        "k": 15,
    })
    items = results.get("items", results.get("found", []))
    return ExecuteResult(
        strategy="scene:find_data_sinks",
        items=items if isinstance(items, list) else [],
        total=len(items) if isinstance(items, list) else 0,
        metadata={"from_node": target},
    )


def _scene_show_auth_chain(client, cfg: dict) -> ExecuteResult:
    """Show authentication flow nodes."""
    results = _call(client, "/recall", {
        "q": "authentication authorization credential token session login",
        "k": 20,
    })
    items = results.get("items", results.get("found", []))
    return ExecuteResult(
        strategy="scene:show_auth_chain",
        items=items if isinstance(items, list) else [],
        total=len(items) if isinstance(items, list) else 0,
    )


def _scene_show_architecture(client, cfg: dict) -> ExecuteResult:
    """Display high-level hub nodes (namespaces, state machines)."""
    results = _call(client, "/recall", {
        "q": "namespace architecture hub state machine module",
        "k": 20,
    })
    items = results.get("items", results.get("found", []))
    return ExecuteResult(
        strategy="scene:show_architecture",
        items=items if isinstance(items, list) else [],
        total=len(items) if isinstance(items, list) else 0,
    )


def _scene_trace_event_flow(client, target: str, cfg: dict) -> ExecuteResult:
    """Follow event emission chain from a node."""
    results = _call(client, "/recall", {
        "q": f"{target} event emit signal handler listener",
        "k": 15,
    })
    edges = _call(client, "/recall", {
        "q": target, "k": 20,
        "collections": [cfg["edge_collection"]],
    })
    items = results.get("items", results.get("found", []))
    edge_items = edges.get("items", edges.get("found", []))
    return ExecuteResult(
        strategy="scene:trace_event_flow",
        items=items if isinstance(items, list) else [],
        edges=edge_items if isinstance(edge_items, list) else [],
        total=len(items) if isinstance(items, list) else 0,
        metadata={"start_node": target},
    )


def _scene_find_input_handlers(client, cfg: dict) -> ExecuteResult:
    """Find where user input enters the system."""
    results = _call(client, "/recall", {
        "q": "input handler parameter user request parse receive",
        "k": 20,
    })
    items = results.get("items", results.get("found", []))
    return ExecuteResult(
        strategy="scene:find_input_handlers",
        items=items if isinstance(items, list) else [],
        total=len(items) if isinstance(items, list) else 0,
    )


def _scene_show_event_lifecycle(client, target: str, cfg: dict) -> ExecuteResult:
    """Show full event lifecycle for a node."""
    results = _call(client, "/recall", {
        "q": f"{target} event lifecycle create emit handle consume",
        "k": 15,
    })
    items = results.get("items", results.get("found", []))
    return ExecuteResult(
        strategy="scene:show_event_lifecycle",
        items=items if isinstance(items, list) else [],
        total=len(items) if isinstance(items, list) else 0,
        metadata={"target": target},
    )


SCENE_HANDLERS = {
    "trace_execution": _scene_trace_execution,
    "find_attack_surface": _scene_find_attack_surface,
    "compare_nodes": _scene_compare_nodes,
    "find_data_sinks": _scene_find_data_sinks,
    "show_auth_chain": _scene_show_auth_chain,
    "trace_event_flow": _scene_trace_event_flow,
    "show_architecture": _scene_show_architecture,
    "find_auth_flow": _scene_show_auth_chain,  # alias
    "find_input_handlers": _scene_find_input_handlers,
    "show_event_lifecycle": _scene_show_event_lifecycle,
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _first_item(data: dict) -> dict | None:
    items = data.get("items", data.get("found", []))
    if isinstance(items, list) and items:
        return items[0]
    return None


# ── UI command executors ─────────────────────────────────────────────────────

def _exec_select_node(client, target: str, hops: int, cfg: dict) -> ExecuteResult:
    """Select a node and expand N hops of neighbors."""
    node_data = _call(client, "/recall", {"q": target, "k": 1})
    node = _first_item(node_data)

    edges = _call(client, "/recall", {
        "q": target, "k": hops * 10,
        "collections": [cfg["edge_collection"]],
    })
    edge_items = edges.get("items", edges.get("found", []))

    return ExecuteResult(
        strategy="ui:select_node",
        node=node,
        edges=edge_items if isinstance(edge_items, list) else [],
        total=1 + (len(edge_items) if isinstance(edge_items, list) else 0),
        metadata={"expand_hops": hops},
    )


def _exec_zoom(client, target: str, cfg: dict) -> ExecuteResult:
    """Zoom to a specific node."""
    node_data = _call(client, "/recall", {"q": target, "k": 1})
    return ExecuteResult(
        strategy="ui:zoom",
        node=_first_item(node_data),
        total=1,
    )


def _exec_view_all(client, cfg: dict) -> ExecuteResult:
    """List all nodes for full graph reset."""
    data = _call(client, "/list", {"collection": cfg["node_collection"], "limit": 200})
    docs = data.get("documents", data.get("items", []))
    return ExecuteResult(
        strategy="ui:view_all",
        items=docs if isinstance(docs, list) else [],
        total=data.get("total", len(docs) if isinstance(docs, list) else 0),
    )


def _exec_set_perspective(client, perspective: str, cfg: dict) -> ExecuteResult:
    """Filter nodes by perspective (Security, Data Flow, Protocol, All)."""
    perspective_queries = {
        "Security": "security vulnerability attack threat defense",
        "Data Flow": "data flow input output parameter schema",
        "Protocol": "protocol communication message RPC event",
        "All": "",
    }
    q = perspective_queries.get(perspective, "")
    if q:
        data = _call(client, "/recall", {"q": q, "k": 50})
        items = data.get("items", data.get("found", []))
    else:
        data = _call(client, "/list", {"collection": cfg["node_collection"], "limit": 200})
        items = data.get("documents", data.get("items", []))

    return ExecuteResult(
        strategy="ui:set_perspective",
        items=items if isinstance(items, list) else [],
        total=len(items) if isinstance(items, list) else 0,
        metadata={"perspective": perspective},
    )


def _exec_dismiss_node(target: str) -> ExecuteResult:
    """Dismiss/hide a node — client-side only, no daemon call needed."""
    return ExecuteResult(
        strategy="ui:dismiss_node",
        metadata={"dismissed": target},
        total=0,
    )


def _exec_focus_cluster(client, target: str, cfg: dict) -> ExecuteResult:
    """Isolate a connected component around a node."""
    node_data = _call(client, "/recall", {"q": target, "k": 1})
    edges = _call(client, "/recall", {
        "q": target, "k": 30,
        "collections": [cfg["edge_collection"]],
    })
    edge_items = edges.get("items", edges.get("found", []))
    return ExecuteResult(
        strategy="ui:focus_cluster",
        node=_first_item(node_data),
        edges=edge_items if isinstance(edge_items, list) else [],
        total=len(edge_items) if isinstance(edge_items, list) else 0,
        metadata={"cluster_center": target},
    )


def _exec_collapse_namespace(client, target: str, cfg: dict) -> ExecuteResult:
    """Group a namespace into a super-node — return child nodes."""
    data = _call(client, "/recall", {"q": f"{target} namespace", "k": 30})
    items = data.get("items", data.get("found", []))
    return ExecuteResult(
        strategy="ui:collapse_namespace",
        items=items if isinstance(items, list) else [],
        total=len(items) if isinstance(items, list) else 0,
        metadata={"namespace": target},
    )


# ── Main executor ────────────────────────────────────────────────────────────

def execute_queryspec(spec: ExecuteRequest) -> ExecuteResult:
    """Deterministic QuerySpec → daemon calls → results. No LLM."""
    cfg = SCOPE_CONFIGS.get(spec.scope, DEFAULT_CONFIG)
    target = spec.target_node_id or (spec.entities[0] if spec.entities else "")

    if spec.action in ("NO_MATCH", "CLARIFY", "CONTENT_SAFETY"):
        return ExecuteResult(strategy="no_op", metadata={"action": spec.action})

    client = _get_client()
    try:
        if spec.action == "UI_COMMAND":
            return _execute_ui_command(client, spec, target, cfg)
        return _execute_query(client, spec, target, cfg)
    finally:
        client.close()


def _execute_ui_command(client, spec: ExecuteRequest, target: str, cfg: dict) -> ExecuteResult:
    """Route UI_COMMAND to the right executor."""
    ua = spec.ui_action

    # Scene actions get their own handlers
    if ua == "SCENE_ACTION" and spec.scene_action_id:
        handler = SCENE_HANDLERS.get(spec.scene_action_id)
        if handler is None:
            return ExecuteResult(
                strategy="error",
                metadata={"error": f"Unknown scene_action_id: {spec.scene_action_id}"},
            )
        # Scene handlers have varying signatures — dispatch by arity
        if spec.scene_action_id in ("find_attack_surface", "show_auth_chain",
                                     "find_auth_flow", "show_architecture",
                                     "find_input_handlers"):
            return handler(client, cfg)
        if spec.scene_action_id == "compare_nodes":
            return handler(client, spec.entities, cfg)
        return handler(client, target, cfg)

    match ua:
        case "SELECT_NODE":
            return _exec_select_node(client, target, spec.expand_hops, cfg)
        case "ZOOM_IN":
            return _exec_zoom(client, target, cfg)
        case "ZOOM_OUT" | "VIEW_ALL":
            return _exec_view_all(client, cfg)
        case "SET_PERSPECTIVE":
            return _exec_set_perspective(client, spec.perspective or "All", cfg)
        case "TOGGLE_PROGRESSIVE":
            return ExecuteResult(strategy="ui:toggle_progressive", metadata={"toggled": True})
        case "DISMISS_NODE":
            return _exec_dismiss_node(target)
        case "FOCUS_CLUSTER":
            return _exec_focus_cluster(client, target, cfg)
        case "COLLAPSE_NAMESPACE":
            return _exec_collapse_namespace(client, target, cfg)
        case _:
            return ExecuteResult(strategy="ui:unknown", metadata={"ui_action": ua})


def _execute_query(client, spec: ExecuteRequest, target: str, cfg: dict) -> ExecuteResult:
    """Execute a QUERY action — semantic search + optional graph traversal."""
    q_parts = []
    if target:
        q_parts.append(target)
    q_parts.extend(spec.entities[:3])
    q_parts.extend(spec.keywords[:5])
    q = " ".join(q_parts) if q_parts else "binary analysis"

    # Build recall params
    recall_params: dict[str, Any] = {"q": q, "k": spec.k}

    # Filter by mind tags if present
    if spec.tier1:
        recall_params["labels"] = [f"mind:{t}" for t in spec.tier1]

    data = _call(client, "/recall", recall_params)
    items = data.get("items", data.get("found", []))

    result = ExecuteResult(
        strategy="query:recall",
        items=items if isinstance(items, list) else [],
        total=len(items) if isinstance(items, list) else 0,
        metadata={
            "confidence": data.get("confidence", 0),
            "query": q,
        },
    )

    # Graph traversal for depth > 1 or relationship queries
    if spec.depth > 1 and spec.entities:
        edges = _call(client, "/recall", {
            "q": " ".join(spec.entities[:3]),
            "k": spec.depth * 10,
            "collections": [cfg["edge_collection"]],
        })
        edge_items = edges.get("items", edges.get("found", []))
        result.edges = edge_items if isinstance(edge_items, list) else []

    return result


# ── Route ────────────────────────────────────────────────────────────────────

@router.post("/execute-queryspec", response_model=ExecuteResult)
def execute_queryspec_endpoint(body: ExecuteRequest) -> ExecuteResult:
    """Deterministic QuerySpec executor. No LLM — pure switch/case routing."""
    logger.info(f"Executing QuerySpec: action={body.action}, ui_action={body.ui_action}, scope={body.scope}")
    return execute_queryspec(body)
