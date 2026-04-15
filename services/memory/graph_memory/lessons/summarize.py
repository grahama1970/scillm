from __future__ import annotations
import time
import json
import typer
from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view

app = typer.Typer(add_completion=False)


@app.command("clusters")
def clusters(scope: str = typer.Option("", help="Optional scope"), min_size: int = typer.Option(3), dry_run: bool = typer.Option(False), json_out: bool = typer.Option(False, "--json")):
    ensure_collections_and_view()
    db = get_db()
    # For each cluster_id in lessons with size >= min_size, create/update a summary lesson
    aql = """
    FOR c IN (
      FOR d IN lessons_v2
        FILTER (d.cluster_id != null) AND (@scope=='' OR d.scope==@scope)
        COLLECT cid = d.cluster_id, sc = d.scope WITH COUNT INTO ct
        FILTER ct >= @min
        RETURN { cluster_id: cid, scope: sc, size: ct }
    ) RETURN c
    """
    rows = list(db.aql.execute(aql, bind_vars={'scope': scope or '', 'min': int(min_size)}))
    ts = int(time.time())
    wrote = 0
    for r in rows:
        cid, sc, size = r['cluster_id'], r['scope'], int(r['size'])
        # Aggregate cluster tags and titles
        agg = list(db.aql.execute(
            """
            LET titles = (FOR d IN lessons_v2 FILTER d.cluster_id==@cid LIMIT 5 RETURN d.title)
            LET tags = UNIQUE(FLATTEN(FOR d IN lessons_v2 FILTER d.cluster_id==@cid RETURN d.tags))
            RETURN { titles: titles, tags: tags }
            """,
            bind_vars={'cid': cid},
        ))
        titles = (agg[0].get('titles') if agg else [])
        tags = agg[0].get('tags') if agg else []
        summ_title = f"PLAYBOOK[{cid[:8]}] {sc or ''} size={size}"
        playbook = '- ' + '\n- '.join(titles)
        doc = {
            'title': summ_title,
            'problem': f"Summary of cluster {cid} ({size} lessons)",
            'playbook': playbook,
            'tags': tags,
            'scope': sc,
            'status': 'active',
            'is_summary': True,
            'cluster_id': cid,
            'updated_at': ts,
        }
        if dry_run:
            if json_out:
                print(json.dumps({'meta': {'dry_run': True}, 'items': [{'title': summ_title, 'cluster_id': cid, 'size': size}], 'errors': []}))
            else:
                typer.echo(f"would write summary: {summ_title}")
            continue
        from .store import store_lesson
        store_lesson(db, doc)
        wrote += 1
    if not dry_run:
        if json_out:
            print(json.dumps({'meta': {'ok': True}, 'items': [{'written': wrote}], 'errors': []}))
        else:
            typer.echo(f"Summaries written: {wrote}")
