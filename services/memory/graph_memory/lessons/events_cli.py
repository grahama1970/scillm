from __future__ import annotations
import json
import typer
from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view
from loguru import logger

app = typer.Typer(add_completion=False)


@app.command("add")
def add(
    kind: str = typer.Option(..., help="Event kind"),
    message: str = typer.Option("", help="Short message"),
    data: str = typer.Option("", help="JSON payload (stringified)"),
    json_out: bool = typer.Option(True, "--json", help="Output JSON envelope"),
):
    """Add a memory_events record quickly from CLI."""
    items = []
    errors = []
    try:
        ensure_collections_and_view()
        db = get_db()
        from ..events import log_event
        payload = {}
        if data:
            try:
                import json as _json
                payload = _json.loads(data)
            except Exception as exc:
                logger.error("add JSON parse failed: {exc}", exc=exc)
                payload = {"_raw": data}
        log_event(db, kind, message or kind, payload)
        items = [{"ok": True}]
    except Exception as e:
        errors.append(str(e))
    out = {"meta": {"kind": kind}, "items": items, "errors": errors}
    print(json.dumps(out, ensure_ascii=False)) if json_out else typer.echo(out)
    return out

@app.command("tail")
def tail(
    limit: int = typer.Option(20),
    json_out: bool = typer.Option(True, "--json", help="Output JSON envelope"),
):
    items = []
    errors = []
    try:
        ensure_collections_and_view()
        db = get_db()
        items = list(db.aql.execute("FOR e IN memory_events SORT e.at DESC LIMIT @n RETURN e", bind_vars={'n': max(1, int(limit))}))
    except Exception as e:
        errors.append(str(e))
    out = {'meta': {'limit': limit}, 'items': items, 'errors': errors}
    print(json.dumps(out, ensure_ascii=False)) if json_out else typer.echo(out)
    return out


def replay(
    limit: int = 50,
    kind: str = "",
    scope: str = "",
    thread: str = "",
    trail: bool = False,
    json_out: bool = True,
):
    """Programmatic replay (pure function). Returns a JSON-serializable dict."""
    items = []
    errors = []
    try:
        ensure_collections_and_view()
        db = get_db()
        query = """
        FOR e IN memory_events
          FILTER (@kind=='' OR e.kind==@kind)
          FILTER (@thread=='' OR e.data.thread_id==@thread OR (HAS(e.data,'thread_id') AND e.data.thread_id==@thread))
          FILTER (@scope=='' OR (HAS(e.data,'scope') AND (e.data.scope==@scope OR (IS_OBJECT(e.data.scope) AND e.data.scope.from==@scope) OR (IS_OBJECT(e.data.scope) AND e.data.scope.to==@scope))))
          SORT e.at DESC
          LIMIT @lim
          RETURN e
        """
        items = list(db.aql.execute(query, bind_vars={'kind': kind, 'scope': scope, 'thread': thread, 'lim': max(1, int(limit))}))
    except Exception as e:
        errors.append(str(e))
    if trail and items:
        # Naive stitcher: group by (thread or scope), order by time, coalesce into small trails when kinds increase
        groups = {}
        for e in items:
            key = (thread or (e.get('data') or {}).get('thread_id') or '') or (scope or (e.get('data') or {}).get('scope') or '')
            groups.setdefault(key, []).append(e)
        trails = []
        for gk, evs in groups.items():
            evs_sorted = sorted(evs, key=lambda x: int(x.get('at') or 0))
            cur = []
            for ev in evs_sorted:
                cur.append({'kind': ev.get('kind'), 'at': ev.get('at'), 'data': ev.get('data')})
                if ev.get('kind') in ('edge_authored', 'feedback'):
                    trails.append({'group': gk, 'events': cur})
                    cur = []
            if cur:
                trails.append({'group': gk, 'events': cur})
        out = {'meta': {'limit': limit, 'kind': kind, 'scope': scope, 'thread': thread, 'trail': True}, 'items': trails, 'errors': errors}
    else:
        out = {'meta': {'limit': limit, 'kind': kind, 'scope': scope, 'thread': thread}, 'items': list(reversed(items)), 'errors': errors}
    if json_out:
        print(json.dumps(out, ensure_ascii=False))
    return out


@app.command("replay")
def replay_cli(
    limit: int = typer.Option(50, help="Number of events"),
    kind: str = typer.Option("", help="Filter by event kind"),
    scope: str = typer.Option("", help="Filter by scope"),
    thread: str = typer.Option("", help="Filter by thread id if available"),
    trail: bool = typer.Option(False, "--trail", help="Return stitched search→feedback→edge trails"),
    json_out: bool = typer.Option(True, "--json", help="Output JSON envelope"),
):
    return replay(limit=limit, kind=kind, scope=scope, thread=thread, trail=trail, json_out=json_out)


@app.command("journey")
def journey_cli(
    thread: str = typer.Option("", help="Thread/session id (recommended)"),
    scope: str = typer.Option("", help="Optional scope filter"),
    limit: int = typer.Option(500, help="Max events to scan (tool_call/search)"),
    json_out: bool = typer.Option(True, "--json", help="Output JSON envelope"),
):
    """Summarize a tool-call journey for a thread or scope.

    Computes counts, unique tools, errors, and latency stats (p50/p95/mean),
    plus a per-tool breakdown.
    """
    import math
    ensure_collections_and_view()
    db = get_db()

    q = """
    FOR e IN memory_events
      FILTER e.kind IN ['tool_call','search']
      FILTER (@thread=='' OR e.data.thread_id==@thread OR (HAS(e.data,'thread_id') AND e.data.thread_id==@thread))
      FILTER (@scope=='' OR (HAS(e.data,'scope') AND (e.data.scope==@scope OR (IS_OBJECT(e.data.scope) AND e.data.scope.from==@scope) OR (IS_OBJECT(e.data.scope) AND e.data.scope.to==@scope))))
      SORT e.at DESC
      LIMIT @lim
      RETURN e
    """
    rows = list(db.aql.execute(q, bind_vars={
        'thread': thread or '', 'scope': scope or '', 'lim': max(1, int(limit))
    }))
    # Only tool_call rows contribute to latency/call stats
    calls = [r for r in rows if r.get('kind') == 'tool_call']
    lat = [int((r.get('data') or {}).get('took_ms') or 0) for r in calls if (r.get('data') or {}).get('took_ms') is not None]
    lat_sorted = sorted(lat)
    n = len(lat_sorted)
    def pct(p):
        if n == 0: return 0
        idx = max(0, min(n-1, int(math.ceil(p/100.0*n))-1))
        return lat_sorted[idx]
    p50 = pct(50)
    p95 = pct(95)
    mean = (sum(lat_sorted)/n) if n else 0.0
    errs = [r for r in calls if not bool((r.get('data') or {}).get('ok', False))]
    tools = {}
    for r in calls:
        t = (r.get('data') or {}).get('tool') or 'unknown'
        tools.setdefault(t, {'count': 0, 'errors': 0, 'lat': []})
        tools[t]['count'] += 1
        if not bool((r.get('data') or {}).get('ok', False)):
            tools[t]['errors'] += 1
        v = (r.get('data') or {}).get('took_ms')
        if isinstance(v, (int, float)):
            tools[t]['lat'].append(int(v))
    tool_breakdown = []
    for name, st in tools.items():
        ls = sorted(st['lat'])
        m = len(ls)
        tp50 = 0 if m==0 else ls[max(0, min(m-1, int(math.ceil(0.5*m))-1))]
        tp95 = 0 if m==0 else ls[max(0, min(m-1, int(math.ceil(0.95*m))-1))]
        tmean = (sum(ls)/m) if m else 0.0
        tool_breakdown.append({'tool': name, 'count': st['count'], 'errors': st['errors'], 'p50_ms': tp50, 'p95_ms': tp95, 'mean_ms': tmean})
    out = {
        'meta': {'thread': thread or '', 'scope': scope or '', 'limit': int(limit)},
        'items': [{
            'calls_total': len(calls),
            'tools_unique': len(tools),
            'errors': len(errs),
            'latency_ms': {'p50': p50, 'p95': p95, 'mean': mean},
            'per_tool': tool_breakdown,
        }],
        'errors': []
    }
    print(json.dumps(out, ensure_ascii=False)) if json_out else typer.echo(out)
    return out
