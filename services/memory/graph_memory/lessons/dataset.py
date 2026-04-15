from __future__ import annotations
import os, json, typer
from loguru import logger
from typing import List, Dict
from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view

app = typer.Typer(add_completion=False)


def _read_list(path: str) -> List[str]:
    items: List[str] = []
    with open(path, 'r', encoding='utf-8') as f:
        for ln in f:
            s = (ln or '').strip()
            if s:
                items.append(s)
    return items


def _find_control(db, token: str, scope: str) -> Dict[str, str] | None:
    # Match by URL or external_id or title prefix
    if '://' in token:
        rows = list(db.aql.execute(
            "FOR l IN lessons_v2 FILTER (@s=='' OR l.scope==@s) AND l.external_url==@u LIMIT 1 RETURN { _id:l._id, title:l.title, scope:l.scope, tags:l.tags }",
            bind_vars={'u': token, 's': scope or ''}
        ))
        if rows:
            return rows[0]
    # Try multiple LIKE variants to accommodate optional space after colon
    like_patterns = [f"CONTROL %:{token}%", f"CONTROL %: {token}%"]
    rows = []
    for pat in like_patterns:
        rows = list(db.aql.execute(
            "FOR l IN lessons_v2 FILTER (@s=='' OR l.scope==@s) AND (l.external_id==@id OR LIKE(l.title, @tp, true)) LIMIT 1 RETURN { _id:l._id, title:l.title, scope:l.scope, tags:l.tags }",
            bind_vars={'id': token, 'tp': pat, 's': scope or ''}
        ))
        if rows:
            break
    return rows[0] if rows else None


@app.command("generate-qa")
def generate_qa(
    controls_file: str = typer.Option(..., help="Path to file with control IDs/URLs (one per line)"),
    scope: str = typer.Option("", help="Scope for lookups (optional)"),
    out: str = typer.Option(..., help="Output JSONL path"),
    per_control: int = typer.Option(2, help="Questions per control (templated)"),
):
    """Generate a tiny QA seed set from listed controls (deterministic templates)."""
    ensure_collections_and_view()
    db = get_db()
    items = _read_list(controls_file)
    os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
    wrote = 0
    with open(out, 'w', encoding='utf-8') as f:
        for tok in items:
            c = _find_control(db, tok, scope)
            if not c:
                continue
            title = c.get('title') or tok
            # Simple deterministic prompts
            qs = [
                f"What does {title} require?",
                f"How would you verify {title}?",
                f"Which evidence would support {title}?",
            ][: max(1, int(per_control))]
            for q in qs:
                ans = f"This control ({title}) is satisfied when related requirements are met and verified with linked evidence in scope {c.get('scope') or scope or ''}."
                rec = {
                    'scope': c.get('scope') or scope or '',
                    'control_lesson_id': c.get('_id'),
                    'question': q,
                    'answer': ans,
                    'source': 'generator:controls_template_v1'
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                wrote += 1
    print(json.dumps({ 'meta': { 'ok': True, 'count': wrote, 'out': out, 'scope': scope }, 'errors': [] }, ensure_ascii=False))


@app.command("generate-qa-llm")
def generate_qa_llm(
    controls_file: str = typer.Option(...),
    scope: str = typer.Option(""),
    out: str = typer.Option(".artifacts/qa_pairs.jsonl"),
    per_entry: int = typer.Option(4),
    provider: str = typer.Option('openai'),
    model: str = typer.Option(''),
    grounded: bool = typer.Option(True, help="Require grounded answers with citations"),
    max_retries: int = typer.Option(2, help="Retries for ungrounded/insufficient answers"),
    k: int = typer.Option(12, help="Retrieval top-K for grounding"),
    profile: str = typer.Option('', '--profile', help='QA profile (fast|accurate)'),
    parallel: bool = typer.Option(False, '--parallel/--no-parallel', help='Use SciLLM parallel_acompletions when available'),
    batch_size: int = typer.Option(32, '--batch-size', min=1, max=256),
):
    """Batched LLM Q/A generation for controls (with caching)."""
    ensure_collections_and_view()
    db = get_db()
    try:
        qac = db.collection('qa_cache')
    except Exception as exc:
        logger.error("qa_cache collection not found, creating: {}", exc)
        try:
            db.create_collection('qa_cache')
            qac = db.collection('qa_cache')
        except Exception as exc:
            logger.error("Failed to create qa_cache collection: {}", exc)
            qac = None
    items = _read_list(controls_file)
    os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
    wrote = 0
    with open(out, 'w', encoding='utf-8') as f:
        for tok in items:
            c = _find_control(db, tok, scope)
            if not c:
                continue
            title = c.get('title') or tok
            chunk = title
            chash = __import__('hashlib').sha1((model+'|'+(c.get('scope') or scope or '')+'|'+chunk).encode('utf-8')).hexdigest()
            cached = None
            if qac is not None:
                try:
                    cached = qac.get(chash)
                except Exception as exc:
                    logger.error("qa_cache lookup failed for {}: {}", chash, exc)
                    cached = None
            qa_list = []
            if cached:
                qa_list = cached.get('pairs') or []
            else:
                # Grounded or legacy generation
                try:
                    if grounded:
                        # Simple retrieval: top-K lessons by LIKE match on title/problem/playbook
                        rows = list(db.aql.execute(
                            "FOR l IN lessons_v2 FILTER (@s=='' OR l.scope==@s) AND (LIKE(l.title,@tp,true) OR LIKE(l.problem,@tp,true) OR LIKE(l.playbook,@tp,true)) LIMIT @k RETURN {id:l._id,title:l.title,problem:l.problem,playbook:l.playbook}",
                            bind_vars={'s': c.get('scope') or scope or '', 'tp': f"%{title}%", 'k': max(3, int(k))}
                        ))
                        snippets = []
                        for r in rows:
                            text = ((r.get('problem') or '') + '\n' + (r.get('playbook') or '')).strip()
                            if text:
                                snippets.append({'chunk_id': f"{r.get('id')}#0", 'quote': text[:500]})
                        sys_p = (
                            "Answer strictly from the provided evidence snippets. Return JSON: {items:[{question,answer,evidence_refs:[chunk_id],grounded:true|false,grounded_score:0..1,rationale_short}]}. "
                            "If insufficient, set grounded=false and include no evidence_refs."
                        )
                        from ..llm import provider
                        messages = [
                            {'role':'system','content': sys_p},
                            {'role':'user','content': f"Control: {title}\nScope: {c.get('scope') or scope}\nEvidence:\n" + "\n\n".join(f"[{s['chunk_id']}] {s['quote']}" for s in snippets) + f"\nCount: {int(per_entry)}\nJSON:"}
                        ]
                        tries = 0
                        payload = {}
                        while tries <= max_retries:
                            tries += 1
                            txt, _dur, prov = provider.completion(
                                model=(model or provider.resolve_default_model('text')),
                                messages=messages,
                                timeout=max(1.0, float(int(os.getenv('GM_LLM_TIMEOUT_MS','120000')))/1000.0),
                                max_tokens=512,
                            )
                            try:
                                payload = json.loads(txt) if isinstance(txt,str) else {}
                            except Exception as exc:
                                logger.error("JSON parse of LLM response failed, attempting fallback: {}", exc)
                                s0 = (txt or '').find('{'); e0 = (txt or '').rfind('}')
                                payload = json.loads(txt[s0:e0+1]) if isinstance(txt,str) and s0!=-1 and e0!=-1 and e0>s0 else {'items': []}
                            items = payload.get('items') or []
                            grounded_pass = any(bool(it.get('grounded')) and (it.get('evidence_refs') or []) for it in items)
                            if grounded_pass:
                                break
                            messages[-1]['content'] += "\nReminder: If insufficient, set grounded=false explicitly and include no evidence_refs."
                        qa_list = []
                        for it in (payload.get('items') or [])[: max(1, int(per_entry))]:
                            qa_list.append({
                                'question': it.get('question') or f"What does {title} require?",
                                'answer': it.get('answer') or '',
                                '_evidence_refs': it.get('evidence_refs') or [],
                                '_grounded': bool(it.get('grounded', False)),
                                '_grounded_score': float(it.get('grounded_score', 0.0) or 0.0),
                                '_rationale_short': (it.get('rationale_short') or '')[:400],
                                '_retries': tries-1,
                                '_quotes': [s['quote'] for s in snippets][:3],
                            })
                    else:
                        # Legacy non-grounded path
                        from ..llm.client import call_llm_json, call_llm_json_parallel  # type: ignore
                        qa_prof = (profile or os.getenv('GM_LLM_PROFILE_QA') or 'fast')
                        _env_parallel = os.getenv('GM_LLM_PARALLEL','').lower()
                        _router_present = bool(os.getenv('SCILLM_ROUTER_MODELS','').strip())
                        auto_parallel = parallel or (_env_parallel in {'1','true','yes'}) or (_router_present and _env_parallel not in {'0','false','no'})
                        if auto_parallel:
                            pairs = call_llm_json_parallel([prompt], profile=qa_prof, timeout_ms=int(os.getenv('GM_LLM_TIMEOUT_MS','120000') or '120000'), max_tokens=512, model=(model or None))
                            payload = (pairs[0][0] if pairs else {})
                        else:
                            payload, _ = call_llm_json(prompt, profile=qa_prof, timeout_ms=int(os.getenv('GM_LLM_TIMEOUT_MS','120000') or '120000'), max_tokens=512, model=(model or None))
                        qa_list = (payload.get('items') or []) if isinstance(payload, dict) else []
                except Exception as exc:
                    logger.error("LLM QA generation failed for {}: {}", title, exc)
                    qa_list = [{ 'question': f"What does {title} require?", 'answer': f"{title} requires associated evidence and verification (fallback)." }]
                if qac is not None:
                    try:
                        qac.insert({'_key': chash, 'pairs': qa_list, 'ts': int(time.time()), 'model': model})
                    except Exception as exc:
                        logger.error("qa_cache insert failed, trying update: {}", exc)
                        try:
                            qac.update({'_key': chash, 'pairs': qa_list, 'ts': int(time.time()), 'model': model})
                        except Exception as exc:
                            logger.error("qa_cache update also failed for {}: {}", chash, exc)
            for qa in qa_list[: max(1, int(per_entry))]:
                rec = {
                    'scope': c.get('scope') or scope or '',
                    'control_lesson_id': c.get('_id'),
                    'question': qa.get('question') or '',
                    'answer': qa.get('answer') or '',
                    'source': 'generator:controls_llm_v1',
                    'version': 'v1'
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                # Persist to qa_pairs collection if available
                try:
                    qp = db.collection('qa_pairs')
                except Exception as exc:
                    logger.error("qa_pairs collection not found, creating: {}", exc)
                    try:
                        db.create_collection('qa_pairs')
                        qp = db.collection('qa_pairs')
                    except Exception as exc:
                        logger.error("Failed to create qa_pairs collection: {}", exc)
                        qp = None
                if qp is not None:
                    try:
                        if grounded and qa.get('_grounded') and qa.get('_grounded_score',0.0) >= 0.75 and (qa.get('_evidence_refs') or []):
                            db.aql.execute(
                                '''
                                UPSERT { anchor_id:@aid, question:@q, scope:@sc }
                                INSERT { anchor_id:@aid, question:@q, answer:@a, scope:@sc, model:@m,
                                         chunk_sha256:@h, created_at:@ts, evidence_refs:@ev, evidence_quotes:@eq,
                                         grounded_score:@gs, retries:@rt, rationale_short:@rs }
                                UPDATE { answer:@a, model:@m, chunk_sha256:@h, updated_at:@ts, evidence_refs:@ev,
                                         evidence_quotes:@eq, grounded_score:@gs, retries:@rt, rationale_short:@rs }
                                IN qa_pairs
                                ''',
                                bind_vars={'aid': c.get('_id'), 'q': rec['question'], 'a': rec['answer'],
                                           'sc': rec['scope'], 'm': (model or ''), 'h': chash,
                                           'ts': int(__import__('time').time()), 'ev': qa.get('_evidence_refs') or [],
                                           'eq': qa.get('_quotes') or [], 'gs': float(qa.get('_grounded_score',0.0) or 0.0),
                                           'rt': int(qa.get('_retries',0)), 'rs': qa.get('_rationale_short','')}
                            )
                        else:
                            db.aql.execute(
                                '''
                                UPSERT { anchor_id:@aid, question:@q, scope:@sc }
                                INSERT { anchor_id:@aid, question:@q, answer:@a, scope:@sc, model:@m,
                                         chunk_sha256:@h, created_at:@ts }
                                UPDATE { answer:@a, model:@m, chunk_sha256:@h, updated_at:@ts }
                                IN qa_pairs
                                ''',
                                bind_vars={'aid': c.get('_id'), 'q': rec['question'], 'a': rec['answer'],
                                           'sc': rec['scope'], 'm': (model or ''), 'h': chash,
                                           'ts': int(__import__('time').time())}
                            )
                    except Exception as exc:
                        logger.error("Failed to persist QA pair to qa_pairs collection: {}", exc)
                wrote += 1
    print(json.dumps({ 'meta': { 'ok': True, 'count': wrote, 'out': out, 'scope': scope }, 'errors': [] }, ensure_ascii=False))
