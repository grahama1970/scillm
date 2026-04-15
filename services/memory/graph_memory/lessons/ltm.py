from __future__ import annotations
import json, time
import os
from typing import Optional
import typer
from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view
from ..events import log_event
from loguru import logger

app = typer.Typer(add_completion=False)


@app.command()
def consolidate(
    scope: str = typer.Option("", help="Optional scope filter"),
    min_title_jaccard: float = typer.Option(0.85, help="Title Jaccard threshold (0..1) for grouping"),
    max_pairs: int = typer.Option(200, help="Safety cap for pairs to analyze"),
    dry_run: bool = typer.Option(True, "--dry-run/--apply-preview", help="Preview merge plan (no writes)"),
    apply: bool = typer.Option(False, "--apply", help="Apply merges (requires env LTM_RETENTION_ENABLE=1)"),
    json_out: bool = typer.Option(False, "--json"),
):
    """Consolidate near-duplicate lessons (opt-in).

    Groups lessons by high title Jaccard and proposes merges to a canonical.
    Apply path (guarded) re-points edges to the canonical and archives duplicates.
    """
    ensure_collections_and_view()
    db = get_db()
    # Fetch candidate lessons (cap for safety)
    rows = list(db.aql.execute(
        """
        FOR l IN lessons_v2
          FILTER @s=='' OR l.scope==@s
          SORT l.updated_at DESC
          LIMIT @n
          RETURN KEEP(l,['_id','_key','title','scope','updated_at'])
        """,
        bind_vars={'s': scope or '', 'n': int(max_pairs)}
    ))
    def toks(t: str) -> set[str]:
        import re
        parts = [p for p in re.split(r"[^a-z0-9]+", (t or "").lower()) if p]
        return set(parts)
    pairs = []
    # Simple O(n^2) pair scan with small n (capped)
    for i in range(len(rows)):
        for j in range(i+1, len(rows)):
            a, b = rows[i], rows[j]
            if (a.get('scope') or '') != (b.get('scope') or ''):
                continue
            ta, tb = toks(a.get('title') or ''), toks(b.get('title') or '')
            if not ta or not tb:
                continue
            jacc = len(ta & tb) / float(len(ta | tb))
            if jacc >= float(min_title_jaccard):
                pairs.append((a, b, jacc))
    # Build merge plan: keep the newer (by updated_at) as canonical
    merges = []
    for a, b, jacc in pairs:
        keep, drop = (a, b) if int(a.get('updated_at') or 0) >= int(b.get('updated_at') or 0) else (b, a)
        merges.append({
            'canonical': keep.get('_id'),
            'canonical_title': keep.get('title'),
            'duplicate': drop.get('_id'),
            'duplicate_title': drop.get('title'),
            'scope': keep.get('scope'),
            'title_jaccard': round(jacc, 3),
        })
    changed = 0
    if apply and (os.getenv('LTM_RETENTION_ENABLE') in ('1','true','TRUE')) and merges:
        now = int(time.time())
        for m in merges:
            can = m['canonical']; dup = m['duplicate']
            # Re-point edges from duplicate to canonical (both directions)
            db.aql.execute("""
              FOR e IN lesson_edges
                FILTER e._from==@dup OR e._to==@dup
                LET nf = e._from==@dup ? @can : e._from
                LET nt = e._to==@dup ? @can : e._to
                UPDATE e WITH { _from: nf, _to: nt, updated_at:@u } IN lesson_edges
            """, bind_vars={'dup': dup, 'can': can, 'u': now})
            # Archive duplicate lesson
            db.aql.execute("UPDATE @id WITH { status:'archived', archived_at:@u } IN lessons_v2", bind_vars={'id': dup, 'u': now})
            changed += 1
            try:
                log_event(db, 'ltm_consolidate', 'merged_duplicate', {'scope': m['scope'], 'from': m['duplicate_title'], 'to': m['canonical_title']})
            except Exception as exc:
                logger.error("toks log_event failed: {exc}", exc=exc)
    out = { 'meta': { 'scope': scope or '', 'min_title_jaccard': min_title_jaccard, 'max_pairs': max_pairs, 'changed': changed }, 'items': merges[:50], 'errors': [] }
    if json_out:
        print(json.dumps(out, ensure_ascii=False))
    else:
        typer.echo(f"consolidate: {len(merges)} candidates; changed={changed}")
@app.command()
def record(
    title: str = typer.Option(..., help="Lesson title"),
    scope: str = typer.Option("tabbed", help="Scope of the lesson"),
    importance: float = typer.Option(0.5, help="Importance 0..1"),
    json_out: bool = typer.Option(False, "--json"),
):
    ensure_collections_and_view()
    db = get_db()
    ts = int(time.time())
    # Update or insert minimal LTM fields on the lesson document
    doc = {
        'ltm_last_seen': ts,
        'ltm_importance': max(0.0, min(1.0, float(importance))),
    }
    res = list(db.aql.execute(
        "UPSERT { title:@t, scope:@s } INSERT { title:@t, scope:@s, status:'active', added_by:'operator', updated_at:@u } UPDATE MERGE(OLD, @d) IN lessons_v2 RETURN NEW",
        bind_vars={'t': title, 's': scope, 'u': ts, 'd': doc},
    ))
    if res:
        try:
            log_event(db, 'ltm_record', f'ltm record for {title}', {'scope': scope, 'importance': doc['ltm_importance']})
        except Exception as exc:
            logger.error("record log_event failed: {exc}", exc=exc)
    out = {'meta': {'scope': scope}, 'items': [{'title': title, 'updated': bool(res)}], 'errors': []}
    if json_out:
        print(json.dumps(out, ensure_ascii=False))


@app.command()
def delete(
    title: str = typer.Option(..., help="Lesson title"),
    scope: str = typer.Option("tabbed", help="Scope of the lesson"),
    json_out: bool = typer.Option(False, "--json"),
):
    ensure_collections_and_view()
    db = get_db()
    ts = int(time.time())
    res = list(db.aql.execute(
        "FOR l IN lessons_v2 FILTER l.title==@t AND l.scope==@s UPDATE l WITH { deleted:true, deleted_at:@u } IN lessons_v2 RETURN NEW",
        bind_vars={'t': title, 's': scope, 'u': ts},
    ))
    try:
        log_event(db, 'ltm_delete', f'ltm delete for {title}', {'scope': scope})
    except Exception as exc:
        logger.error("delete log_event failed: {exc}", exc=exc)
    out = {'meta': {'scope': scope}, 'items': [{'title': title, 'deleted': bool(res)}], 'errors': []}
    if json_out:
        print(json.dumps(out, ensure_ascii=False))


@app.command()
def metrics(
    limit: int = typer.Option(10),
    json_out: bool = typer.Option(False, "--json"),
):
    ensure_collections_and_view()
    db = get_db()
    cur = db.aql.execute(
        "FOR e IN memory_events FILTER e.kind IN ['ltm_record','ltm_delete'] SORT e.at DESC LIMIT @n RETURN {kind:e.kind, at:e.at}",
        bind_vars={'n': int(limit)},
    )
    items = list(cur)
    out = {'meta': {'limit': limit}, 'items': items, 'errors': []}
    if json_out:
        print(json.dumps(out, ensure_ascii=False))


@app.command()
def retain(
    dry_run: bool = typer.Option(False, "--dry-run"),
    min_score: float = typer.Option(0.10, help="Retention min score (0..1)"),
    half_life_days: float = typer.Option(90.0, help="Recency half-life in days"),
    scope: str = typer.Option("", help="Optional scope filter"),
    apply: bool = typer.Option(False, "--apply", help="Apply changes (requires env LTM_RETENTION_ENABLE=1)"),
    json_out: bool = typer.Option(False, "--json"),
):
    """Retention job (opt-in): compute importance×recency score and archive low-score lessons.

    Score = (ltm_importance or 0.5) * (0.5 ** (age_days / half_life_days)).
    Lessons with score < min_score will be marked status='archived' (no hard deletes here).
    """
    ensure_collections_and_view()
    db = get_db()
    now = int(time.time())
    # Pull a limited set to avoid surprise magnitude; operators can re-run.
    rows = list(db.aql.execute(
        """
        FOR l IN lessons_v2
          FILTER @scope=='' OR l.scope==@scope
          RETURN KEEP(l, ['_id','_key','title','scope','updated_at','ltm_importance'])
        """,
        bind_vars={'scope': scope or ''}
    ))
    # Optional per-scope half-life overrides via JSON mapping env
    import json as _json
    scope_hl = {}
    try:
        scope_hl = _json.loads(os.getenv('LTM_SCOPE_HALF_LIVES', '') or 'null') or {}
    except Exception as exc:
        logger.error("retain JSON parse failed: {exc}", exc=exc)
        scope_hl = {}
    actions = []
    for l in rows:
        imp = float(l.get('ltm_importance') or 0.5)
        upd = int(l.get('updated_at') or 0)
        age_days = (now - upd) / 86400.0 if upd else 365.0
        hl = float(scope_hl.get(l.get('scope') or '', half_life_days))
        score = imp * (0.5 ** (age_days / max(1.0, hl)))
        if score < float(min_score):
            actions.append({'id': l.get('_id'), 'title': l.get('title'), 'scope': l.get('scope'), 'score': score})
    changed = 0
    if apply and os.getenv('LTM_RETENTION_ENABLE') in ('1','true','TRUE') and actions:
        for a in actions:
            db.aql.execute("UPDATE @id WITH { status:'archived', archived_at:@u } IN lessons_v2", bind_vars={'id': a['id'], 'u': now})
            changed += 1
    out = {
        'meta': {
            'dry_run': dry_run,
            'min_score': min_score,
            'half_life_days': half_life_days,
            'scope': scope or '',
            'changed': changed,
        },
        'items': actions[:50],
        'errors': []
    }
    if json_out:
        print(json.dumps(out, ensure_ascii=False))
