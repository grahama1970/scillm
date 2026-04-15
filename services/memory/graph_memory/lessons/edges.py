from __future__ import annotations
import time
import hashlib
import json
import typer
from ..arango_client import get_db
from loguru import logger

# Separate Typer apps for console scripts mapping
approve_app = typer.Typer(add_completion=False)
prune_app = typer.Typer(add_completion=False)
reject_app = typer.Typer(add_completion=False)
delete_app = typer.Typer(add_completion=False)

@approve_app.command("approve")
def approve(edge_id: str = typer.Option("", help="Edge _id to approve (or use from/to)"), from_title: str = typer.Option(""), from_scope: str = typer.Option("tabbed"), to_title: str = typer.Option(""), to_scope: str = typer.Option("tabbed"), human_rationale: str = typer.Option("Approved"), json_out: bool = typer.Option(False, '--json'), dry_run: bool = typer.Option(False, '--dry-run', help='Plan only; no write')):
    db = get_db()
    ts = int(time.time())
    if not edge_id:
        f = list(db.aql.execute('FOR d IN lessons_v2 FILTER d.title==@t AND d.scope==@s LIMIT 1 RETURN d._id', bind_vars={'t':from_title,'s':from_scope}))
        t = list(db.aql.execute('FOR d IN lessons_v2 FILTER d.title==@t AND d.scope==@s LIMIT 1 RETURN d._id', bind_vars={'t':to_title,'s':to_scope}))
        if not f or not t:
            print('edge_id or from/to titles required')
            raise typer.Exit(2)
        arr = list(db.aql.execute('FOR e IN lesson_edges FILTER e._from==@f AND e._to==@t AND e.type=="related" LIMIT 1 RETURN e._id', bind_vars={'f':f[0],'t':t[0]}))
        if not arr:
            print('edge not found')
            raise typer.Exit(3)
        edge_id = arr[0]
    # Archive previous version (best-effort)
    try:
        old = list(db.aql.execute('RETURN DOCUMENT(@eid)', bind_vars={'eid': edge_id}))[0]
        if old:
            payload = json.dumps(old, sort_keys=True, ensure_ascii=False)
            commit_id = hashlib.sha1(payload.encode('utf-8')).hexdigest()
            db.collection('edge_revisions').insert({
                'edge_id': old.get('_id'),
                'pair_id': old.get('pair_id'),
                'rev': int((old.get('rev') or 0)) + 1,
                'snapshot': old,
                'commit_id': commit_id,
                'at': ts,
                'by': 'human_approve',
            })
    except Exception as exc:
        logger.error("approve caught error: {exc}", exc=exc)
    if dry_run:
        out_id = edge_id
    else:
        aql = 'LET e = DOCUMENT(@eid) UPDATE e WITH { approved: true, status: "active", rationale: @hr, rationales: APPEND(e.rationales ? e.rationales : [], { by: "human", text: @hr, at: @ts }), last_verified_at: @ts, updated_at: @ts } IN lesson_edges RETURN NEW._id'
        for _attempt in range(3):
            try:
                out = list(db.aql.execute(aql, bind_vars={'eid':edge_id,'hr':human_rationale,'ts':ts}))
                break
            except Exception as exc:
                err = str(exc)
                if ('write-write conflict' in err or 'timeout waiting to lock' in err) and _attempt < 2:
                    time.sleep(0.5 * (_attempt + 1))
                    continue
                raise
        out_id = out[0]
    if json_out:
        print(json.dumps({'meta': {'ok': True, 'dry_run': bool(dry_run)}, 'items': [{'_id': out_id}], 'errors': []}))
    else:
        print('approved (dry-run):' if dry_run else 'approved:', out_id)

@prune_app.command("prune")
def prune():
    db = get_db()
    aql = '''
    FOR e IN lesson_edges
      FILTER e.type=='related' AND e.approved==false AND e.status=='pending'
        AND e.weight < 0.30
        AND (DATE_NOW()/1000 - e.created_at) > 365*86400
        AND (e.usage_count == null OR e.usage_count == 0)
      REMOVE e IN lesson_edges RETURN OLD._id
    '''
    removed = list(db.aql.execute(aql))
    print('pruned:', removed)


def _pair_id(a_id: str, b_id: str) -> str:
    a, b = (a_id, b_id) if a_id <= b_id else (b_id, a_id)
    return hashlib.sha1((a + '|' + b).encode('utf-8')).hexdigest()


@reject_app.command("reject")
def reject(
    edge_id: str = typer.Option("", help="Edge _id to reject (or use from/to)"),
    from_title: str = typer.Option(""),
    from_scope: str = typer.Option("tabbed"),
    to_title: str = typer.Option(""),
    to_scope: str = typer.Option("tabbed"),
    human_rationale: str = typer.Option("Rejected"),
):
    """Mark a pair as rejected to prevent future proposals.

    If edge_id is not provided, from/to titles and scopes are used to compute the pair id.
    """
    db = get_db()
    ts = int(time.time())
    pid = None
    if edge_id:
        try:
            e = db.collection("lesson_edges").get(edge_id.split('/')[-1])
            if e:
                pid = e.get("pair_id")
        except Exception as exc:
            logger.error("reject get failed: {exc}", exc=exc)
            pid = None
    if not pid:
        f = list(db.aql.execute('FOR d IN lessons_v2 FILTER d.title==@t AND d.scope==@s LIMIT 1 RETURN d._id', bind_vars={'t':from_title,'s':from_scope}))
        t = list(db.aql.execute('FOR d IN lessons_v2 FILTER d.title==@t AND d.scope==@s LIMIT 1 RETURN d._id', bind_vars={'t':to_title,'s':to_scope}))
        if not f or not t:
            print('edge_id or from/to titles required')
            raise typer.Exit(2)
        pid = _pair_id(f[0], t[0])
    doc = {
        "_key": pid,
        "pair_id": pid,
        "reason": "manual_reject",
        "details": human_rationale,
        "last_checked_at": ts,
        "attempts": 1,
    }
    try:
        db.collection("rejected_pairs").insert(doc)
        print('rejected:', pid)
    except Exception as exc:
        logger.error("reject int parse failed: {exc}", exc=exc)
        try:
            db.collection("rejected_pairs").update(doc)
            print('rejected (updated):', pid)
        except Exception as exc:
            logger.error("reject int parse failed: {exc}", exc=exc)
            print('reject failed for:', pid)


@delete_app.command("delete")
def delete_edge(
    edge_id: str = typer.Option("", help="Edge _id or _key to delete (or use from/to)"),
    from_title: str = typer.Option("", help="Source lesson title (fallback lookup)"),
    from_scope: str = typer.Option("tabbed", help="Source scope"),
    to_title: str = typer.Option("", help="Target lesson title (fallback lookup)"),
    to_scope: str = typer.Option("tabbed", help="Target scope"),
    json_out: bool = typer.Option(False, "--json", help="Print JSON envelope"),
):
    """Hard delete a lesson edge.

    Uses the same from/to lookup pattern as approve when edge_id is omitted.
    """
    db = get_db()
    if not edge_id:
        f = list(db.aql.execute('FOR d IN lessons_v2 FILTER d.title==@t AND d.scope==@s LIMIT 1 RETURN d._id', bind_vars={'t': from_title, 's': from_scope}))
        t = list(db.aql.execute('FOR d IN lessons_v2 FILTER d.title==@t AND d.scope==@s LIMIT 1 RETURN d._id', bind_vars={'t': to_title, 's': to_scope}))
        if not f or not t:
            print('edge_id or from/to titles required')
            raise typer.Exit(2)
        arr = list(db.aql.execute('FOR e IN lesson_edges FILTER e._from==@f AND e._to==@t LIMIT 1 RETURN e._id', bind_vars={'f': f[0], 't': t[0]}))
        if not arr:
            print('edge not found')
            raise typer.Exit(3)
        edge_id = arr[0]

    # Normalize to _key
    key = edge_id.split('/')[-1]
    try:
        db.aql.execute('REMOVE @k IN lesson_edges', bind_vars={'k': key})
        if json_out:
            print(json.dumps({'meta': {'ok': True}, 'items': [{'_id': f'lesson_edges/{key}'}], 'errors': []}))
        else:
            print('deleted:', f'lesson_edges/{key}')
    except Exception as exc:  # pragma: no cover
        if json_out:
            print(json.dumps({'meta': {'ok': False}, 'items': [], 'errors': [str(exc)]}))
        else:
            print('delete failed:', exc)
