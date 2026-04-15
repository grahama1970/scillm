"""LLM scoring commands for lesson edge relations.

Split from relations.py — contains llm-score and llm-score-anchor commands.
"""
from __future__ import annotations
import time
import json
import os
import sys
from loguru import logger

from .relations import app, _pair_id, _call_ollama, _run_codex_exec, ALLOWED_TYPES, SYMMETRIC_TYPES
from ..arango_client import get_db
from ..events import log_event
from ..setup_schema import ensure_collections_and_view
import typer


@app.command('llm-score')
def llm_score(
    scope: str = typer.Option('', help='Scope filter (optional)'),
    top_n: int = typer.Option(200, help='Max edges to score'),
    top_weight_k: int = typer.Option(0, help='If >0, score only the top K edges by weight'),
    min_input_weight: float = typer.Option(0.0, help='Minimum existing weight to consider (pending/weak active)'),
    provider: str = typer.Option('ollama', help='ollama|openai|codex (ollama default)'),
    model: str = typer.Option('', help='Primary model (defaults to qwen3:14b; qwen3:8b with --fast)'),
    fast: bool = typer.Option(False, '--fast', help='Use the faster local primary (qwen3:8b)'),
    profile: str = typer.Option('', '--profile', help='Override scoring profile (fast|accurate)'),
    fallback_model: str = typer.Option('', help='Fallback model (defaults granite3.3:8b)'),
    code_model: str = typer.Option('', help='Code-centric model (defaults glm4)'),
    alpha: float = typer.Option(0.8, help='Blend factor for FAISS weight vs LLM suggested'),
    delta: float = typer.Option(0.05, help='Max absolute weight change from FAISS weight'),
    conf_min: float = typer.Option(0.55, help='Min LLM confidence to accept suggestion'),
    type_conf_min: float = typer.Option(0.70, help='Min confidence to change type'),
    reject_mode: str = typer.Option('demote', help='delete|demote|penalize'),
    penalty: float = typer.Option(0.10, help='Penalty strength when reject_mode=penalize (0..1)'),
    include_active_below: float = typer.Option(0.0, help='Also re-score active edges with weight below threshold (0=off)'),
    gray_low: float = typer.Option(0.45, help='Lower bound of FAISS gray band'),
    gray_high: float = typer.Option(0.60, help='Upper bound of FAISS gray band'),
    jaccard_min: float = typer.Option(0.20, help='Minimum lexical overlap threshold'),
    delta_max: float = typer.Option(0.25, help='Max allowed model weight disagreement before escalation'),
    audit_rate: float = typer.Option(0.02, help='Random audit fraction (0..1)'),
    codex_escalate: bool = typer.Option(True, '--codex-escalate/--no-codex-escalate', help='Enable GPT-5 escalation via codex exec'),
    codex_timeout_ms: int = typer.Option(8000, help='Per-edge codex exec timeout (ms)'),
    codex_max: int = typer.Option(20, help='Max GPT-5 escalations per run'),
    codex_concurrency: int = typer.Option(2, help='Max concurrent GPT-5 escalations'),
    dry_run: bool = typer.Option(False, '--dry-run'),
    json_out: bool = typer.Option(True, '--json'),
    human_delete: bool = typer.Option(False, '--human-delete', help='Allow delete mode to remove edges (safety gate)'),
):
    """Use an LLM to generate rationales and bounded weight suggestions for edges.

    - Reads pending edges (and optionally weak active edges) in the given scope.
    - Calls a local or hosted model to produce { keep, weight, confidence, rationale, type? }.
    - Writes llm_rationale and (optionally) updates weight within a tight bound; may reject or penalize.
    """
    # Env overrides (Sparta/REL09 analogs)
    env_min_w = os.getenv("REL09_MIN_INPUT_WEIGHT")
    if env_min_w:
        try:
            min_input_weight = float(env_min_w)
        except Exception as exc:
            logger.error("parsing REL09_MIN_INPUT_WEIGHT env var failed: {}", exc)
    env_topk = os.getenv("REL09_DEBUG_TOPK")
    if env_topk:
        try:
            top_weight_k = int(env_topk)
        except Exception as exc:
            logger.error("parsing REL09_DEBUG_TOPK env var failed: {}", exc)

    ensure_collections_and_view()
    db = get_db()
    ts = int(time.time())
    # Collect eligible edges with weight gating and optional top-k by weight
    limit_n = int(top_weight_k) if top_weight_k and top_weight_k > 0 else int(top_n)
    sort_clause = "SORT TO_NUMBER(e.weight) DESC" if top_weight_k and top_weight_k > 0 else "SORT e.updated_at ASC"
    aql = f"""
    FOR e IN lesson_edges
      LET w = TO_NUMBER(e.weight)
      LET lf = DOCUMENT('lessons', SPLIT(e._from,'/')[1])
      LET lt = DOCUMENT('lessons', SPLIT(e._to,'/')[1])
      FILTER (@s=='' OR (lf!=null AND lf.scope==@s) OR (lt!=null AND lt.scope==@s))
      FILTER w >= @wmin
      FILTER (e.status=='pending' OR @th>0 AND e.approved==true AND w < @th)
      {sort_clause}
      LIMIT @n
      RETURN {{ edge: e, lf: lf, lt: lt, weight: w }}
    """
    rows = list(db.aql.execute(aql, bind_vars={'s': scope or '', 'n': limit_n, 'th': float(include_active_below or 0.0), 'wmin': float(min_input_weight or 0.0)}))
    updated = 0
    rejected = 0
    errors: list[str] = []
    # Progress logging setup (disabled when --json is used)
    try:
        _PROGRESS_EVERY_SEC = int(os.getenv('PROGRESS_EVERY_SEC', '5') or '5')
    except Exception as exc:
        logger.error("parsing PROGRESS_EVERY_SEC env var failed: {}", exc)
        _PROGRESS_EVERY_SEC = 5
    _progress_enabled = _PROGRESS_EVERY_SEC > 0 and not bool(json_out)
    _total = len(rows)
    _processed = 0
    _cache_skips = 0
    _escalations = 0
    _t0 = time.time()
    _last_log = _t0
    def _maybe_log_progress(force: bool = False) -> None:
        nonlocal _last_log
        if not _progress_enabled:
            return
        now = time.time()
        if force or (now - _last_log) >= _PROGRESS_EVERY_SEC:
            elapsed = now - _t0
            rate = (_processed / elapsed) if elapsed > 0 else 0.0
            try:
                typer.echo(
                    f"llm-score progress: {_processed}/{_total} | updated={updated} "
                    f"rejected={rejected} cache_skip={_cache_skips} escalations={_escalations} "
                    f"elapsed={elapsed:.1f}s rate={rate:.2f}/s"
                )
            except Exception as exc:
                logger.error("typer.echo llm-score progress failed: {}", exc)
            # update last-log timestamp
            _last_log = now

    def clamp(v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))


    def _is_code_pair(src: dict, tgt: dict) -> bool:
        def has_code(d: dict) -> bool:
            if not d:
                return False
            if d.get('language'):
                return True
            loc = d.get('code_location') or {}
            return bool(loc.get('file_path'))
        return has_code(src) or has_code(tgt)

    def _model_order(src: dict, tgt: dict) -> list[str]:
        """Order models to try, honoring explicit --model first."""
        explicit = model or ''
        primary = explicit or os.getenv('OLLAMA_MODEL') or (os.getenv('OLLAMA_PRIMARY_MODEL_FAST') if fast else os.getenv('OLLAMA_PRIMARY_MODEL')) or ('qwen3:8b-instruct' if fast else 'qwen3:14b-instruct')
        fallback = fallback_model or os.getenv('OLLAMA_FALLBACK_MODEL') or 'granite3.3:8b-instruct'
        code_m = code_model or os.getenv('OLLAMA_CODE_MODEL') or 'glm4'
        if provider == 'ollama':
            return [primary]  # only local tags matter here
        if _is_code_pair(src, tgt):
            return [code_m, primary, fallback]
        return [primary, fallback]

    def _score_edge(src: dict, tgt: dict, e: dict, provider: str = 'ollama') -> tuple[dict, str]:
        sim = float(e.get('raw_sim') or e.get('weight') or 0.0)
        overlaps = []
        st = (src.get('title') or '').lower().split(); tt = (tgt.get('title') or '').lower().split()
        tok_overlap = len(set(st) & set(tt))
        if tok_overlap:
            overlaps.append(f"{tok_overlap} title tokens overlap")
        tag_overlap = 0.0
        try:
            tag_overlap = float(e.get('tags_overlap') or 0.0)
        except Exception as exc:
            logger.error("parsing tags_overlap float failed: {}", exc)
        scope_match = 1.0 if (src.get('scope') or '') == (tgt.get('scope') or '') and src.get('scope') else 0.0
        overlaps.append(f"tag overlap~{int(tag_overlap*100)}%")
        if scope_match:
            overlaps.append('scope match')
        prompt = (
            "You are verifying a relationship between two lessons.\n"
            + "Return strict JSON with keys: keep (boolean), weight (0..1), confidence (0..1), type (duplicates|similar_to|solves|related), rationale.\n\n"
            + f"A: {src.get('title') or ''}\n"
            + f"B: {tgt.get('title') or ''}\n"
            + f"faiss_sim: {sim:.2f}; evidence: {', '.join(overlaps)}\n"
            + "JSON: "
        )
        tried = []
        out = { 'error': 'provider-not-implemented' }
        used_model = ''
        order = _model_order(src, tgt)
        for mdl in order:
            tried.append(mdl)
            if provider == 'ollama':
                try:
                    out = _call_ollama(prompt, mdl); used_model = mdl
                except Exception as _e:
                    out = { 'error': f'provider-fallback-failed: {_e}' }
            else:
                # SciLLM/Chutes paved path
                try:
                    from ..llm.client import call_llm_json  # type: ignore
                    prof = (profile or os.getenv('GM_LLM_PROFILE_SCORE') or ("fast" if fast else "accurate"))
                    payload, used = call_llm_json(
                        prompt,
                        profile=prof,
                        timeout_ms=int(os.getenv('GM_LLM_TIMEOUT_MS','120000') or '120000'),
                        max_tokens=512,
                        model=(model or None),
                    )
                    # Blocklist expensive providers/models (e.g., gemini) unless user forced a specific model
                    block = [s.strip().lower() for s in (os.getenv('GM_LLM_BLOCKLIST','gemini,gemini-2.5-flash') or '').split(',') if s.strip()]
                    if (not model) and used and any(b in used.lower() for b in block):
                        fb_prof = os.getenv('GM_LLM_BLOCKLIST_FALLBACK_PROFILE', 'fast')
                        payload, used = call_llm_json(
                            prompt,
                            profile=fb_prof,
                            timeout_ms=int(os.getenv('GM_LLM_TIMEOUT_MS','120000') or '120000'),
                            max_tokens=512,
                            model=None,
                        )
                    out = payload; used_model = used or mdl
                    if (not isinstance(out, dict)) or (('keep' not in out) and ('weight' not in out) and ('confidence' not in out) and ('items' not in out)):
                        raise RuntimeError("llm_json_error_or_malformed")
                except Exception as _e:
                    out = { 'error': f'provider-not-implemented: {_e}' }
            if 'error' in out:
                try:
                    sys.stderr.write(f"[llm-score] provider={provider} model={mdl} error={out.get('error')}\n")
                except Exception as exc:
                    logger.error("stderr write llm-score error failed: {}", exc)
            if 'error' not in out and isinstance(out, dict) and ('keep' in out or 'weight' in out or 'confidence' in out):
                if not used_model:
                    used_model = mdl
                break
        return out, (used_model or (order[0] if order else ''))

    # Optional SciLLM parallel batch (auto-enabled when Router models present unless explicitly disabled)
    _env_parallel = os.getenv('GM_LLM_PARALLEL','').lower()
    _router_present = bool(os.getenv('SCILLM_ROUTER_MODELS','').strip())
    _use_parallel = (_env_parallel in {'1','true','yes'}) or (_router_present and _env_parallel not in {'0','false','no'})
    if provider == 'ollama':
        _use_parallel = False  # keep Ollama path serial & local
    _batch_size = int(os.getenv('GM_LLM_BATCH_SIZE','32') or '32')
    _parallel_prompts: list[str] = []
    _parallel_indices: list[int] = []
    _parallel_results: dict[int, tuple[dict, str]] = {}

    # Prepass to collect prompts for items we plan to score (respect cache skip)
    if _use_parallel and not dry_run:
        for idx, row in enumerate(rows):
            e = row['edge']; lf = row['lf'] or {}; lt = row['lt'] or {}
            faiss_sim = float(e.get('raw_sim') or e.get('weight') or 0.0)
            first_model = (model or os.getenv('OLLAMA_MODEL') or '')
            try:
                import hashlib as _hl
                cache_basis0 = f"{first_model}|{e.get('pair_id') or ''}|{round(faiss_sim,2)}"
                cache_hash0 = _hl.sha1(cache_basis0.encode('utf-8')).hexdigest()
            except Exception as exc:
                logger.error("computing cache hash failed: {}", exc)
                cache_hash0 = ''
            if cache_hash0 and e.get('llm_cache_hash') == cache_hash0 and reject_mode == 'demote' and float(include_active_below or 0.0) <= 0.0:
                if bool(e.get('approved')) and (e.get('status') == 'active') and not bool(e.get('requires_llm')):
                    continue
            # Build overlaps & prompt (mirror _score_edge lightweight path)
            overlaps = []
            st = (lf.get('title') or '').lower().split(); tt = (lt.get('title') or '').lower().split()
            tok_overlap = len(set(st) & set(tt))
            if tok_overlap:
                overlaps.append(f"{tok_overlap} title tokens overlap")
            try:
                tag_overlap = float(e.get('tags_overlap') or 0.0)
            except Exception as exc:
                logger.error("parsing tags_overlap float failed: {}", exc)
                tag_overlap = 0.0
            scope_match = 1.0 if (lf.get('scope') or '') == (lt.get('scope') or '') and lf.get('scope') else 0.0
            overlaps.append(f"tag overlap~{int(tag_overlap*100)}%")
            if scope_match:
                overlaps.append('scope match')
            prompt = (
                "You are verifying a relationship between two lessons.\n"
                + "Return strict JSON with keys: keep (boolean), weight (0..1), confidence (0..1), type (duplicates|similar_to|solves|related), rationale.\n\n"
                + f"A: {lf.get('title') or ''}\n"
                + f"B: {lt.get('title') or ''}\n"
                + f"faiss_sim: {faiss_sim:.2f}; evidence: {', '.join(overlaps)}\n"
                + "JSON: "
            )
            _parallel_prompts.append(prompt)
            _parallel_indices.append(idx)
        if _parallel_prompts:
            try:
                from ..llm.client import call_llm_json_parallel  # type: ignore
                for start in range(0, len(_parallel_prompts), max(1, _batch_size)):
                    chunk_prompts = _parallel_prompts[start:start+_batch_size]
                    chunk_idxs = _parallel_indices[start:start+_batch_size]
                    pairs = call_llm_json_parallel(chunk_prompts, profile=("fast" if fast else "accurate"), timeout_ms=int(os.getenv('GM_LLM_TIMEOUT_MS','120000') or '120000'), max_tokens=512, model=(model or None))
                    for off, (pl, usedm) in enumerate(pairs):
                        _parallel_results[chunk_idxs[off]] = (pl, usedm or (model or ''))
            except Exception as exc:
                logger.error("parallel LLM batch call failed: {}", exc)
                _parallel_results = {}

    for _row_i, row in enumerate(rows):
        e = row['edge']; lf = row['lf'] or {}; lt = row['lt'] or {}
        # LLM cache (base hash uses first-choice model; final hash updated with the actual used model)
        faiss_sim = float(e.get('raw_sim') or e.get('weight') or 0.0)
        first_model = (_model_order(lf, lt) or ['ollama'])[0]
        try:
            import hashlib as _hl
            cache_basis0 = f"{first_model}|{e.get('pair_id') or ''}|{round(faiss_sim,2)}"
            cache_hash0 = _hl.sha1(cache_basis0.encode('utf-8')).hexdigest()
        except Exception as exc:
            logger.error("computing cache hash failed: {}", exc)
            cache_hash0 = ''
        if cache_hash0 and e.get('llm_cache_hash') == cache_hash0 and reject_mode == 'demote' and float(include_active_below or 0.0) <= 0.0:
            # Only skip safe-to-skip cases: already active & approved and not flagged as requiring LLM
            if bool(e.get('approved')) and (e.get('status') == 'active') and not bool(e.get('requires_llm')):
                _processed += 1
                _cache_skips += 1
                _maybe_log_progress()
                continue
        llm = {'keep': True, 'weight': float(e.get('weight') or 0.0), 'confidence': 0.0, 'type': e.get('type') or 'related', 'rationale': '(no llm)'}
        used_model = first_model
        if not dry_run:
            # Use parallel result when available; otherwise score singly
            parallel_hit = _parallel_results.get(_row_i)
            if parallel_hit is not None:
                result, used = parallel_hit
                if 'error' not in (result or {}):
                    llm.update(result)
                    used_model = used or first_model
            else:
                result, used = _score_edge(lf, lt, e, provider=provider)
                if 'error' not in result:
                    llm.update(result)
                    used_model = used or first_model
            # Escalation to GPT-5 for statistically strange cases
            try:
                import random
                st = (lf.get('title') or '').lower().split(); tt = (lt.get('title') or '').lower().split()
                jacc = 0.0
                if st and tt:
                    sa, sb = set(st), set(tt)
                    jacc = (len(sa & sb) / max(1, len(sa | sb)))
                cluster_small = not bool(lf.get('cluster_id') or lt.get('cluster_id'))
                rnd = random.random()
                need_escalate = (
                    (('error' in result) and result.get('error') == 'no-json')
                    or (gray_low <= faiss_sim <= gray_high and jacc < jaccard_min)
                    or cluster_small
                    or (rnd < float(audit_rate or 0.0))
                )
            except Exception as exc:
                logger.error("evaluating escalation condition failed: {}", exc)
                need_escalate = False
            if codex_escalate and need_escalate:
                import asyncio
                try:
                    _t0 = time.time()
                    llm_ce = asyncio.run(_run_codex_exec(prompt, '', int(codex_timeout_ms)))
                    _ms = int((time.time() - _t0) * 1000)
                    if 'error' not in llm_ce:
                        llm.update(llm_ce)
                    try:
                        # include scope for observability-by-scope aggregation
                        _escalations += 1
                        log_event(db, 'llm_escalate', 'codex_exec', { 'model': 'gpt-5', 'took_ms': _ms, 'scope': (scope or (lf.get('scope') or lt.get('scope') or '')) })
                    except Exception as exc:
                        logger.error("log_event llm_escalate failed: {}", exc)
                except Exception as exc:
                    logger.error("codex escalation failed: {}", exc)
        _processed += 1
        keep = bool(llm.get('keep', True)) and float(llm.get('confidence') or 0.0) >= float(conf_min)
        if not keep:
            rejected += 1
            pid = e.get('pair_id')
            try:
                db.collection('rejected_pairs').insert({ 'pair_id': pid, '_key': pid, 'reason': 'llm_veto', 'details': llm.get('rationale') or '', 'last_checked_at': ts })
            except Exception as exc:
                logger.error("inserting into rejected_pairs failed: {}", exc)
                try:
                    db.collection('rejected_pairs').update({ '_key': pid, 'reason': 'llm_veto', 'details': llm.get('rationale') or '', 'last_checked_at': ts })
                except Exception as exc:
                    logger.error("updating rejected_pairs failed: {}", exc)
            # increment rejection_count and last_rejected_at
            new_doc=None
            if not dry_run:
                try:
                    new_doc=list(db.aql.execute('LET d=DOCUMENT(@id) UPDATE d WITH { rejection_count: (d.rejection_count ? d.rejection_count : 0) + 1, last_rejected_at:@ts, updated_at:@ts } IN lesson_edges RETURN NEW', bind_vars={'id': e.get('_id'), 'ts': ts}))[0]
                except Exception as exc:
                    logger.error("incrementing rejection_count in lesson_edges failed: {}", exc)
                    new_doc=None
            rcount=int((new_doc or {}).get('rejection_count') or 1)
            # delete requires human flag
            if reject_mode == 'delete' and human_delete and not dry_run:
                try:
                    db.aql.execute('REMOVE @key IN lesson_edges', bind_vars={'key': e.get('_key')})
                except Exception as exc:
                    logger.error("removing edge from lesson_edges failed: {}", exc)
                _maybe_log_progress()
                continue
            # penalize on schedule or explicit
            if (reject_mode == 'penalize' or rcount>=2) and not dry_run:
                pval= clamp(float(penalty if reject_mode=='penalize' else (0.5 if rcount==2 else 1.0)), 0.0, 1.0)
                try:
                    db.aql.execute('LET d=DOCUMENT(@id) UPDATE d WITH { penalty: @p, status: "pending", updated_at:@ts, llm_cache_hash:@ch, llm_cache_ts:@ts } IN lesson_edges', bind_vars={'id': e.get('_id'), 'p': pval, 'ts': ts, 'ch': cache_hash0})
                except Exception as exc:
                    logger.error("applying penalty to lesson_edge failed: {}", exc)
                _maybe_log_progress()
                continue
            if not dry_run:
                db.aql.execute('LET d=DOCUMENT(@id) UPDATE d WITH { status: "pending", updated_at: @ts, llm_cache_hash:@ch, llm_cache_ts:@ts } IN lesson_edges', bind_vars={'id': e.get('_id'), 'ts': ts, 'ch': cache_hash0})
            _maybe_log_progress()
            continue
        try:
            w0 = float(e.get('weight') or 0.0)
            w1 = clamp(float(llm.get('weight') or w0), 0.0, 1.0)
            new_w = clamp(alpha * w0 + (1.0 - alpha) * w1, w0 - delta, w0 + delta)
        except Exception as exc:
            logger.error("computing blended weight failed: {}", exc)
            new_w = float(e.get('weight') or 0.0)
        new_type = str(e.get('type') or 'related')
        if float(llm.get('confidence') or 0.0) >= float(type_conf_min):
            t_sug = str(llm.get('type') or '')
            if t_sug in {'duplicates','similar_to','solves','related'}:
                new_type = t_sug
        if not dry_run:
            try:
                db.aql.execute(
                    'LET d=DOCUMENT(@id) UPDATE d WITH { weight: @w, confidence:@w, type:@ty, approved:true, status:"active", requires_llm:false, updated_at:@ts, last_verified_at:@ts, llm_rationale:@rat, llm_confidence:@cf, llm_weight_suggested:@ws, llm_model:@m, llm_cache_hash:@ch, llm_cache_ts:@ts } IN lesson_edges',
                    bind_vars={'id': e.get('_id'), 'w': new_w, 'ty': new_type, 'ts': ts, 'rat': llm.get('rationale') or '', 'cf': float(llm.get('confidence') or 0.0), 'ws': float(llm.get('weight') or 0.0), 'm': used_model, 'ch': (__import__('hashlib').sha1(f"{used_model}|{e.get('pair_id') or ''}|{round(faiss_sim,2)}".encode('utf-8')).hexdigest() if used_model else cache_hash0)}
                )
                updated += 1
            except Exception as ex:
                errors.append(str(ex))
        _maybe_log_progress()
    out = { 'meta': { 'scope': scope or '', 'provider': provider, 'model': model or os.getenv('OLLAMA_MODEL') or '' }, 'items': [{ 'updated': updated, 'rejected': rejected }], 'errors': errors }
    # Invariant guard: ensure active edges in the target scope carry an llm_rationale
    try:
        if (scope or '').strip():
            db.aql.execute(
                """
                FOR e IN lesson_edges
                  LET lf = DOCUMENT('lessons', SPLIT(e._from,'/')[1])
                  FILTER lf!=null AND lf.scope==@s AND e.approved==true AND (e.llm_rationale==null OR e.llm_rationale=='')
                  UPDATE e WITH { llm_rationale: 'auto-approved' } IN lesson_edges
                """,
                bind_vars={'s': scope.strip()}
            )
    except Exception as exc:
        logger.error("backfilling llm_rationale for scope failed: {}", exc)
    if json_out:
        print(json.dumps(out, ensure_ascii=False))
    else:
        # Final summary for human monitoring
        try:
            elapsed = time.time() - _t0
            rate = (_processed / elapsed) if elapsed > 0 else 0.0
            typer.echo(
                f"llm-score done: processed={_processed}/{_total} updated={updated} "
                f"rejected={rejected} cache_skip={_cache_skips} escalations={_escalations} "
                f"elapsed={elapsed:.1f}s rate={rate:.2f}/s"
            )
        except Exception as exc:
            logger.error("typer.echo llm-score done summary failed: {}", exc)


@app.command('llm-score-anchor')
def llm_score_anchor(
    anchor: str = typer.Option('', help='Anchor lesson id (lessons/<key>)'),
    batch_file: str = typer.Option('', help='JSONL or single-JSON file with {anchor_id, pairs:[{src_id,tgt_id}], max_tokens?, temperature?}'),
    provider: str = typer.Option('ollama'),
    model: str = typer.Option('', help='Model to use (defaults follow llm-score logic)'),
    conf_min: float = typer.Option(0.60, help='Global min confidence to accept'),
    parallel: bool = typer.Option(False, '--parallel/--no-parallel', help='Use SciLLM parallel_acompletions when available'),
    batch_size: int = typer.Option(32, '--batch-size', min=1, max=256, help='Parallel chunk size'),
    json_out: bool = typer.Option(True, '--json'),
) -> None:
    """Anchor-aware, batched scoring of pairs with labels and rationales.

    Input: --anchor and an implicit pair list (not provided here), or --batch-file with a single JSON line:
      {"anchor_id":"lessons/123","pairs":[{"src_id":"lessons/1","tgt_id":"lessons/2"}],"max_tokens":512,"temperature":0.2}
    Output JSON: { anchor_id, results:[ {src_id,tgt_id,label,confidence,rationale,evidence_spans,raw_tokens} ] }
    Writes: lesson_edges with additional fields { anchor_id, anchor_label, anchor_confidence, evidence_spans }.
    Cache: anchor_score_cache keyed by sha1(anchor_id|sorted(src_id,tgt_id)|model|content_sig)
    """
    ensure_collections_and_view()
    db = get_db()
    try:
        cache_col = db.collection('anchor_score_cache')
    except Exception as exc:
        logger.error("getting anchor_score_cache collection failed: {}", exc)
        try:
            db.create_collection('anchor_score_cache')
            cache_col = db.collection('anchor_score_cache')
        except Exception as exc:
            logger.error("creating anchor_score_cache collection failed: {}", exc)
            cache_col = None
    try:
        ann_col = db.collection('edge_annotations')
    except Exception as exc:
        logger.error("getting edge_annotations collection failed: {}", exc)
        ann_col = None

    def _cache_key(anchor_id: str, a: str, b: str, used_model: str, sig: str) -> str:
        import hashlib as _hl
        m = '|'.join([anchor_id, *sorted([a,b]), used_model or '', sig or ''])
        return _hl.sha1(m.encode('utf-8')).hexdigest()

    # Parse batch
    tasks = []
    anchor_id = anchor.strip()
    if batch_file:
        try:
            import json as _json
            lines = open(batch_file, 'r', encoding='utf-8').read().splitlines()
            # Support single JSON or JSONL with multiple batches
            objs = []
            if len(lines) == 1:
                objs = [_json.loads(lines[0])]
            else:
                for ln in lines:
                    ln = (ln or '').strip()
                    if not ln:
                        continue
                    try:
                        objs.append(_json.loads(ln))
                    except Exception as exc:
                        logger.error("parsing JSON line from anchor file failed: {}", exc)
                        continue
            for obj in objs:
                if not anchor_id:
                    anchor_id = obj.get('anchor_id') or anchor_id
                pair_list = obj.get('pairs')
                if pair_list is not None and len(pair_list) == 0:
                    if json_out:
                        print(json.dumps({'meta': {'ok': False}, 'items': [], 'errors': ['empty_pairs']}, ensure_ascii=False))
                    else:
                        typer.echo('empty pairs in batch')
                    return
                for pr in pair_list or []:
                    s = pr.get('src_id'); t = pr.get('tgt_id')
                    if s and t:
                        tasks.append((s, t))
        except Exception as exc:
            logger.error("parsing anchor pair tasks failed: {}", exc)
    if not anchor_id or not tasks:
        if json_out:
            print(json.dumps({'meta': {'ok': False, 'error': 'missing anchor or tasks'}, 'items': [], 'errors': ['missing_args']}))
        else:
            typer.echo('missing anchor or tasks')
        return

    # Build prompt helper
    LABELS = ['supports','complements','prerequisite','overlaps','conflicts','unrelated']
    def _prompt(a_title: str, s_title: str, t_title: str) -> str:
        return (
            "You are scoring a pair in the context of an anchor requirement/control.\n"
            "Return strict JSON with keys: label, confidence (0..1), rationale, evidence_spans ([]), raw_tokens (int).\n"
            + f"Anchor: {a_title}\n"
            + f"A: {s_title}\n"
            + f"B: {t_title}\n"
            + f"Labels: {', '.join(LABELS)}\n"
            + "JSON: "
        )

    def _title(doc_id: str) -> str:
        try:
            row = list(db.aql.execute("RETURN DOCUMENT(@id).title", bind_vars={'id': doc_id}))
            return (row[0] or '') if row else doc_id
        except Exception as exc:
            logger.error("fetching document title from ArangoDB failed: {}", exc)
            return doc_id

    results = []
    used_model = model or ''
    # Local helpers to avoid NameError in fallback paths
    def _toks(text: str) -> set:
        try:
            return set((text or '').lower().split())
        except Exception as exc:
            logger.error("tokenizing text failed: {}", exc)
            return set()
    def _overlap(a: set, b: set) -> bool:
        return len(a & b) > 0
    # Label-specific minima
    lbl_min_conf = {
        'conflicts': float(os.getenv('LABEL_MIN_CONFLICTS','0.65') or '0.65'),
        'prerequisite': float(os.getenv('LABEL_MIN_PREREQ','0.65') or '0.65'),
        'supports': float(os.getenv('LABEL_MIN_SUPPORTS', os.getenv('LABEL_MIN_GENERIC', '0.60')) or '0.60'),
        'complements': float(os.getenv('LABEL_MIN_COMPLEMENTS', os.getenv('LABEL_MIN_GENERIC', '0.60')) or '0.60'),
        'overlaps': float(os.getenv('LABEL_MIN_OVERLAPS', os.getenv('LABEL_MIN_GENERIC', '0.60')) or '0.60'),
        'unrelated': float(os.getenv('LABEL_MIN_UNRELATED', '1.01') or '1.01'),
    }
    # Optional mapping to semantic types recorded in edge_annotations
    _map_to_type = os.getenv('ANCHOR_MAP_TO_TYPE','0').lower() in {'1','true','yes'}
    _ttl_days = int(os.getenv('ANCHOR_SCORE_TTL_DAYS','30') or '30')
    # Decide parallel mode & prepare caches
    # Default to parallel when Router model list is present unless explicitly disabled
    _env_parallel = os.getenv('GM_LLM_PARALLEL','').lower()
    _router_present = bool(os.getenv('SCILLM_ROUTER_MODELS','').strip())
    use_parallel = parallel or (_env_parallel in {'1','true','yes'}) or (_router_present and _env_parallel not in {'0','false','no'})
    titles = []
    sigs = []
    cache_keys = []
    for src_id, tgt_id in tasks:
        # Content signature based on titles for quick reuse
        a_title = _title(anchor_id); s_title = _title(src_id); t_title = _title(tgt_id)
        titles.append((a_title, s_title, t_title))
        sig = __import__('hashlib').sha1(f"{a_title}|{s_title}|{t_title}".encode('utf-8')).hexdigest()
        sigs.append(sig)
        ck = _cache_key(anchor_id, src_id, tgt_id, model or os.getenv('OLLAMA_MODEL') or '', sig)
        cache_keys.append(ck)
        cached = None
        if cache_col is not None:
            try:
                cached = cache_col.get(ck)
            except Exception as exc:
                logger.error("reading from anchor_score_cache failed: {}", exc)
                cached = None
        if cached:
            res = cached.get('result') or {}
        else:
            res = None
            if cache_col is not None and res is not None:
                try:
                    ttl_use = _ttl_days
                    if accepted and conf < (lbl_thr + low_conf_margin):
                        ttl_use = _ttl_days_low_conf
                    exp = int(time.time()) + (ttl_use * 86400 if ttl_use > 0 else 0)
                    doc = {'_key': ck, 'anchor_id': anchor_id, 'src_id': src_id, 'tgt_id': tgt_id, 'model': used_model or model or '', 'sig': sig, 'result': res, 'ts': int(time.time())}
                    if ttl_use > 0:
                        doc['expires_at'] = exp
                    cache_col.insert(doc)
                except Exception as exc:
                    logger.error("inserting into anchor_score_cache failed: {}", exc)
                    try:
                        up = {'_key': ck, 'result': res, 'ts': int(time.time())}
                        if _ttl_days > 0:
                            up['expires_at'] = int(time.time()) + (_ttl_days * 86400)
                        cache_col.update(up)
                    except Exception as exc:
                        logger.error("updating anchor_score_cache failed: {}", exc)
        if res is None:
            # mark to compute via batch/serial later
            results.append({'src_id': src_id, 'tgt_id': tgt_id, 'label': 'pending', 'confidence': 0.0, 'accepted': False, 'rationale': '', 'evidence_spans': [], 'raw_tokens': 0})
        else:
            label = str(res.get('label') or 'unrelated')
            conf = float(res.get('confidence') or 0.0)
            rat = str(res.get('rationale') or '')
            spans = res.get('evidence_spans') or []
            lbl_thr = lbl_min_conf.get(label, float(conf_min))
            accepted = conf >= lbl_thr
            results.append({'src_id': src_id, 'tgt_id': tgt_id, 'label': label, 'confidence': conf, 'accepted': accepted, 'rationale': rat, 'evidence_spans': spans, 'raw_tokens': int(res.get('raw_tokens') or 0)})

    # Fill in pending results via parallel or serial calls
    to_fill = [i for i, r in enumerate(results) if r.get('label') == 'pending']
    if to_fill:
        anchor_prof = os.getenv('GM_LLM_PROFILE_ANCHOR') or 'accurate'
        if use_parallel:
            from ..llm.client import call_llm_json_parallel  # type: ignore
            # Build prompts for indices
            fill_prompts = []
            for idx in to_fill:
                a_title, s_title, t_title = titles[idx]
                fill_prompts.append(_prompt(a_title, s_title, t_title))
            for start in range(0, len(fill_prompts), max(1, int(batch_size))):
                chunk = fill_prompts[start:start+batch_size]
                idxs = to_fill[start:start+batch_size]
                pairs = call_llm_json_parallel(chunk, profile=anchor_prof, timeout_ms=int(os.getenv('GM_LLM_TIMEOUT_MS','120000') or '120000'), max_tokens=512, model=(model or None))
                for off, (pl, usedm) in enumerate(pairs):
                    ires = idxs[off]
                    res = pl or {}
                    used_model_local = usedm or (model or '')
                    # extract
                    a_title, s_title, t_title = titles[ires]
                    label = str(res.get('label') or ('related' if _overlap(_toks(s_title), _toks(t_title)) else 'unrelated'))
                    conf = float(res.get('confidence') or 0.0)
                    rat = str(res.get('rationale') or '')
                    spans = res.get('evidence_spans') or []
                    lbl_thr = lbl_min_conf.get(label, float(conf_min))
                    accepted = conf >= lbl_thr
                    results[ires].update({'label': label, 'confidence': conf, 'accepted': accepted, 'rationale': rat, 'evidence_spans': spans, 'raw_tokens': int(res.get('raw_tokens') or 0)})
                    # cache store
                    _store = None
                    try:
                        # store in cache via helper logic; inline minimal to avoid refactor
                        ttl_use = _ttl_days if not (accepted and conf < (lbl_thr + low_conf_margin)) else _ttl_days_low_conf
                        if cache_col is not None:
                            exp = int(time.time()) + (ttl_use * 86400 if ttl_use > 0 else 0)
                            doc = {'_key': cache_keys[ires], 'anchor_id': anchor_id, 'src_id': tasks[ires][0], 'tgt_id': tasks[ires][1], 'model': used_model_local, 'sig': sigs[ires], 'result': res, 'ts': int(time.time())}
                            if ttl_use > 0:
                                doc['expires_at'] = exp
                            cache_col.insert(doc)
                    except Exception as exc:
                        logger.error("inserting batch result into anchor_score_cache failed: {}", exc)
                        try:
                            if cache_col is not None:
                                up = {'_key': cache_keys[ires], 'result': res, 'ts': int(time.time())}
                                if _ttl_days > 0:
                                    up['expires_at'] = int(time.time()) + (_ttl_days * 86400)
                                cache_col.update(up)
                        except Exception as exc:
                            logger.error("updating batch result in anchor_score_cache failed: {}", exc)
        else:
            from ..llm.client import call_llm_json  # type: ignore
            for ires in to_fill:
                a_title, s_title, t_title = titles[ires]
                pr = _prompt(a_title, s_title, t_title)
                res, usedm = call_llm_json(pr, profile=anchor_prof, timeout_ms=int(os.getenv('GM_LLM_TIMEOUT_MS','120000') or '120000'), max_tokens=512, model=(model or None))
                label = str(res.get('label') or ('related' if _overlap(_toks(s_title), _toks(t_title)) else 'unrelated'))
                conf = float(res.get('confidence') or 0.0)
                rat = str(res.get('rationale') or '')
                spans = res.get('evidence_spans') or []
                lbl_thr = lbl_min_conf.get(label, float(conf_min))
                accepted = conf >= lbl_thr
                results[ires].update({'label': label, 'confidence': conf, 'accepted': accepted, 'rationale': rat, 'evidence_spans': spans, 'raw_tokens': int(res.get('raw_tokens') or 0)})
        # Persist as annotation on edge
        try:
            db.aql.execute(
                '''
                LET existing = FIRST(FOR e IN lesson_edges FILTER e._from==@f AND e._to==@t LIMIT 1 RETURN e)
                UPSERT { _from:@f, _to:@t, type: (existing ? existing.type : 'related') }
                INSERT { _from:@f, _to:@t, type:(existing ? existing.type : 'related'), approved:false, status:"pending",
                         created_at:@ts, updated_at:@ts, last_verified_at:@ts, pair_id:@pid,
                         anchor_id:@aid, anchor_label:@lab, anchor_confidence:@cf, llm_rationale:@rat, evidence_spans:@sp }
                UPDATE { updated_at:@ts, last_verified_at:@ts, anchor_id:@aid, anchor_label:@lab, anchor_confidence:@cf, llm_rationale:@rat, evidence_spans:@sp }
                IN lesson_edges
                ''',
                bind_vars={
                    'f': src_id, 't': tgt_id, 'pid': _pair_id(src_id, tgt_id), 'aid': anchor_id,
                    'lab': label, 'cf': conf, 'rat': rat, 'sp': spans, 'ts': int(time.time())
                }
            )
        except Exception as exc:
            logger.error("upserting anchor_pair_scores failed: {}", exc)
        # Optional semantic mapping into edge_annotations for operator triage
        if _map_to_type and ann_col is not None:
            semantic = None
            if label == 'prerequisite':
                semantic = 'depends_on'
            elif label == 'conflicts':
                semantic = 'violates'
            if semantic:
                try:
                    ann_col.insert({
                        '_key': __import__('hashlib').sha1(f"{anchor_id}|{src_id}|{tgt_id}|{semantic}".encode('utf-8')).hexdigest(),
                        'anchor_id': anchor_id,
                        '_from': src_id,
                        '_to': tgt_id,
                        'suggested_type': semantic,
                        'confidence': conf,
                        'accepted': accepted,
                        'status': 'pending_approval',
                        'created_at': int(time.time()),
                        'rationale': rat,
                        'evidence_spans': spans,
                    })
                except Exception as exc:
                    logger.error("inserting edge_annotation failed: {}", exc)
    total_tokens = sum(int(r.get('raw_tokens') or 0) for r in results)
    # Build label_counts summary
    label_counts = {}
    for r in results:
        try:
            lab = str(r.get('label') or 'unrelated')
            label_counts[lab] = label_counts.get(lab, 0) + 1
        except Exception as exc:
            logger.error("counting label in results failed: {}", exc)
            continue
    out = { 'meta': { 'anchor_id': anchor_id, 'model': used_model or (model or os.getenv('OLLAMA_MODEL') or ''), 'label_counts': label_counts, 'raw_tokens_total': total_tokens }, 'results': results, 'errors': [] }
    print(json.dumps(out, ensure_ascii=False) if json_out else str(out))
