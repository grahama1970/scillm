from __future__ import annotations
import json, os, re, time
from typing import Any, Dict, List, Tuple, Set
import typer

from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view
from loguru import logger

app = typer.Typer(add_completion=False)


def _toks(s: str) -> List[str]:
    return [t.lower() for t in re.findall(r"[A-Za-z0-9_\-]+", s or "")]


def _norm_title(t: str) -> str:
    t = re.sub(r"^DEMO\[[^\]]+\]\s*", "", t or "", flags=re.I)
    t = re.sub(r"[#:]+\s*", " ", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / max(1, len(a | b))


@app.command("relations")
def extract_relations(
    scope: str = typer.Option("", help="Optional scope filter"),
    types: str = typer.Option(os.getenv("EXTRACT_REL_TYPES", "solves,duplicates,similar_to,related"), help="CSV of relation types"),
    min_conf: float = typer.Option(float(os.getenv("EXTRACT_REL_MIN_CONF", "0.70") or 0.70), help="Minimum confidence 0..1"),
    limit: int = typer.Option(1000, help="Max candidates to emit"),
    stage: bool = typer.Option(False, help="Write to relation_candidates staging collection"),
    json_out: bool = typer.Option(True, "--json", help="Output JSON envelope"),
):
    """Guided, read‑only relation extraction (opt‑in).

    Heuristics only (no LLM):
      - duplicates: normalized title equality
      - similar_to: high Jaccard (title+tags tokens)
      - solves: directional if source contains fix/how to/solves and token overlap
      - related: fallback for strong token overlap
    """
    ensure_collections_and_view()
    db = get_db()
    rows = list(db.collection("lessons"))
    if scope:
        rows = [d for d in rows if (d.get("scope") or "") == scope]
    if not rows:
        if json_out:
            print(json.dumps({"meta": {"scope": scope}, "items": [], "errors": ["no lessons"]}))
        return

    wanted = {t.strip().lower() for t in (types or '').split(',') if t.strip()}
    # Precompute fields
    docs: List[Dict[str, Any]] = []
    for d in rows:
        title = str(d.get('title') or '')
        toks = set(_toks(title) + [t.lower() for t in (d.get('tags') or [])])
        docs.append({
            '_id': f"lessons/{d['_key']}",
            '_key': str(d['_key']),
            'title': title,
            'norm': _norm_title(title),
            'toks': toks,
            'scope': d.get('scope') or ''
        })

    items: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str, str]] = set()
    def push(frm: str, to: str, ty: str, conf: float, why: str):
        nonlocal items
        if conf < float(min_conf):
            return
        key = (frm, to, ty)
        if key in seen:
            return
        seen.add(key)
        items.append({
            'from': frm, 'to': to, 'type': ty, 'confidence': round(float(conf), 3), 'rationale': why
        })

    # Index by normalized title for duplicates
    if 'duplicates' in wanted:
        by_norm: Dict[str, List[Dict[str, Any]]] = {}
        for d in docs:
            by_norm.setdefault(d['norm'], []).append(d)
        for norm, arr in by_norm.items():
            if not norm or len(arr) < 2:
                continue
            for i in range(len(arr)):
                for j in range(i + 1, len(arr)):
                    a, b = arr[i], arr[j]
                    conf = 0.95
                    push(a['_id'], b['_id'], 'duplicates', conf, 'normalized-title-match')
                    push(b['_id'], a['_id'], 'duplicates', conf, 'normalized-title-match')

    # Pairwise token overlap for similar_to / related / solves
    want_sim = 'similar_to' in wanted
    want_rel = 'related' in wanted
    want_solves = 'solves' in wanted
    solver_terms = {'fix', 'solves', 'solution', 'repair', 'resolve', 'how', 'troubleshoot'}
    for i in range(len(docs)):
        a = docs[i]
        a_set = a['toks']
        a_solver = any(t in a_set for t in solver_terms)
        for j in range(i + 1, len(docs)):
            b = docs[j]
            b_set = b['toks']
            jac = _jaccard(a_set, b_set)
            # similar_to at high Jaccard
            if want_sim and jac >= 0.55:
                push(a['_id'], b['_id'], 'similar_to', jac, f'jaccard={jac:.2f}')
                push(b['_id'], a['_id'], 'similar_to', jac, f'jaccard={jac:.2f}')
            # solves (directional) when a looks like a solution to b (token overlap)
            if want_solves and a_solver and jac >= 0.30:
                conf = min(0.9, 0.5 + jac)
                push(a['_id'], b['_id'], 'solves', conf, f'solver_terms & overlap={jac:.2f}')
            if want_solves and (any(t in b_set for t in solver_terms)) and jac >= 0.30:
                conf = min(0.9, 0.5 + jac)
                push(b['_id'], a['_id'], 'solves', conf, f'solver_terms & overlap={jac:.2f}')
            # related fallback (strong but not duplicate)
            if want_rel and jac >= 0.40:
                push(a['_id'], b['_id'], 'related', jac * 0.85, f'overlap={jac:.2f}')
                push(b['_id'], a['_id'], 'related', jac * 0.85, f'overlap={jac:.2f}')

            if len(items) >= int(limit):
                break
        if len(items) >= int(limit):
            break

    # Enforce limit hard (safety)
    try:
        lim = int(limit)
    except Exception as exc:
        logger.error("push int parse failed: {exc}", exc=exc)
        lim = 1000
    if len(items) > lim:
        items = items[:lim]

    # Stage optionally
    if stage and items:
        try:
            col = db.collection('relation_candidates')
        except Exception as exc:
            logger.error("push collection access failed: {exc}", exc=exc)
            db.create_collection('relation_candidates')
            col = db.collection('relation_candidates')
        ts = int(time.time())
        for it in items:
            doc = {
                'from': it['from'], 'to': it['to'], 'type': it['type'], 'confidence': it['confidence'],
                'rationale': it.get('rationale') or 'auto', 'status': 'staged', 'at': ts
            }
            try:
                col.insert(doc)
            except Exception as exc:
                logger.error("push insert failed: {exc}", exc=exc)

    out = { 'meta': { 'scope': scope, 'types': sorted(list(wanted)), 'min_conf': float(min_conf), 'staged': bool(stage), 'count': len(items) }, 'items': items, 'errors': [] }
    if json_out:
        print(json.dumps(out, ensure_ascii=False))


@app.command("quality")
def quality(
    labels_file: str = typer.Option(..., help="JSON file with expected triples"),
    scope: str = typer.Option("", help="Optional scope filter"),
    use_staging: bool = typer.Option(False, help="Evaluate against relation_candidates; otherwise run extractor"),
    json_out: bool = typer.Option(True, "--json"),
):
    """Compute precision/recall/F1 for extracted relations (non‑gating).

    labels_file JSON schema:
      { "triples": [ {"from": "lessons/<key>", "to": "lessons/<key>", "type": "related|similar_to|..."}, ... ] }
    """
    ensure_collections_and_view()
    db = get_db()
    lab = json.load(open(labels_file, 'r'))
    gold = set()
    for t in lab.get('triples', []):
        a = str(t.get('from') or '').strip()
        b = str(t.get('to') or '').strip()
        ty = str(t.get('type') or '').strip().lower()
        if not (a and b and ty):
            continue
        gold.add((a, b, ty))

    pred = set()
    if use_staging:
        try:
            rows = list(db.collection('relation_candidates'))
        except Exception as exc:
            logger.error("quality collection access failed: {exc}", exc=exc)
            rows = []
        for r in rows:
            frm = str(r.get('from') or '')
            to = str(r.get('to') or '')
            ty = str(r.get('type') or '').lower()
            if frm and to and ty:
                pred.add((frm, to, ty))

    tp = len([1 for t in pred if t in gold])
    fp = max(0, len(pred) - tp)
    fn = max(0, len(gold) - tp)
    prec = (tp / (tp + fp)) if (tp + fp) else 0.0
    rec = (tp / (tp + fn)) if (tp + fn) else 0.0
    f1 = (2 * prec * rec / (prec + rec)) if (prec + rec) else 0.0
    out = { 'meta': { 'scope': scope, 'use_staging': bool(use_staging) }, 'items': [{ 'tp': tp, 'fp': fp, 'fn': fn, 'precision': round(prec,3), 'recall': round(rec,3), 'f1': round(f1,3) }], 'errors': [] }
    if json_out:
        print(json.dumps(out, ensure_ascii=False))


@app.command("entities")
def entities(
    scope: str = typer.Option("code", help="Target scope for symbol snapshots/lessons"),
    code_path: str = typer.Option(..., help="Repository root to analyze"),
    use_lsp: bool = typer.Option(False, "--use-lsp", help="Use multilspy-backed LSP enrichment if available"),
    use_serena: bool = typer.Option(False, "--use-serena", help="EXPERIMENTAL: plan to source symbols via a live Serena MCP session"),
    persist: bool = typer.Option(True, help="Persist symbol snapshots and synthetic lessons"),
    json_out: bool = typer.Option(True, "--json", help="Emit JSON envelope"),
):
    """Collect code symbols and optionally enrich via LSP.

    - Default: Tree‑Sitter symbol snapshots (no LSP).
    - --use-lsp: Attempt multilspy LSP enrichment (falls back to Tree‑Sitter).
    - --use-serena: Draft adapter. Emits a dry‑run plan and then degrades to --use-lsp if set.
    """
    ensure_collections_and_view()

    # Optional: show Serena plan (dry‑run) for operator visibility.
    serena_plan = None
    if use_serena:
        serena_cmd = os.getenv("SERENA_STDIO_CMD", "")
        serena_env = {k: os.getenv(k) for k in ("SERENA_PROJECT", "SERENA_CONFIG") if os.getenv(k)}
        serena_plan = {
            "adapter": "serena-mcp",
            "stdio_cmd": serena_cmd.split() if serena_cmd else [],
            "env": serena_env,
            "status": "draft",
            "note": "In this draft adapter, we fall back to local LSP/Tree‑Sitter."
        }

    from . import symbols as _symbols
    from . import lsp_worker as _lsp

    repo = os.path.expanduser(code_path)
    items: list[dict[str, object]] = []
    errors: list[str] = []
    try:
        if use_lsp:
            _lsp.enrich_symbols(scope=scope, repo_root=repo, languages=None, debounce_seconds=0.0, persist=persist)
            # Count snapshots enriched for quick feedback
            from ..arango_client import get_db
            db = get_db()
            count = int(list(db.aql.execute("RETURN LENGTH(FOR s IN symbol_snapshots FILTER s.scope==@s RETURN 1)", bind_vars={'s': scope}))[0])
            items.append({"scope": scope, "snapshots": count, "provider": "lsp-or-fallback"})
        else:
            from pathlib import Path as _P
            recs = _symbols.collect_symbols(scope=scope, repo_path=_P(repo), file_paths=None, persist=persist)
            items.append({"scope": scope, "symbols": len(recs), "provider": "tree-sitter"})
    except Exception as exc:
        errors.append(str(exc))

    out = {"meta": {"scope": scope, "repo": repo, "serena_plan": serena_plan}, "items": items, "errors": errors}
    if json_out:
        import json as _json
        print(_json.dumps(out, ensure_ascii=False))
