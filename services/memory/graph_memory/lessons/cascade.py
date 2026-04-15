from __future__ import annotations
import time, os
import json
import typer
from loguru import logger
from typing import Any, Dict, List, Set, Deque, Tuple
from collections import deque
from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view

app = typer.Typer(add_completion=False)

MITIGATION_TYPES = {"solves", "mitigates"}
DEPEND_TYPES = {"depends_on"}
VERIFY_TYPES = {"verifies"}
VIOLATION_TYPES = {"violates"}


def _as_of_ts(as_of: int) -> int:
    return int(as_of) if as_of and int(as_of) > 0 else int(time.time())


def _edge_temporal_predicate() -> str:
    return (
        "ed.status=='active' "
        "AND (ed.valid_from==null OR ed.valid_from<=@as_of) "
        "AND (ed.valid_to==null OR ed.valid_to>@as_of)"
    )


def _fetch_lesson_by_title(db, title: str, scope: str):
    aql = """
    FOR l IN lessons_v2
      FILTER l.title == @t
      FILTER @scope=='' OR l.scope==@scope
      LIMIT 1
      RETURN { _id: l._id, title: l.title, scope: l.scope }
    """
    rows = list(db.aql.execute(aql, bind_vars={"t": title, "scope": scope or ""}))
    return rows[0] if rows else None


def _fetch_out_edges(db, node_id: str, as_of_ts: int) -> List[Dict[str, Any]]:
    aql = f"""
    FOR ed IN lesson_edges
      FILTER ed._from==@nid
      FILTER {_edge_temporal_predicate()}
      RETURN {{ _from: ed._from, _to: ed._to, type: ed.type }}
    """
    return list(db.aql.execute(aql, bind_vars={"nid": node_id, "as_of": as_of_ts}))


def _fetch_in_depends_on_batch(db, node_ids: List[str], as_of_ts: int) -> Dict[str, List[Dict[str, Any]]]:
    """Batch inbound depends_on: for each target Y in node_ids find edges X -> Y where X depends_on Y."""
    if not node_ids:
        return {}
    aql = f"""
    FOR ed IN lesson_edges
      FILTER ed._to IN @targets
      FILTER ed.type IN @types
      FILTER {_edge_temporal_predicate()}
      RETURN {{ _from: ed._from, _to: ed._to, type: ed.type }}
    """
    rows = list(db.aql.execute(aql, bind_vars={
        "targets": list(node_ids),
        "types": list(DEPEND_TYPES),
        "as_of": as_of_ts,
    }))
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["_to"], []).append(r)
    return out


def _fetch_in_verifies_batch(db, node_ids: List[str], as_of_ts: int) -> Dict[str, List[Dict[str, Any]]]:
    """Batch inbound verifies: for each target Y in node_ids find edges X -> Y where X verifies Y."""
    if not node_ids:
        return {}
    aql = f"""
    FOR ed IN lesson_edges
      FILTER ed._to IN @targets
      FILTER ed.type IN @types
      FILTER {_edge_temporal_predicate()}
      RETURN {{ _from: ed._from, _to: ed._to, type: ed.type }}
    """
    rows = list(db.aql.execute(aql, bind_vars={
        "targets": list(node_ids),
        "types": list(VERIFY_TYPES),
        "as_of": as_of_ts,
    }))
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["_to"], []).append(r)
    return out


def _fetch_in_violates_batch(db, node_ids: List[str], as_of_ts: int) -> Dict[str, List[Dict[str, Any]]]:
    """Batch inbound violates: for each target Y in node_ids find edges X -> Y where X violates Y."""
    if not node_ids:
        return {}
    aql = f"""
    FOR ed IN lesson_edges
      FILTER ed._to IN @targets
      FILTER ed.type IN @types
      FILTER {_edge_temporal_predicate()}
      RETURN {{ _from: ed._from, _to: ed._to, type: ed.type }}
    """
    rows = list(db.aql.execute(aql, bind_vars={
        "targets": list(node_ids),
        "types": list(VIOLATION_TYPES),
        "as_of": as_of_ts,
    }))
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["_to"], []).append(r)
    return out


def error_cascade(requirement: str, scope: str, as_of: int, depth: int) -> Dict[str, Any]:
    ensure_collections_and_view()
    db = get_db()
    as_of_ts = _as_of_ts(as_of)
    user_depth = int(depth)
    depth_cap = 4
    depth = max(1, min(user_depth, depth_cap))

    seed = _fetch_lesson_by_title(db, requirement, scope)
    if not seed:
        return {"meta": {"scope": scope, "requirement": requirement}, "items": [], "errors": ["requirement_not_found"]}

    frontier: Deque[Tuple[str, List[str], List[str]]] = deque()
    visited: Set[str] = set()
    results: List[Dict[str, Any]] = []

    seed_id = seed["_id"]
    frontier.append((seed_id, [seed_id], []))
    visited.add(seed_id)
    seed_entry = {
        "node_id": seed_id,
        "title": seed["title"],
        "path": [seed_id],
        "relation_types": [],
        "reason": "seed_requirement_failure",
        "severity": "failing",
    }
    # Optional synergy with audit: mark at_risk when outdated verifier coverage
    if os.getenv("CASCADE_OUTDATED_VERIFIER_AT_RISK") == "1":
        try:
            from .audit import contradictions as _audit
            audit = _audit(scope=scope)
            outdated_ids = {
                it.get("to") for it in audit.get("items", {}).get("invalid_temporal", [])
                if it and it.get("reason") in ("verifier_outdated_coverage", "verifier_no_window_overlap")
            }
            if seed_id in outdated_ids:
                seed_entry["severity"] = "at_risk"
                seed_entry["reason"] = "seed_requirement_failure_outdated_coverage"
        except Exception as exc:
            logger.error("Outdated verifier audit check failed: {}", exc)
    results.append(seed_entry)

    violation_escalate = os.getenv("RISK_VIOLATION_ESCALATE") == "1"

    while frontier:
        # breadth layer
        layer: List[Tuple[str, List[str], List[str]]] = []
        while frontier:
            layer.append(frontier.popleft())
        layer_ids = [c for c, _, _ in layer]
        inbound_depends_map = _fetch_in_depends_on_batch(db, layer_ids, as_of_ts)
        inbound_verifies_map = _fetch_in_verifies_batch(db, layer_ids, as_of_ts)
        inbound_violates_map = _fetch_in_violates_batch(db, layer_ids, as_of_ts)

        next_frontier: Deque[Tuple[str, List[str], List[str]]] = deque()
        for current, path_ids, path_rels in layer:
            if len(path_ids) - 1 >= depth:
                continue

            # outward edges (verifies/mitigation/violates)
            for e in _fetch_out_edges(db, current, as_of_ts):
                rel_type = e.get("type") or ""
                nxt = e["_to"]
                if nxt in visited:
                    continue
                propagate = False
                reason = ""
                severity = "at_risk"
                if rel_type in VERIFY_TYPES:
                    propagate = True
                    reason = "verifier_inactive"
                elif rel_type in MITIGATION_TYPES:
                    propagate = True
                    reason = "mitigation_inactive"
                elif rel_type in VIOLATION_TYPES:
                    propagate = True
                    reason = "violation_link"
                    if violation_escalate:
                        severity = "failing"
                if not propagate:
                    continue
                visited.add(nxt)
                new_path_ids = path_ids + [nxt]
                new_path_rels = path_rels + [rel_type]
                try:
                    title = list(db.aql.execute("LET d = DOCUMENT(@id) RETURN d.title", bind_vars={"id": nxt}))[0]
                except Exception as exc:
                    logger.error("Title lookup failed for {}: {}", nxt, exc)
                    title = nxt
                results.append({
                    "node_id": nxt,
                    "title": title,
                    "path": new_path_ids,
                    "relation_types": new_path_rels,
                    "reason": reason,
                    "severity": severity,
                })
                next_frontier.append((nxt, new_path_ids, new_path_rels))

            # inbound depends_on propagation from batch
            for e in inbound_depends_map.get(current, []):
                depender = e["_from"]
                if depender in visited:
                    continue
                visited.add(depender)
                new_path_ids = path_ids + [depender]
                new_path_rels = path_rels + [e.get("type") or "depends_on"]
                try:
                    title = list(db.aql.execute("LET d = DOCUMENT(@id) RETURN d.title", bind_vars={"id": depender}))[0]
                except Exception as exc:
                    logger.error("Title lookup failed for {}: {}", depender, exc)
                    title = depender
                results.append({
                    "node_id": depender,
                    "title": title,
                    "path": new_path_ids,
                    "relation_types": new_path_rels,
                    "reason": "depends_on_upstream_failure",
                    "severity": "at_risk",
                })
                next_frontier.append((depender, new_path_ids, new_path_rels))

            # inbound verifies propagation from batch (verifiers of failing requirement)
            for e in inbound_verifies_map.get(current, []):
                verifier = e["_from"]
                if verifier in visited:
                    continue
                visited.add(verifier)
                new_path_ids = path_ids + [verifier]
                new_path_rels = path_rels + [e.get("type") or "verifies"]
                try:
                    title = list(db.aql.execute("LET d = DOCUMENT(@id) RETURN d.title", bind_vars={"id": verifier}))[0]
                except Exception as exc:
                    logger.error("Title lookup failed for {}: {}", verifier, exc)
                    title = verifier
                results.append({
                    "node_id": verifier,
                    "title": title,
                    "path": new_path_ids,
                    "relation_types": new_path_rels,
                    "reason": "verifier_inactive",
                    "severity": "at_risk",
                })
                next_frontier.append((verifier, new_path_ids, new_path_rels))

            # inbound violates propagation from batch (threats that violate failing requirement)
            for e in inbound_violates_map.get(current, []):
                violator = e["_from"]
                if violator in visited:
                    continue
                visited.add(violator)
                new_path_ids = path_ids + [violator]
                new_path_rels = path_rels + [e.get("type") or "violates"]
                try:
                    title = list(db.aql.execute("LET d = DOCUMENT(@id) RETURN d.title", bind_vars={"id": violator}))[0]
                except Exception as exc:
                    logger.error("Title lookup failed for {}: {}", violator, exc)
                    title = violator
                severity = "failing" if violation_escalate else "at_risk"
                results.append({
                    "node_id": violator,
                    "title": title,
                    "path": new_path_ids,
                    "relation_types": new_path_rels,
                    "reason": "violation_link",
                    "severity": severity,
                })
                next_frontier.append((violator, new_path_ids, new_path_rels))

        frontier.extend([t for t in next_frontier if len(t[1]) - 1 < depth])

    return {
        "meta": {"requirement": requirement, "scope": scope, "as_of": as_of_ts, "depth": depth, "depth_cap_exceeded": user_depth > depth_cap},
        "items": results,
        "errors": [],
    }


@app.command("errors")
def errors_cli(
    requirement: str = typer.Option(..., help="Requirement title"),
    scope: str = typer.Option("", help="Scope filter"),
    as_of: int = typer.Option(0, help="Unix timestamp (0=now)"),
    depth: int = typer.Option(2, help="Cascade depth 1..4"),
    json_out: bool = typer.Option(True, "--json"),
):
    res = error_cascade(requirement=requirement, scope=scope, as_of=as_of, depth=depth)
    if json_out:
        print(json.dumps(res))
    else:
        typer.echo(f"cascade items={len(res.get('items') or [])}")
