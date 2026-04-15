from __future__ import annotations
import time
import hashlib
import typer
from typing import Set
from ..arango_client import get_db
from ..events import log_event
from ..setup_schema import ensure_collections_and_view
import json
import os
from loguru import logger

# Exposed helpers (module-level) so tests can monkeypatch
def _call_ollama(prompt: str, mdl: str) -> dict:
    """Direct call to local Ollama (non-Chutes), allowed for provider=ollama.

    Tries structured JSON with schema when enabled; otherwise plain json + repair.
    """
    import os as _os, jsonschema, time
    from ..http_clients import get_session as _get_session
    from ..extras.json_utils import clean_json_string

    base = _os.getenv('OLLAMA_BASE_URL') or _os.getenv('OLLAMA_API_BASE') or 'http://127.0.0.1:11434'
    to = int(_os.getenv('OLLAMA_TIMEOUT_MS', '20000') or '20000')/1000.0
    structured_enabled = (_os.getenv('OLLAMA_STRUCTURED_ENABLED','1').lower() in {'1','true','yes'})
    require_schema = (_os.getenv('OLLAMA_REQUIRE_SCHEMA','0').lower() in {'1','true','yes'})

    schema = {
        "type": "object",
        "properties": {
            "keep": {"type": "boolean"},
            "weight": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "type": {"type": "string"},
            "rationale": {"type": "string"}
        },
        "required": ["keep", "weight", "confidence", "type", "rationale"],
        "additionalProperties": False
    }

    formats = []
    if structured_enabled:
        formats.append({'type': 'json', 'schema': schema})
    formats.append('json')

    last_err = None
    for fmt in formats:
        try:
            r = _get_session().post(
                base.rstrip('/') + '/api/generate',
                json={
                    'model': mdl,
                    'prompt': prompt,
                    'stream': False,
                    'format': fmt,
                    'options': { 'temperature': 0.1 },
                },
                timeout=to,
            )
            r.raise_for_status()
            js = r.json(); txt = js.get('response') or ''
            cleaned = clean_json_string(txt, return_dict=True)
            if isinstance(cleaned, dict):
                if fmt != 'json':
                    try:
                        jsonschema.validate(cleaned, schema)
                    except Exception as _e:
                        last_err = _e
                        if require_schema:
                            return { 'error': f'schema_validation_failed: {_e}', 'sample': cleaned }
                        continue
                return cleaned
        except Exception as e:
            last_err = e
            time.sleep(0.3)
            continue
    return { 'error': str(last_err) if last_err else 'no-json' }


async def _run_codex_exec(prompt: str, mdl: str, timeout_ms: int) -> dict:
    import asyncio, os as _os, json as _json
    bin_ = _os.getenv('CODEX_EXEC_BIN', 'codex')
    env_id = _os.getenv('CODEX_ENV_ID', '')
    if not env_id:
        return {'error': 'missing CODEX_ENV_ID'}
    cmd = [bin_, 'exec', '-e', env_id, '-m', mdl or _os.getenv('CODEX_MODEL','gpt-5'), '-c', '--no-search', '-']
    try:
        p = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(p.communicate(input=prompt.encode('utf-8')), timeout=timeout_ms/1000.0)
        if p.returncode != 0:
            return {'error': f'codex exit {p.returncode}', 'stderr': (err or b'').decode('utf-8')[:400]}
        txt = (out or b'').decode('utf-8').strip()
        start = txt.find('{'); end = txt.rfind('}')
        if start != -1 and end != -1 and end > start:
            return _json.loads(txt[start:end+1])
        return {'error': 'no-json'}
    except asyncio.TimeoutError:
        return {'error': 'timeout'}
    except Exception as e:
        return { 'error': str(e) }


app = typer.Typer(add_completion=False)

ALLOWED_TYPES: Set[str] = {
    'solves', 'mitigates', 'duplicates', 'uses_tool', 'caused_by', 'verifies', 'depends_on', 'similar_to', 'related', 'violates',
    # Sanity script edge types
    'uses_script',      # lesson -> sanity_script (lesson references a working example)
    'supersedes',       # sanity_script -> sanity_script (newer replaces older)
    'contradicted_by',  # sanity_script -> lesson (script found to be wrong, lesson explains why)
    # Theory of Mind (ToM) edge types for persona agents
    'observes',         # Agent observed this about user (user observation edges)
    'revises',          # New observation revises old one (softer than contradicts)
    'trusts',           # Directional trust (user->agent or agent->user)
    'respects',         # Directional respect
    'distrusts',        # Explicit distrust (contradicts trusts)
    'triggers',         # Topic/user triggers persona state change
    'satisfies',        # Interaction satisfies a drive
    'frustrates',       # Interaction frustrates a drive
    'lesson_informs_belief',  # Lesson informs/shapes a belief
}
SYMMETRIC_TYPES: Set[str] = {'duplicates', 'similar_to', 'related'}


def _pair_id(a_id: str, b_id: str) -> str:
    a, b = (a_id, b_id) if a_id <= b_id else (b_id, a_id)
    return hashlib.sha1((a + '|' + b).encode('utf-8')).hexdigest()


def _resolve_lesson_id(db, title: str, scope: str) -> str:
    rows = list(db.aql.execute(
        "FOR d IN lessons_v2 FILTER d.title==@t AND (@s=='' OR d.scope==@s) LIMIT 1 RETURN d._id",
        bind_vars={'t': title, 's': scope or ''},
    ))
    return rows[0] if rows else ''


@app.command('add')
def add(
    from_title: str = typer.Option(...),
    from_scope: str = typer.Option(''),
    to_title: str = typer.Option(...),
    to_scope: str = typer.Option(''),
    type: str = typer.Option(..., help=f"Type in {sorted(ALLOWED_TYPES)}"),
    weight: float = typer.Option(0.75),
    rationale: str = typer.Option('Authored'),
    provenance: str = typer.Option('human'),
    approved: bool = typer.Option(True),
    json_out: bool = typer.Option(False, "--json"),
    dry_run: bool = typer.Option(False, "--dry-run", help='Plan only; do not write'),
):
    ensure_collections_and_view()
    db = get_db()
    t = str(type).strip()
    if t not in ALLOWED_TYPES:
        typer.echo(f"invalid type: {t}")
        raise typer.Exit(2)
    f_id = _resolve_lesson_id(db, from_title, from_scope)
    to_id = _resolve_lesson_id(db, to_title, to_scope)
    if not f_id or not to_id:
        typer.echo('from/to lessons not found')
        raise typer.Exit(3)
    pid = _pair_id(f_id, to_id)
    ts = int(time.time())
    w = max(0.0, min(1.0, float(weight)))
    wrote = []
    for frm, to in ((f_id, to_id), (to_id, f_id)) if t in SYMMETRIC_TYPES else ((f_id, to_id),):
        if not dry_run:
            aql = (
                "UPSERT { _from: @from, _to: @to, type: @ty } "
                "INSERT { _from: @from, _to: @to, type: @ty, source: @prov, weight: @w, confidence: @w, approved: @appr, rationale: @rat, rationales: [ { by: @prov, text: @rat, at: @ts } ], status: @status, created_at: @ts, updated_at: @ts, last_verified_at: @ts, valid_from: @ts, valid_to: null, pair_id: @pid, decay_policy: 'standard' } "
                "UPDATE { source: @prov, weight: @w, confidence: @w, approved: @appr, rationale: @rat, updated_at: @ts, last_verified_at: @ts, status: @status, pair_id: @pid } IN lesson_edges"
            )
            db.aql.execute(aql, bind_vars={
                'from': frm, 'to': to, 'ty': t, 'prov': provenance, 'w': w, 'appr': bool(approved), 'rat': rationale, 'ts': ts, 'status': 'active' if approved else 'pending', 'pid': pid
            })
        wrote.append({'from': frm, 'to': to, 'type': t})
    try:
        log_event(db, 'edge_authored', f"{t} {from_title} -> {to_title}", {'from': from_title, 'to': to_title, 'type': t, 'scope': {'from': from_scope, 'to': to_scope}})
    except Exception as exc:
        logger.error("log_event edge_authored failed: {}", exc)
    if json_out:
        print(json.dumps({'meta': {'ok': True, 'dry_run': bool(dry_run)}, 'items': wrote, 'errors': []}))
    else:
        typer.echo('edge(s) planned' if dry_run else 'edge(s) written')


@app.command('validate')
def validate(scope: str = typer.Option(''), fix: bool = typer.Option(False)):
    ensure_collections_and_view()
    db = get_db()
    # Progress logging (validation may scan many edges)
    try:
        _PROGRESS_EVERY_SEC = int(os.getenv('PROGRESS_EVERY_SEC', '5') or '5')
    except Exception as exc:
        logger.error("parsing PROGRESS_EVERY_SEC env var failed: {}", exc)
        _PROGRESS_EVERY_SEC = 5
    _progress_enabled = _PROGRESS_EVERY_SEC > 0 and not bool(os.getenv('JSON_OUT',''))
    _t0 = time.time(); _last_log = _t0; _checked = 0; _fixed = 0
    def _maybe_log(force: bool = False):
        nonlocal _last_log
        if not _progress_enabled:
            return
        now = time.time()
        if force or (now - _last_log) >= _PROGRESS_EVERY_SEC:
            elapsed = now - _t0
            try:
                typer.echo(f"validate progress: checked={_checked} fixed={_fixed} elapsed={elapsed:.1f}s")
            except Exception as exc:
                logger.error("typer.echo validate progress failed: {}", exc)
            _last_log = now
    # Fetch edges with lesson scopes
    aql = (
        """
        FOR e IN lesson_edges
          LET fromKey = SPLIT(e._from,'/')[1]
          LET toKey = SPLIT(e._to,'/')[1]
          LET lf = DOCUMENT('lessons', fromKey)
          LET lt = DOCUMENT('lessons', toKey)
          FILTER (@s=='' OR (lf!=null AND lf.scope==@s) OR (lt!=null AND lt.scope==@s))
          RETURN { e: e, lf: lf, lt: lt }
        """
    )
    rows = list(db.aql.execute(aql, bind_vars={'s': scope or ''}))
    problems = 0
    for r in rows:
        e = r['e']
        ty = e.get('type') or 'related'
        if ty not in ALLOWED_TYPES:
            problems += 1
            typer.echo(f"invalid type: {ty} for {e.get('_id')}")
            continue
        # weight bounds
        w = float(e.get('weight') or 0)
        if not (0.0 <= w <= 1.0):
            problems += 1
            if fix:
                nw = max(0.0, min(1.0, w))
                db.aql.execute("LET d = DOCUMENT(@id) UPDATE d WITH { weight: @w, confidence: @w } IN lesson_edges", bind_vars={'id': e['_id'], 'w': nw})
                _fixed += 1
        # pair_id present
        pid = e.get('pair_id')
        if not pid:
            problems += 1
            if fix:
                pid = _pair_id(e['_from'], e['_to'])
                db.aql.execute("LET d = DOCUMENT(@id) UPDATE d WITH { pair_id: @p } IN lesson_edges", bind_vars={'id': e['_id'], 'p': pid})
                _fixed += 1
        # symmetry for symmetric types
        if ty in SYMMETRIC_TYPES:
            rev = list(db.aql.execute("FOR x IN lesson_edges FILTER x._from==@a AND x._to==@b AND x.type==@t LIMIT 1 RETURN x._id", bind_vars={'a': e['_to'], 'b': e['_from'], 't': ty}))
            if not rev:
                problems += 1
                if fix:
                    ts = int(time.time())
                    db.aql.execute(
                        "UPSERT { _from:@a, _to:@b, type:@t } INSERT { _from:@a, _to:@b, type:@t, weight:@w, confidence:@w, approved:true, status:'active', created_at:@ts, updated_at:@ts, last_verified_at:@ts, pair_id:@pid } UPDATE { weight:@w, confidence:@w, approved:true, status:'active', updated_at:@ts, last_verified_at:@ts, pair_id:@pid } IN lesson_edges",
                        bind_vars={'a': e['_to'], 'b': e['_from'], 't': ty, 'w': max(0.0, min(1.0, w)), 'ts': ts, 'pid': _pair_id(e['_from'], e['_to'])}
                    )
                    _fixed += 1
        _checked += 1
        _maybe_log()
    typer.echo(f"validation done, problems: {problems}{' (fixed where possible)' if fix else ''}")
    _maybe_log(force=True)


@app.command('invalidate')
def invalidate(
    edge_id: str = typer.Option('', help="Edge document id (lesson_edges/<key>)"),
    from_title: str = typer.Option('', help="Alternative: source lesson title"),
    from_scope: str = typer.Option('', help="Source scope"),
    to_title: str = typer.Option('', help="Alternative: target lesson title"),
    to_scope: str = typer.Option('', help="Target scope"),
    valid_to: int = typer.Option(..., help="Unix timestamp marking when this edge stops being valid"),
    deactivate: bool = typer.Option(True, help="Also set status='inactive'"),
    json_out: bool = typer.Option(False, "--json"),
):
    """Timestamp an edge as no-longer-valid (no hard delete)."""
    ensure_collections_and_view()
    db = get_db()
    if not edge_id:
        if not (from_title and to_title):
            typer.echo("provide edge_id or from/to titles")
            raise typer.Exit(2)
        frm = _resolve_lesson_id(db, from_title, from_scope)
        to = _resolve_lesson_id(db, to_title, to_scope)
        if not (frm and to):
            typer.echo("from/to lessons not found")
            raise typer.Exit(3)
        rows = list(db.aql.execute(
            "FOR e IN lesson_edges FILTER e._from==@f AND e._to==@t LIMIT 1 RETURN e",
            bind_vars={'f': frm, 't': to}
        ))
        if not rows:
            typer.echo("edge not found")
            raise typer.Exit(4)
        edge_id = rows[0]['_id']
    payload = { 'valid_to': int(valid_to), 'updated_at': int(time.time()) }
    if deactivate:
        payload['status'] = 'inactive'
    db.aql.execute("LET d = DOCUMENT(@id) UPDATE d WITH @p IN lesson_edges", bind_vars={'id': edge_id, 'p': payload})
    if json_out:
        print(json.dumps({'meta': {'ok': True}, 'items': [{'edge_id': edge_id, 'valid_to': int(valid_to), 'status': 'inactive' if deactivate else None}], 'errors': []}))
    else:
        typer.echo('edge invalidated')


@app.command('set-validity')
def set_validity(
    edge_id: str = typer.Option(..., help="Edge document id (lesson_edges/<key>)"),
    valid_from: int = typer.Option(None, help="Unix ts or omit to leave"),
    valid_to: int = typer.Option(None, help="Unix ts or omit to leave"),
    json_out: bool = typer.Option(False, "--json"),
):
    """Adjust valid_from/valid_to timestamps for an edge."""
    ensure_collections_and_view()
    db = get_db()
    payload = { 'updated_at': int(time.time()) }
    if valid_from is not None:
        payload['valid_from'] = int(valid_from)
    if valid_to is not None:
        payload['valid_to'] = int(valid_to)
    db.aql.execute("LET d = DOCUMENT(@id) UPDATE d WITH @p IN lesson_edges", bind_vars={'id': edge_id, 'p': payload})
    if json_out:
        print(json.dumps({'meta': {'ok': True}, 'items': [{'edge_id': edge_id, 'valid_from': payload.get('valid_from'), 'valid_to': payload.get('valid_to')}], 'errors': []}))
    else:
        typer.echo('edge validity updated')


@app.command('llm-selfcheck')
def llm_selfcheck(json_out: bool = typer.Option(True, '--json')) -> None:
    """Print a quick JSON self-check for LLM integration.

    Reports:
      - scillm proxy reachability (HTTP API at localhost:4001)
      - selected model from resolve_model() without a flag
    No LLM calls are made, just connectivity check.
    """
    import os, json as _json
    import httpx
    from ..llm.client import resolve_model  # type: ignore

    api_base = os.getenv('SCILLM_API_BASE', 'http://localhost:4001')
    api_key = os.getenv('SCILLM_PROXY_KEY', 'sk-dev-proxy-123')

    info = {
        'ok': True,
        'scillm_api_base': api_base,
        'scillm_reachable': False,
        'selected_model': None,
        'selected_model_source': None,
        'notes': []
    }

    # Check scillm proxy reachability
    try:
        resp = httpx.get(f"{api_base}/v1/scillm/health", timeout=5.0)
        info['scillm_reachable'] = resp.status_code == 200
        if resp.status_code == 200:
            info['scillm_health'] = resp.json()
    except Exception as e:
        info['notes'].append(f'scillm_unreachable: {e.__class__.__name__}: {e}')
        info['ok'] = False

    # Check model resolution
    try:
        model, source = resolve_model(None)
        info['selected_model'] = model
        info['selected_model_source'] = source
    except Exception as e:
        info['notes'].append(f'resolve_model_error: {e.__class__.__name__}: {e}')

    out = _json.dumps(info, ensure_ascii=False)
    if json_out:
        print(out)
    else:
        typer.echo(out)


# Register LLM scoring commands (llm-score, llm-score-anchor) from the split module.
# This import must come AFTER app is defined so relations_llm can import app from here.
from . import relations_llm as _relations_llm  # noqa: E402, F401
