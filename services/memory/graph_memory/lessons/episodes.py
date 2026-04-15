from __future__ import annotations
import json
import time
import uuid
import typer
from typing import Optional

from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view
from ..graphops.episodes_store import get_episode_steps as _get_episode_steps
from loguru import logger

app = typer.Typer(add_completion=False)


@app.command('add')
def add(
    text: str = typer.Option('', help='Freeform text payload'),
    data: str = typer.Option('', help='JSON payload (stringified)'),
    event_time: Optional[int] = typer.Option(None, help='Unix ts when it occurred'),
    source: str = typer.Option('manual', help='Source tag'),
    scope: str = typer.Option('', help='Optional scope'),
    thread_id: str = typer.Option('', help='Thread/group identifier'),
    proof_status: str = typer.Option('', help='Proof status (pending, proved, failed, not_provable)'),
    proof_assessment: str = typer.Option('', help='JSON proof assessment result'),
    proof_id: str = typer.Option('', help='Reference to proofs collection if proved'),
    json_out: bool = typer.Option(False, '--json'),
    dry_run: bool = typer.Option(False, '--dry-run', help='Plan only; do not write'),
):
    """Add a minimal episode record (first-class ingestion unit)."""
    ensure_collections_and_view()
    db = get_db()
    try:
        col = db.collection('episodes')
    except Exception as exc:
        logger.error("add collection access failed: {exc}", exc=exc)
        db.create_collection('episodes')
        col = db.collection('episodes')
    ts = int(time.time())
    payload = {
        '_key': uuid.uuid4().hex[:16],
        'text': text or None,
        'data': None,
        'event_time': int(event_time) if event_time is not None else ts,
        'ingest_time': ts,
        'source': source or 'manual',
        'scope': scope or '',
        'thread_id': thread_id or '',
        'proof_status': proof_status or None,
        'proof_assessment': None,
        'proof_id': proof_id or None,
    }
    if data:
        try:
            payload['data'] = json.loads(data)
        except Exception as exc:
            logger.error("add JSON parse failed: {exc}", exc=exc)
            payload['data'] = {'_raw': data}
    if proof_assessment:
        try:
            payload['proof_assessment'] = json.loads(proof_assessment)
        except Exception as exc:
            logger.error("add JSON parse failed: {exc}", exc=exc)
            payload['proof_assessment'] = {'_raw': proof_assessment}
    if not dry_run:
        col.insert(payload)
    if json_out:
        print(json.dumps({'meta': {'ok': True, 'dry_run': bool(dry_run)}, 'items': [ {'_key': payload['_key'], 'scope': payload['scope'], 'proof_status': payload['proof_status']} ], 'errors': []}))
    else:
        typer.echo('episode planned' if dry_run else 'episode added')


@app.command('ls')
def ls(
    scope: str = typer.Option('', help='Optional scope filter'),
    limit: int = typer.Option(5, help='Max rows'),
    json_out: bool = typer.Option(False, '--json'),
):
    ensure_collections_and_view()
    db = get_db()
    try:
        rows = list(db.aql.execute(
            'FOR e IN episodes FILTER @s=="" OR e.scope==@s SORT e.ingest_time DESC LIMIT @n RETURN KEEP(e,["_key","scope","source","event_time","ingest_time","thread_id"])',
            bind_vars={'s': scope or '', 'n': int(limit)}
        ))
    except Exception as exc:
        logger.error("ls caught error: {exc}", exc=exc)
        rows = []
    if json_out:
        print(json.dumps({'meta': {'scope': scope or '', 'limit': int(limit)}, 'items': rows, 'errors': []}))
    else:
        for r in rows:
            typer.echo(f"{r.get('_key')} {r.get('scope')} {r.get('source')} {r.get('event_time')}")


@app.command('show-steps')
def show_steps(
    episode_id: str = typer.Option(..., help='episodes/<key>'),
    json_out: bool = typer.Option(True, '--json'),
):
    """Show steps for an episode_id (if episode_steps are present)."""
    ensure_collections_and_view()
    try:
        steps = _get_episode_steps(episode_id)
        out = {'meta': {'episode_id': episode_id}, 'items': steps, 'errors': []}
    except Exception as e:
        out = {'meta': {'episode_id': episode_id}, 'items': [], 'errors': [str(e)]}
    print(json.dumps(out, indent=2 if json_out else None))
