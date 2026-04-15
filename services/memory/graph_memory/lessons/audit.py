from __future__ import annotations
import time, os
import json
import typer
from typing import Any, Dict, List, Tuple, Set
from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view

app = typer.Typer(add_completion=False)

SYMMETRIC_TYPES = {"duplicates", "similar_to", "related"}
MITIGATION_TYPES = {"solves", "mitigates"}
VIOLATION_TYPES = {"violates"}
REQUIREMENT_TAGS = {"requirement"}
THREAT_TAGS = {"threat", "vulnerability"}
CONTROL_TAGS = {"control", "mitigation"}


def _as_of_ts(as_of: int) -> int:
    return int(as_of) if as_of and int(as_of) > 0 else int(time.time())


def _edge_temporal_predicate() -> str:
    return (
        "e.status=='active' "
        "AND (e.valid_from==null OR e.valid_from<=@as_of) "
        "AND (e.valid_to==null OR e.valid_to>@as_of)"
    )


def _fetch_active_edges(db, scope: str, as_of_ts: int) -> List[Dict[str, Any]]:
    aql = f"""
    FOR e IN lesson_edges
      LET lf = DOCUMENT('lessons', SPLIT(e._from,'/')[1])
      LET lt = DOCUMENT('lessons', SPLIT(e._to,'/')[1])
      FILTER {_edge_temporal_predicate()}
      FILTER @scope=='' OR (lf!=null AND lf.scope==@scope) OR (lt!=null AND lt.scope==@scope)
      RETURN {{ _id: e._id, _from: e._from, _to: e._to, type: e.type, valid_from: e.valid_from, valid_to: e.valid_to, status: e.status }}
    """
    return list(db.aql.execute(aql, bind_vars={"as_of": as_of_ts, "scope": scope or ""}))


def _fetch_lessons(db, scope: str) -> Dict[str, Dict[str, Any]]:
    aql = """
    FOR l IN lessons_v2
      FILTER @scope=='' OR l.scope==@scope
      RETURN { _id: l._id, title: l.title, tags: l.tags, scope: l.scope, updated_at: l.updated_at }
    """
    out: Dict[str, Dict[str, Any]] = {}
    for row in db.aql.execute(aql, bind_vars={"scope": scope or ""}):
        out[row["_id"]] = row
    return out


def _is_requirement(lesson: Dict[str, Any]) -> bool:
    tags = set((lesson.get("tags") or []))
    title = (lesson.get("title") or "").strip()
    return bool(tags & REQUIREMENT_TAGS) or title.startswith("R")


def _is_threat(lesson: Dict[str, Any]) -> bool:
    tags = set((lesson.get("tags") or []))
    title = (lesson.get("title") or "").strip()
    return bool(tags & THREAT_TAGS) or title.startswith("T")


def _is_control(lesson: Dict[str, Any]) -> bool:
    tags = set((lesson.get("tags") or []))
    return bool(tags & CONTROL_TAGS)


def contradictions(scope: str, as_of: int = 0, within_days: int = 0) -> Dict[str, Any]:
    ensure_collections_and_view()
    db = get_db()
    as_of_ts = _as_of_ts(as_of)
    edges = _fetch_active_edges(db, scope, as_of_ts)
    lessons = _fetch_lessons(db, scope)

    pair_map: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    reverse_lookup: Set[Tuple[str, str, str]] = set()
    for e in edges:
        frm, to, ty = e["_from"], e["_to"], e.get("type") or ""
        canonical = (min(frm, to), max(frm, to))
        pair_map.setdefault(canonical, []).append(e)
        reverse_lookup.add((frm, to, ty))

    edge_conflicts: List[Dict[str, Any]] = []
    mixed_type: List[Dict[str, Any]] = []
    incompatible_sets = [
        {"duplicates", "solves"},
        {"duplicates", "mitigates"},
        {"solves", "mitigates"},
        {"duplicates", "violates"},
        {"mitigates", "violates"},
    ]

    for (a, b), plist in pair_map.items():
        types_here = {p.get("type") for p in plist if p.get("type")}
        for p in plist:
            ty = p.get("type")
            if ty in SYMMETRIC_TYPES:
                if (p["_to"], p["_from"], ty) not in reverse_lookup:
                    edge_conflicts.append({
                        "edge_id": p["_id"],
                        "reason": "missing_reverse_symmetric",
                        "type": ty,
                        "from": p["_from"],
                        "to": p["_to"],
                    })
            else:
                if (p["_to"], p["_from"], ty) in reverse_lookup:
                    edge_conflicts.append({
                        "edge_id": p["_id"],
                        "reason": "unexpected_reverse_for_nonsymmetric",
                        "type": ty,
                        "from": p["_from"],
                        "to": p["_to"],
                    })
        for inc in incompatible_sets:
            if len(types_here & inc) > 1:
                mixed_type.append({
                    "pair": [a, b],
                    "types": sorted(types_here),
                    "reason": "incompatible_type_combo",
                })

    invalid_temporal: List[Dict[str, Any]] = []
    verifier_latest: Dict[str, int] = {}
    # Optional staleness heuristic for verifies edges
    stale_days_raw = os.getenv("VERIFIES_MAX_STALENESS_DAYS", "0")
    try:
        stale_days = int(stale_days_raw)
    except ValueError:
        stale_days = 0
    stale_cutoff = as_of_ts - stale_days * 86400 if stale_days > 0 else None

    for e in edges:
        ty = e.get("type")
        frm, to = e["_from"], e["_to"]
        vf = e.get("valid_from")
        vt = e.get("valid_to")
        # invalid window
        if vf is not None and vt is not None and vf >= vt:
            invalid_temporal.append({
                "edge_id": e["_id"],
                "from": frm,
                "to": to,
                "type": ty,
                "reason": "invalid_window",
            })
        # mutual verifies
        if ty == "verifies" and (to, frm, "verifies") in reverse_lookup:
            invalid_temporal.append({
                "edge_id": e["_id"],
                "from": frm,
                "to": to,
                "type": ty,
                "reason": "mutual_verifies_conflict",
            })
        if ty == "verifies" and stale_cutoff:
            v = lessons.get(frm) or {}
            r = lessons.get(to) or {}
            v_upd = v.get("updated_at") or 0
            r_upd = r.get("updated_at") or 0
            if v_upd < stale_cutoff and r_upd >= stale_cutoff:
                invalid_temporal.append({
                    "edge_id": e["_id"],
                    "from": frm,
                    "to": to,
                    "type": ty,
                    "reason": "verifier_stale",
                })
        if ty == "verifies":
            vu = lessons.get(frm, {}).get("updated_at") or 0
            cur = verifier_latest.get(to, 0)
            if vu > cur:
                verifier_latest[to] = vu
        # conflicting non-symmetric reverse (exclude depends_on which we allow for modeling flows)
        if ty not in SYMMETRIC_TYPES and ty != "depends_on" and (to, frm, ty) in reverse_lookup:
            invalid_temporal.append({
                "edge_id": e["_id"],
                "from": frm,
                "to": to,
                "type": ty,
                "reason": "conflicting_nonsymmetric_reverse",
            })

    inbound_verifies: Dict[str, int] = {}
    for e in edges:
        if e.get("type") == "verifies":
            inbound_verifies[e["_to"]] = inbound_verifies.get(e["_to"], 0) + 1

    missing_verification: List[Dict[str, Any]] = []
    for lid, lesson in lessons.items():
        if _is_requirement(lesson) and inbound_verifies.get(lid, 0) == 0:
            missing_verification.append({
                "lesson_id": lid,
                "title": lesson.get("title"),
                "reason": "no_active_verifies",
            })

    # Outdated coverage rule (requirement updated_at >> newest verifier updated_at)
    outdated_gap_days_raw = os.getenv("VERIFIER_OUTDATED_GAP_DAYS", "0")
    try:
        outdated_gap_days = int(outdated_gap_days_raw)
    except ValueError:
        outdated_gap_days = 0
    if outdated_gap_days > 0:
        gap_sec = outdated_gap_days * 86400
        for rid, lesson in lessons.items():
            if _is_requirement(lesson):
                req_upd = lesson.get("updated_at") or 0
                newest_ver = verifier_latest.get(rid, 0)
                if newest_ver and req_upd - newest_ver >= gap_sec:
                    invalid_temporal.append({
                        "edge_id": None,
                        "from": None,
                        "to": rid,
                        "type": "verifies",
                        "reason": "verifier_outdated_coverage",
                        "gap_days": round((req_upd - newest_ver) / 86400, 2),
                    })

    # Strict window overlap placeholder (future lesson validity)
    if os.getenv("VERIFIER_STRICT_WINDOW") == "1":
        for rid, lesson in lessons.items():
            if not _is_requirement(lesson):
                continue
            r_vfrom = lesson.get("valid_from")
            r_vto = lesson.get("valid_to")
            if r_vfrom is None and r_vto is None:
                continue
            ver_edges = [x for x in edges if x.get("type") == "verifies" and x["_to"] == rid]
            if not ver_edges:
                continue
            overlap_found = False
            for ev in ver_edges:
                v_lesson = lessons.get(ev["_from"], {})
                vf = v_lesson.get("valid_from")
                vt = v_lesson.get("valid_to")
                if (vf is not None and vt is not None and r_vfrom is not None and r_vto is not None):
                    if not (vt <= r_vfrom or vf >= r_vto):
                        overlap_found = True
                        break
                else:
                    overlap_found = True
                    break
            if not overlap_found:
                invalid_temporal.append({
                    "edge_id": None,
                    "from": None,
                    "to": rid,
                    "type": "verifies",
                    "reason": "verifier_no_window_overlap",
                })

    inbound_mitigations: Dict[str, int] = {}
    for e in edges:
        if e.get("type") in MITIGATION_TYPES:
            inbound_mitigations[e["_to"]] = inbound_mitigations.get(e["_to"], 0) + 1

    missing_mitigation: List[Dict[str, Any]] = []
    for lid, lesson in lessons.items():
        if _is_threat(lesson) and inbound_mitigations.get(lid, 0) == 0:
            missing_mitigation.append({
                "lesson_id": lid,
                "title": lesson.get("title"),
                "reason": "no_active_mitigation",
            })

    connected: Set[str] = set()
    for e in edges:
        connected.add(e["_from"])
        connected.add(e["_to"])
    orphans: List[Dict[str, Any]] = []
    for lid, lesson in lessons.items():
        if _is_control(lesson) and lid not in connected:
            orphans.append({
                "lesson_id": lid,
                "title": lesson.get("title"),
                "reason": "no_edges",
            })

    result = {
        "meta": {
            "scope": scope,
            "as_of": as_of_ts,
            "categories": [
                "edge_conflicts",
                "mixed_type",
                "invalid_temporal",
                "missing_verification",
                "missing_mitigation",
                "orphans",
            ],
        },
        "items": {
            "edge_conflicts": edge_conflicts,
            "mixed_type": mixed_type,
            "invalid_temporal": invalid_temporal,
            "missing_verification": missing_verification,
            "missing_mitigation": missing_mitigation,
            "orphans": orphans,
        },
        "errors": [],
    }
    if os.getenv("AUDIT_REMEDIATION_HINTS") == "1":
        remediation = {
            "add_mitigation_for": [{"threat_id": m["lesson_id"], "title": (lessons.get(m["lesson_id"], {}) or {}).get("title") } for m in missing_mitigation],
            "add_verification_for": [{"requirement_id": r["lesson_id"], "title": (lessons.get(r["lesson_id"], {}) or {}).get("title") } for r in missing_verification],
            "link_control": [{"control_id": c["lesson_id"], "title": (lessons.get(c["lesson_id"], {}) or {}).get("title") } for c in orphans],
        }
        result["meta"]["hints_enabled"] = True
        result["remediation"] = remediation
    else:
        result["meta"]["hints_enabled"] = False
    return result


@app.command("contradictions")
def contradictions_cli(
    scope: str = typer.Option("", help="Scope filter"),
    as_of: int = typer.Option(0, help="Unix timestamp (0=now)"),
    within_days: int = typer.Option(0, help="Recent window (days)"),
    json_out: bool = typer.Option(True, "--json"),
):
    res = contradictions(scope=scope, as_of=as_of, within_days=within_days)
    if json_out:
        print(json.dumps(res))
    else:
        for cat, arr in (res.get("items") or {}).items():
            typer.echo(f"{cat}: {len(arr)}")
