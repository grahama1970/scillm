from __future__ import annotations
import os, subprocess, hashlib, shlex, pathlib
import time, json, typer
from loguru import logger
from typing import Any, Dict, List, Set, Tuple, Optional
from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view

app = typer.Typer(add_completion=False)


def _now() -> int:
    return int(time.time())


def _sha256(txt: str) -> str:
    return "sha256:" + hashlib.sha256(txt.encode("utf-8")).hexdigest()


def _try_import_certainly() -> Optional[Any]:
    try:
        import certainly.formalize as formalize  # type: ignore
        return formalize
    except Exception as exc:
        logger.error("certainly.formalize not available: {}", exc)
        return None


def _formalize_via_module(module, baseline: str) -> List[Dict[str, Any]]:
    try:
        data = list(module.export_baseline(baseline))
        return data
    except Exception as e:
        raise RuntimeError(f"module_formalize_error:{e}")


def _formalize_via_subprocess(baseline: str) -> List[Dict[str, Any]]:
    cmd = f"certainly export --baseline {shlex.quote(baseline)} --format jsonl"
    try:
        proc = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
    except subprocess.TimeoutError:
        raise RuntimeError("subprocess_timeout")
    if proc.returncode != 0:
        raise RuntimeError(f"subprocess_error:{proc.stderr.strip()[:400]}")
    records: List[Dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            records.append(obj)
        except Exception as exc:
            logger.error("JSONL line parse error: {}", exc)
            raise RuntimeError("jsonl_parse_error")
    return records


def _normalize_formalized(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for r in records:
        rid = (r.get("req_id") or "").strip()
        if not rid:
            continue
        sem_hash = r.get("sem_hash")
        text_basis = r.get("norm_text") or r.get("raw_text") or ""
        if not sem_hash:
            if text_basis:
                sem_hash = _sha256(text_basis.lower().strip())
            else:
                continue
        tags = r.get("tags") or []
        out[rid] = {
            "req_id": rid,
            "sem_hash": sem_hash,
            "tags": tags,
            "updated_at": r.get("updated_at"),
            "parameters": r.get("parameters") or [],
            "allocations": r.get("baseline_allocations") or r.get("allocations") or [],
            "title": r.get("section_title") or r.get("title") or rid,
        }
    return out

def _formalization_cache_dir() -> pathlib.Path:
    root = os.getenv("CERTAINLY_CACHE_DIR") or ".cache/certainly"
    p = pathlib.Path(root)
    p.mkdir(parents=True, exist_ok=True)
    return p

def _formalization_cache_key(baseline: str) -> pathlib.Path:
    return _formalization_cache_dir() / f"{baseline}.jsonl"

def _load_formalization_cache(baseline: str) -> Optional[Dict[str, Dict[str, Any]]]:
    if os.getenv("CERTAINLY_CACHE", "1").lower() not in ("1", "true"):  # default on
        return None
    fp = _formalization_cache_key(baseline)
    if not fp.exists():
        return None
    try:
        recs = []
        for line in fp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            recs.append(json.loads(line))
        return _normalize_formalized(recs)
    except Exception as exc:
        logger.error("Failed to load formalization cache for baseline: {}", exc)
        return None

def _store_formalization_cache(baseline: str, mapping: Dict[str, Dict[str, Any]]) -> None:
    if os.getenv("CERTAINLY_CACHE", "1").lower() not in ("1", "true"):
        return
    if not mapping:
        return
    fp = _formalization_cache_key(baseline)
    try:
        with fp.open("w", encoding="utf-8") as f:
            for v in mapping.values():
                f.write(json.dumps(v, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.error("Failed to store formalization cache: {}", exc)


def _formalize_baseline(baseline: str) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    meta = {"baseline": baseline, "count": 0, "failed": False, "mode": None}
    if not baseline:
        return {}, meta
    module = _try_import_certainly()
    cached = _load_formalization_cache(baseline)
    if cached:
        meta["mode"] = "cache"
        meta["count"] = len(cached)
        return cached, meta
    first_err: Optional[str] = None
    records: List[Dict[str, Any]] = []
    if module:
        try:
            meta["mode"] = "module"
            records = _formalize_via_module(module, baseline)
        except Exception as e:
            first_err = str(e)
    if not records:
        try:
            meta["mode"] = meta["mode"] or "subprocess"
            records = _formalize_via_subprocess(baseline)
        except Exception as e:
            if not first_err:
                first_err = str(e)
    mapping = _normalize_formalized(records)
    meta["count"] = len(mapping)
    if mapping:
        _store_formalization_cache(baseline, mapping)
        return mapping, meta
    meta["failed"] = True
    if first_err:
        meta["error"] = first_err
    return {}, meta


def _structured_diff(formA: Dict[str, Dict[str, Any]], formB: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    a_keys = set(formA.keys())
    b_keys = set(formB.keys())
    added = [formB[k] for k in sorted(b_keys - a_keys)]
    removed = [formA[k] for k in sorted(a_keys - b_keys)]
    modified: List[Dict[str, Any]] = []
    for k in sorted(a_keys & b_keys):
        ra, rb = formA[k], formB[k]
        changes: Dict[str, Any] = {}
        if ra["sem_hash"] != rb["sem_hash"]:
            changes["sem_hash_changed"] = True
        pa, pb = set(ra.get("parameters", [])), set(rb.get("parameters", []))
        if pa != pb:
            changes["parameters_added"] = sorted(pb - pa)
            changes["parameters_removed"] = sorted(pa - pb)
        aa, ab = set(ra.get("allocations", [])), set(rb.get("allocations", []))
        if aa != ab:
            changes["allocations_added"] = sorted(ab - aa)
            changes["allocations_removed"] = sorted(aa - ab)
        if changes:
            # granular composite change_type
            s = bool(changes.get("sem_hash_changed"))
            p = bool(changes.get("parameters_added") or changes.get("parameters_removed"))
            a = bool(changes.get("allocations_added") or changes.get("allocations_removed"))
            if s and p and a:
                change_type = "semantic_param_alloc"
            elif s and p:
                change_type = "semantic_param"
            elif s and a:
                change_type = "semantic_alloc"
            elif p and a:
                change_type = "param_alloc"
            elif p and not (s or a):
                change_type = "param_only"
            elif a and not (s or p):
                change_type = "alloc_only"
            elif s and not (p or a):
                change_type = "semantic_text_change"
            else:
                change_type = "other"
            modified.append({"req_id": k, "title": rb["title"], "changed": changes, "change_type": change_type})
    # lightweight semantic clustering by title keywords
    def _cluster(rec: Dict[str, Any]) -> str:
        t = (rec.get("title") or "").lower()
        TAG_MAP = [
            ("crypto", ("crypto","cryptograph","encrypt","signature","sign ")),
            ("identity", ("auth","identity","iam","account")),
            ("logging", ("log","audit","trace")),
            ("network", ("network","firewall","transport")),
            ("data_protection", ("data","pii","privacy","mask","tokenization")),
            ("configuration", ("config","configuration","parameter")),
        ]
        for name, keys in TAG_MAP:
            if any(k in t for k in keys):
                return name
        return "other"
    clusters: Dict[str,int] = {}
    for m in modified:
        m["cluster"] = _cluster(m)
        clusters[m["cluster"]] = clusters.get(m["cluster"], 0) + 1
        # Optional policy severity mapping per change_type
        policy_file = os.getenv("REQUIREMENT_DIFF_POLICY_FILE", "").strip()
        if policy_file and os.path.exists(policy_file):
            try:
                pol = json.loads(open(policy_file, "r", encoding="utf-8").read())
                sev = pol.get(m.get("change_type"))
                if sev:
                    m["policy_severity"] = sev
            except Exception as exc:
                logger.error("Failed to load diff policy file: {}", exc)
    return {"added": added, "removed": removed, "modified": modified, "semantic_cluster_counts": clusters}


def _fetch_lessons_by_baseline(db, baseline: str, scope: str) -> Dict[str, Dict[str, Any]]:
    if not baseline:
        return {}
    aql = """
    FOR l IN lessons_v2
      FILTER (@scope=='' OR l.scope==@scope)
      FILTER l.tags ANY LIKE @pattern
      RETURN { _id:l._id, title:l.title, tags:l.tags, scope:l.scope, updated_at:l.updated_at }
    """
    pattern = f"baseline:{baseline}"
    rows = list(db.aql.execute(aql, bind_vars={"scope": scope or "", "pattern": pattern}))
    return {r["_id"]: r for r in rows}


def _fetch_controls_evidence(db, scope: str):
    aql = """
    FOR l IN lessons_v2
      FILTER (@scope=='' OR l.scope==@scope)
      FILTER l.tags ANY LIKE 'control:%' OR l.tags ANY LIKE 'evidence:%'
      RETURN { _id:l._id, title:l.title, tags:l.tags, scope:l.scope, updated_at:l.updated_at }
    """
    rows = list(db.aql.execute(aql, bind_vars={"scope": scope or ""}))
    return rows


def _fetch_edges_for_ids(db, ids: Set[str]) -> List[Dict[str, Any]]:
    if not ids:
        return []
    aql = """
    FOR e IN lesson_edges
      FILTER e._from IN @ids OR e._to IN @ids
      RETURN { _id:e._id, _from:e._from, _to:e._to, type:e.type, status:e.status }
    """
    return list(db.aql.execute(aql, bind_vars={"ids": list(ids)}))


def requirement_diff(baseline_a: str, baseline_b: str, scope: str, require_formalization: bool | None = None) -> Dict[str, Any]:
    ensure_collections_and_view()
    db = get_db()
    strict_env = os.getenv("CERTAINLY_ENABLED") == "1"
    allow_partial = os.getenv("CERTAINLY_ALLOW_PARTIAL") == "1"
    if require_formalization is None:
        require_formalization = strict_env

    # Always attempt formalization first; only fail if strict/require_formalization without partial allowed
    formal_a, meta_a = _formalize_baseline(baseline_a)
    formal_b, meta_b = _formalize_baseline(baseline_b)
    formal_mode = "attempted"
    if (meta_a.get("failed") or meta_b.get("failed")):
        if require_formalization and not allow_partial:
            return {
                "meta": {
                    "baseline_a": baseline_a,
                    "baseline_b": baseline_b,
                    "scope": scope,
                    "ts": _now(),
                    "formalization": {"mode": "failed", "baseline_a": meta_a, "baseline_b": meta_b},
                },
                "items": {},
                "errors": ["formalization_failed"],
            }
    else:
        # Both formalized successfully: use structured diff
        struct = _structured_diff(formal_a, formal_b)
        return {
            "meta": {
                "baseline_a": baseline_a,
                "baseline_b": baseline_b,
                "scope": scope,
                "ts": _now(),
                "formalization": {"mode": "structured", "baseline_a": meta_a, "baseline_b": meta_b},
            },
            "items": struct,
            "errors": [],
        }

    # fallback legacy heuristic
    a = _fetch_lessons_by_baseline(db, baseline_a, scope)
    b = _fetch_lessons_by_baseline(db, baseline_b, scope)
    ids_a = set(a.keys())
    ids_b = set(b.keys())
    added = [b[i] for i in sorted(ids_b - ids_a)]
    removed = [a[i] for i in sorted(ids_a - ids_b)]
    modified: List[Dict[str, Any]] = []
    for i in sorted(ids_a & ids_b):
        la, lb = a[i], b[i]
        tags_a = set(la.get("tags") or [])
        tags_b = set(lb.get("tags") or [])
        updated_changed = la.get("updated_at") != lb.get("updated_at")
        tags_changed = tags_a != tags_b
        if updated_changed or tags_changed:
            modified.append({
                "_id": i,
                "title": lb.get("title"),
                "changed": {
                    "updated_at_old": la.get("updated_at"),
                    "updated_at_new": lb.get("updated_at"),
                    "tags_added": sorted(tags_b - tags_a),
                    "tags_removed": sorted(tags_a - tags_b),
                },
            })
    return {
        "meta": {
            "baseline_a": baseline_a,
            "baseline_b": baseline_b,
            "scope": scope,
            "ts": _now(),
            "formalization": {"mode": ("failed" if (meta_a.get("failed") or meta_b.get("failed")) else formal_mode), "baseline_a": meta_a, "baseline_b": meta_b},
        },
        "items": {"added": added, "removed": removed, "modified": modified},
        "errors": [],
    }


def compliance_coverage(baseline: str, scope: str) -> Dict[str, Any]:
    ensure_collections_and_view()
    db = get_db()
    records = _fetch_controls_evidence(db, scope)
    controls = [r for r in records if any((t or "").startswith("control:") for t in (r.get("tags") or []))]
    evidence = [r for r in records if any((t or "").startswith("evidence:") for t in (r.get("tags") or []))]
    control_ids = {c["_id"] for c in controls}
    edges = _fetch_edges_for_ids(db, control_ids)
    covered: Set[str] = set()
    for e in edges:
        if e["_from"] in control_ids or e["_to"] in control_ids:
            covered.add(e["_from"]) ; covered.add(e["_to"])
    relevant_controls = set(control_ids)
    covered_controls = relevant_controls & covered
    coverage_pct = round(100.0 * (len(covered_controls) / max(1, len(relevant_controls))), 2)
    gaps = sorted(relevant_controls - covered_controls)
    return {
        "meta": {"baseline": baseline, "scope": scope, "coverage_pct": coverage_pct, "ts": _now()},
        "items": {"gaps": [{"control_id": g} for g in gaps], "coverage_count": len(covered_controls), "control_count": len(relevant_controls)},
        "errors": [],
    }


def requirement_cascade(requirement: str, scope: str, depth: int) -> Dict[str, Any]:
    from .cascade import error_cascade
    base = error_cascade(requirement=requirement, scope=scope, as_of=_now(), depth=depth)
    base["meta"]["mode"] = "requirement_change_simulation"
    return base


def evidence_trace(control: str, scope: str) -> Dict[str, Any]:
    ensure_collections_and_view()
    db = get_db()
    aql = """
    FOR l IN lessons_v2
      FILTER @scope=='' OR l.scope==@scope
      FILTER l._id==@control OR l.title==@control
      LIMIT 1
      RETURN { _id:l._id, title:l.title, tags:l.tags, scope:l.scope }
    """
    rows = list(db.aql.execute(aql, bind_vars={"scope": scope or "", "control": control}))
    if not rows:
        return {"meta": {"control": control, "scope": scope}, "items": {}, "errors": ["control_not_found"]}
    root = rows[0]
    aql_edges = """
    FOR e IN lesson_edges
      FILTER e._from==@cid OR e._to==@cid
      LET otherId = e._from==@cid ? e._to : e._from
      LET other = DOCUMENT(otherId)
      FILTER other != null
      FILTER other.tags ANY LIKE 'evidence:%'
      RETURN { edge_id:e._id, evidence_id:other._id, evidence_title:other.title, type:e.type, updated:other.updated_at }
    """
    artifacts = list(db.aql.execute(aql_edges, bind_vars={"cid": root["_id"]}))
    raw_days = os.getenv("EVIDENCE_FRESH_DAYS", "90")
    try:
        days = max(1, int(raw_days))
    except ValueError:
        days = 90
    cutoff = _now() - days * 86400
    enriched = []
    now_ts = _now()
    for a in artifacts:
        upd = a.get("updated") or 0
        age_days = round(((now_ts - upd) / 86400), 2) if upd else None
        fresh = bool(upd >= cutoff)
        a["fresh"] = fresh
        a["age_days"] = age_days
        a["freshness_reason"] = "recent_update" if fresh else "older_than_threshold"
        enriched.append(a)
    freshness = "stale" if any(not a["fresh"] for a in enriched) else "fresh"
    return {"meta": {"control": control, "scope": scope, "freshness": freshness, "freshness_days": days, "ts": _now()}, "items": {"artifacts": enriched}, "errors": []}


def _risk_weights(scope: str) -> Tuple[int, int, int, int]:
    def _coerce(name: str, default: int) -> int:
        v = os.getenv(name)
        if v is None:
            return default
        try:
            i = int(v)
            return max(0, i)
        except ValueError:
            return default
    # Defaults via env first
    base = (
        _coerce("RISK_W_EDGE_CONFLICTS", 1),
        _coerce("RISK_W_MISSING_MITIGATION", 3),
        _coerce("RISK_W_MISSING_VERIFICATION", 2),
        _coerce("RISK_W_INVALID_TEMPORAL", 2),
    )
    # Load file and merge, then re-apply env (env overrides file)
    path = (os.getenv("RISK_SCOPE_WEIGHTS_FILE") or "").strip()
    if path and os.path.exists(path):
        try:
            data = json.loads(open(path, "r", encoding="utf-8").read())
            if isinstance(data, dict):
                cfg = data.get(scope) or data.get("*")
                if isinstance(cfg, dict):
                    base = (
                        int(cfg.get("edge_conflicts", base[0])),
                        int(cfg.get("missing_mitigation", base[1])),
                        int(cfg.get("missing_verification", base[2])),
                        int(cfg.get("invalid_temporal", base[3])),
                    )
        except Exception as exc:
            logger.error("Failed to load risk scope weights file: {}", exc)
    # Env overrides after file
    base = (
        _coerce("RISK_W_EDGE_CONFLICTS", base[0]),
        _coerce("RISK_W_MISSING_MITIGATION", base[1]),
        _coerce("RISK_W_MISSING_VERIFICATION", base[2]),
        _coerce("RISK_W_INVALID_TEMPORAL", base[3]),
    )
    return base


def risk_matrix(scenario_set: str, scope: str) -> Dict[str, Any]:
    from .audit import contradictions
    audit_res = contradictions(scope=scope)
    seeds = {
        "edge_conflicts": len(audit_res["items"]["edge_conflicts"]),
        "missing_mitigation": len(audit_res["items"]["missing_mitigation"]),
        "missing_verification": len(audit_res["items"]["missing_verification"]),
        "invalid_temporal": len(audit_res["items"]["invalid_temporal"]),
    }
    w_edge, w_mitig, w_verify, w_temp = _risk_weights(scope)
    score = seeds["edge_conflicts"] * w_edge + seeds["missing_mitigation"] * w_mitig + seeds["missing_verification"] * w_verify + seeds["invalid_temporal"] * w_temp
    if score >= 20:
        bucket = "critical"
    elif score >= 10:
        bucket = "high"
    elif score >= 5:
        bucket = "medium"
    else:
        bucket = "low"
    total = sum(seeds.values()) or 1
    components_pct = {k: round(v * 100.0 / total, 2) for k, v in seeds.items()}
    weighted = {
        "edge_conflicts": seeds["edge_conflicts"] * w_edge,
        "missing_mitigation": seeds["missing_mitigation"] * w_mitig,
        "missing_verification": seeds["missing_verification"] * w_verify,
        "invalid_temporal": seeds["invalid_temporal"] * w_temp,
    }
    contrib_sorted = sorted(weighted.items(), key=lambda kv: kv[1], reverse=True)
    return {
        "meta": {"scenario_set": scenario_set, "scope": scope, "score": score, "bucket": bucket, "risk_weights": {"edge_conflicts": w_edge, "missing_mitigation": w_mitig, "missing_verification": w_verify, "invalid_temporal": w_temp}, "ts": _now()},
        "items": {"scenarios": [{"name": "baseline", "score": score, "components": seeds, "components_pct": components_pct, "weighted_contributions": contrib_sorted}]},
        "errors": [],
    }


@app.command("diff")
def diff_cmd(baseline_a: str = typer.Option(...), baseline_b: str = typer.Option(...), scope: str = typer.Option("", help="Scope"), require_formalization: bool = typer.Option(False, "--require-formalization/--no-require-formalization", help="Force Certainly formalization (fail if not possible)"), json_out: bool = typer.Option(True, "--json")):
    out = requirement_diff(baseline_a, baseline_b, scope, require_formalization=require_formalization)
    print(json.dumps(out, ensure_ascii=False) if json_out else str(out))


@app.command("coverage")
def coverage_cmd(baseline: str = typer.Option("", help="Baseline"), scope: str = typer.Option("", help="Scope"), json_out: bool = typer.Option(True, "--json")):
    out = compliance_coverage(baseline, scope)
    print(json.dumps(out, ensure_ascii=False) if json_out else str(out))


@app.command("cascade")
def cascade_cmd(requirement: str = typer.Option(...), scope: str = typer.Option("", help="Scope"), depth: int = typer.Option(2), json_out: bool = typer.Option(True, "--json")):
    out = requirement_cascade(requirement, scope, depth)
    print(json.dumps(out, ensure_ascii=False) if json_out else str(out))


@app.command("trace")
def trace_cmd(control: str = typer.Option(...), scope: str = typer.Option("", help="Scope"), json_out: bool = typer.Option(True, "--json")):
    out = evidence_trace(control, scope)
    print(json.dumps(out, ensure_ascii=False) if json_out else str(out))


@app.command("risk")
def risk_cmd(scenario_set: str = typer.Option("", help="Scenario grouping"), scope: str = typer.Option("", help="Scope"), explain: bool = typer.Option(False, "--explain", help="Include normalized explain vectors"), json_out: bool = typer.Option(True, "--json")):
    out = risk_matrix(scenario_set, scope)
    if explain:
        scenario = out["items"]["scenarios"][0]
        total_weighted = sum(v for _, v in scenario["weighted_contributions"]) or 1
        explain_vec = {k: round(v * 100.0 / total_weighted, 2) for k, v in scenario["weighted_contributions"]}
        normalized = {k: round(v / total_weighted, 6) for k, v in scenario["weighted_contributions"]}
        out["meta"]["explain"] = {"weighted_contribution_pct": explain_vec, "weighted_contribution_normalized": normalized, "components_pct": scenario["components_pct"]}
    print(json.dumps(out, ensure_ascii=False) if json_out else str(out))
