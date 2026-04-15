from __future__ import annotations
import time
import json
import typer
from typing import Dict, Any

from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view
from loguru import logger

app = typer.Typer(add_completion=False)


def _build_for_scope(db, scope: str, limit: int) -> Dict[str, Any]:
    # Select lessons in scope; newest first; limit if provided
    rows = list(db.aql.execute(
        """
        FOR l IN lessons_v2
          FILTER @s=='' OR l.scope==@s
          SORT l.updated_at DESC
          LIMIT @n
          RETURN l
        """,
        bind_vars={'s': scope or '', 'n': max(1, int(limit))}
    ))
    updated = 0
    now = int(time.time())
    for l in rows:
        lid = l.get('_id')
        # Episodes linked by promoted_lesson_id
        eps = list(db.aql.execute(
            "FOR e IN episodes FILTER e.promoted_lesson_id==@lid RETURN e.created_at",
            bind_vars={'lid': lid}
        ))
        if not eps:
            continue
        cmin = min(int(x or 0) for x in eps)
        cmax = max(int(x or 0) for x in eps)
        cnt = len(eps)
        # Update lesson with a brief timeline summary and set updated_at to last event
        try:
            db.collection('lessons').update({
                '_key': l.get('_key'),
                'timeline_summary': f'{cnt} episodes; last at {cmax}',
                'time_span': {'from': cmin, 'to': cmax, 'count': cnt},
                'updated_at': cmax if cmax else (l.get('updated_at') or now),
            })
            updated += 1
        except Exception as exc:
            logger.error("_build_for_scope caught error: {exc}", exc=exc)
    return {'updated': updated}


@app.command("build")
def build(
    scope: str = typer.Option("", help="Optional scope"),
    limit: int = typer.Option(500, help="Max lessons to scan"),
    json_out: bool = typer.Option(True, "--json", help="Output JSON envelope"),
):
    ensure_collections_and_view()
    db = get_db()
    out = _build_for_scope(db, scope, limit)
    print(json.dumps({'meta': {'scope': scope}, 'items': [out], 'errors': []}))


@app.command("thread")
def thread(
    thread_id: str = typer.Option(..., help="Thread identifier"),
    scope: str = typer.Option("", help="Optional scope filter"),
    json_out: bool = typer.Option(True, "--json", help="Output JSON envelope"),
):
    ensure_collections_and_view()
    db = get_db()
    query = """
    FOR e IN episodes
      FILTER e.thread_id==@thread
      FILTER @scope=='' OR e.scope==@scope
      SORT e.created_at ASC
      LET lesson = e.promoted_lesson_id ? DOCUMENT(e.promoted_lesson_id) : null
      RETURN {
        episode: KEEP(e, ['_key','status','scope','title','details','created_at','promoted_lesson_id']),
        lesson: lesson ? KEEP(lesson, ['_key','title','scope','tags','updated_at']) : null
      }
    """
    rows = list(db.aql.execute(query, bind_vars={'thread': thread_id, 'scope': scope or ''}))
    times = [int(r['episode'].get('created_at') or 0) for r in rows if r.get('episode')]
    meta = {
        'thread_id': thread_id,
        'scope': scope,
        'count': len(rows),
        'from': min(times) if times else None,
        'to': max(times) if times else None,
    }
    out = {'meta': meta, 'items': rows, 'errors': []}
    print(json.dumps(out, ensure_ascii=False))
    return out
