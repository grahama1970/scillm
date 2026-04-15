from __future__ import annotations
import time, hashlib, random
from typing import List
import os
import numpy as np
import json
import typer
from loguru import logger

try:  # Optional tree-sitter tokenization support
    from tree_sitter import Parser, Query  # type: ignore
    from tree_sitter_languages import get_language  # type: ignore
except Exception as exc:  # pragma: no cover - optional dependency not installed
    logger.error("tree-sitter optional dependency not available: {}", exc)
    Parser = None  # type: ignore
    Query = None  # type: ignore
    get_language = None  # type: ignore

from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view

app = typer.Typer(add_completion=False)


def _text_hash(model_id: str, text: str) -> str:
    return hashlib.sha1((model_id + '|' + text).encode('utf-8')).hexdigest()


def _embed_key(model_id: str, lesson_id: str) -> str:
    # Stable key per (model, lesson_id)
    return hashlib.sha1((model_id + '|' + lesson_id).encode('utf-8')).hexdigest()


def _as_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except Exception as exc:
        logger.error("_as_int conversion failed for {!r}: {}", v, exc)
        return default


def l2_normalize(x: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(x, axis=1, keepdims=True) + 1e-8
    return x / n


def pair_id(a_id: str, b_id: str) -> str:
    a, b = (a_id, b_id) if a_id <= b_id else (b_id, a_id)
    return hashlib.sha1((a + '|' + b).encode('utf-8')).hexdigest()


_TS_PARSERS: dict[str, Parser] = {}
_TS_QUERIES: dict[str, Query] = {}
_TS_LANGS = ("python", "typescript")


def _tree_sitter_tokens(text: str) -> List[str]:
    if Parser is None or Query is None or get_language is None:
        return []
    source = text.encode("utf-8")
    for lang in _TS_LANGS:
        try:
            parser = _TS_PARSERS.get(lang)
            if parser is None:
                parser = Parser()
                parser.set_language(get_language(lang))
                _TS_PARSERS[lang] = parser
            query = _TS_QUERIES.get(lang)
            if query is None:
                query = Query(get_language(lang), "(identifier) @id")
                _TS_QUERIES[lang] = query
            tree = parser.parse(source)
            captures = query.captures(tree.root_node)
            tokens = []
            for node, _ in captures:
                try:
                    tok = node.text.decode("utf-8").strip()
                except Exception as exc:
                    logger.error("Failed to decode tree-sitter node text: {}", exc)
                    tok = ""
                if tok:
                    tokens.append(tok.lower())
            if tokens:
                return list(dict.fromkeys(tokens))
        except Exception as exc:
            logger.error("tree-sitter parse failed for lang={}: {}", lang, exc)
            continue
    return []


def _toks(s: str) -> List[str]:
    import re

    base = [t.lower() for t in re.findall(r"[A-Za-z0-9_\-]+", s or "")]
    if os.getenv("USE_TREESITTER_TOKS", "0").lower() in {"1", "true", "yes"}:
        tokens = _tree_sitter_tokens(s or "")
        if tokens:
            return tokens
    return base


def _overlap(a: List[str], b: List[str]) -> int:
    if not a or not b:
        return 0
    A, B = set(a), set(b)
    return len(A & B)


def doc_text(d) -> str:
    parts: List[str] = []
    for k in ("title", "problem", "playbook"):
        v = d.get(k)
        if v:
            # For playbook, keep only Idea: lines to reduce noise
            if k == "playbook":
                try:
                    lines = [ln.strip() for ln in str(v).splitlines() if ln.strip()]
                    idea_lines = [ln for ln in lines if ln.lower().startswith('idea:')]
                    if idea_lines:
                        # Optional operator cap
                        try:
                            cap = int(os.getenv('EMBED_CHUNK_LIMIT', '0') or '0')
                        except Exception as exc:
                            logger.error("Failed to parse EMBED_CHUNK_LIMIT for playbook: {}", exc)
                            cap = 0
                        if cap > 0:
                            idea_lines = idea_lines[:cap]
                        # Tiny token-overlap filter vs title+tags tokens
                        title_toks = _toks(d.get('title') or '')
                        tag_toks = _toks(' '.join(d.get('tags') or []))
                        base = title_toks + tag_toks
                        try:
                            min_ov = int(os.getenv('PROPOSER_MIN_OVERLAP', '1') or '1')
                        except Exception as exc:
                            logger.error("Failed to parse PROPOSER_MIN_OVERLAP for playbook: {}", exc)
                            min_ov = 1
                        filt = []
                        for ln in idea_lines:
                            if _overlap(_toks(ln), base) >= max(0, min_ov):
                                filt.append(ln)
                        parts.append("\n".join(filt or idea_lines))
                    else:
                        parts.append(str(v))
                except Exception as exc:
                    logger.error("Playbook extraction failed, using raw value: {}", exc)
                    parts.append(str(v))
            else:
                parts.append(str(v))
    # Include knowledge chunks harvested during ingestion (e.g., arXiv ideas)
    chunks = d.get("chunks") or d.get("ideas")
    if chunks:
        try:
            # Optional operator cap
            try:
                cap = int(os.getenv('EMBED_CHUNK_LIMIT', '0') or '0')
            except Exception as exc:
                logger.error("Failed to parse EMBED_CHUNK_LIMIT for chunks: {}", exc)
                cap = 0
            # Token overlap filter vs. title+tags
            title_toks = _toks(d.get('title') or '')
            tag_toks = _toks(' '.join(d.get('tags') or []))
            base = title_toks + tag_toks
            try:
                min_ov = int(os.getenv('PROPOSER_MIN_OVERLAP', '1') or '1')
            except Exception as exc:
                logger.error("Failed to parse PROPOSER_MIN_OVERLAP for chunks: {}", exc)
                min_ov = 1
            filt = []
            for x in chunks:
                if not x:
                    continue
                s = str(x).strip()
                if not s:
                    continue
                if _overlap(_toks(s), base) >= max(0, min_ov):
                    filt.append(s)
                if cap > 0 and len(filt) >= cap:
                    break
            parts.append("\n".join(filt or [str(x).strip() for x in chunks if x][: (cap or 6)]))
        except Exception as exc:
            logger.error("Chunks processing failed for doc_text: {}", exc)
    if d.get('code_symbol'):
        lang = (d.get('language') or '').strip()
        if lang:
            parts.append(str(lang))
        loc = d.get('code_location') or {}
        path = loc.get('file_path')
        if path:
            parts.append(str(path))
        params = d.get('parameters') or []
        if params:
            parts.append(' '.join([str(p) for p in params if p]))
    tags = d.get("tags") or []
    if tags:
        parts.append(" ".join(tags))
    return "\n".join(parts)


def tags_overlap(a: List[str], b: List[str]) -> float:
    A, B = set([t.lower() for t in a or []]), set([t.lower() for t in b or []])
    if not A and not B:
        return 0.0
    inter = len(A & B)
    uni = len(A | B)
    return inter / max(1, uni)


@app.command()
def propose(
    k: int = typer.Option(12, help="K neighbors"),
    sim_thresh: float = typer.Option(0.55, help="Min cosine sim"),
    min_top: int = typer.Option(3, help="Ensure at least top-N evaluate"),
    scope: str = typer.Option("", help="Optional scope filter for candidates"),
    ids_file: str = typer.Option("", help="Optional file of external_ids or lesson ids to restrict candidate pool"),
    dry_run: bool = typer.Option(False, help="Do not write edges; just print candidates"),
    updated_since: int = typer.Option(0, help="Only evaluate sources with updated_at >= ts (0 disables)"),
    write_limit: int = typer.Option(0, help="Max number of edges to write (0 = no limit)"),
):
    try:
        import faiss  # type: ignore
        from sentence_transformers import SentenceTransformer  # type: ignore
    except Exception as e:
        typer.echo(f"FAISS or sentence-transformers not available: {e}")
        raise typer.Exit(2)

    db = get_db()
    try:
        ensure_collections_and_view()
    except Exception as exc:
        logger.error("ensure_collections_and_view failed (non-fatal): {}", exc)
    lessons = list(db.collection("lessons"))
    if scope:
        lessons = [d for d in lessons if (d.get("scope") or "") == scope]
    if ids_file:
        try:
            with open(ids_file, "r", encoding="utf-8") as f:
                wanted = {ln.strip() for ln in f if ln.strip()}
            def _want(d):
                return (d.get("external_id") in wanted) or (f"lessons/{d.get('_key')}" in wanted) or (d.get("title") in wanted)
            lessons = [d for d in lessons if _want(d)]
        except Exception as exc:
            logger.error("Failed to read ids_file {!r}: {}", ids_file, exc)
    if not lessons:
        typer.echo("No lessons found.")
        raise typer.Exit(0)

    texts = [doc_text(d) for d in lessons]
    ids = [f"lessons/{d['_key']}" for d in lessons]
    tags_list = [d.get("tags") or [] for d in lessons]
    scopes = [d.get("scope") or "" for d in lessons]

    model_id = os.getenv('EMBEDDING_MODEL') or os.getenv('GM_MODEL_ID') or 'all-MiniLM-L6-v2'
    device = os.getenv('EMBEDDING_DEVICE') or os.getenv('GM_DEVICE') or None
    if (os.getenv('GM_FORCE_CPU') in ('1','true','TRUE')) or device == 'cpu':
        os.environ.setdefault('CUDA_VISIBLE_DEVICES', '')
        device = 'cpu'
    # Accept both full hub path and short name
    if '/' in model_id:
        model = SentenceTransformer(model_id, device=device)
    else:
        model = SentenceTransformer(model_id, device=device)

    # Embedding cache in Arango (collection: lesson_embeddings)
    try:
        embed_col = db.collection("lesson_embeddings")
    except Exception as exc:
        logger.error("lesson_embeddings collection not available: {}", exc)
        embed_col = None
    embed_keys = [_embed_key(model_id, ids[i]) for i in range(len(ids))]
    content_hashes = [_text_hash(model_id, texts[i]) for i in range(len(ids))]
    by_key = {}
    if embed_col is not None:
        existing = list(
            db.aql.execute(
                """
                FOR k IN @keys
                  LET d = DOCUMENT('lesson_embeddings', k)
                  RETURN d
                """,
                bind_vars={"keys": embed_keys},
            )
        )
        by_key = {doc["_key"]: doc for doc in existing if doc}
    need_idxs: List[int] = []
    if by_key:
        for i, kkey in enumerate(embed_keys):
            doc = by_key.get(kkey)
            if not doc or doc.get("content_hash") != content_hashes[i]:
                need_idxs.append(i)
    else:
        need_idxs = list(range(len(ids)))

    # Progress logging
    try:
        _PROGRESS_EVERY_SEC = int(os.getenv('PROGRESS_EVERY_SEC', '5') or '5')
    except Exception as exc:
        logger.error("Failed to parse PROGRESS_EVERY_SEC: {}", exc)
        _PROGRESS_EVERY_SEC = 5
    _progress_enabled = _PROGRESS_EVERY_SEC > 0
    _t0 = time.time()
    _last_log = _t0
    _processed_sources = 0
    _skipped_sources = 0
    _wrote_edges = 0

    def _maybe_log(force: bool = False):
        nonlocal _last_log
        if not _progress_enabled:
            return
        now = time.time()
        if force or (now - _last_log) >= _PROGRESS_EVERY_SEC:
            elapsed = now - _t0
            total = _total_sources if '_total_sources' in globals() else len(ids)
            rate = (_processed_sources / elapsed) if elapsed > 0 else 0.0
            try:
                typer.echo(
                    f"proposer progress: {_processed_sources}/{total} | wrote={_wrote_edges} "
                    f"skipped={_skipped_sources} embed_updates={_embed_updates} elapsed={elapsed:.1f}s rate={rate:.2f}/s"
                )
            except Exception as exc:
                logger.error("Progress logging echo failed: {}", exc)
            _last_log = now

    from ..config import EMBEDDING_DIM
    dim = EMBEDDING_DIM
    emb = np.zeros((len(ids), dim), dtype='float32')
    if need_idxs:
        to_encode = [texts[i] for i in need_idxs]
        computed = model.encode(to_encode, convert_to_numpy=True, normalize_embeddings=True)
        computed = l2_normalize(computed.astype('float32'))
        dim = computed.shape[1] if hasattr(computed, 'shape') else dim
        if emb.shape[1] != dim:
            emb = np.zeros((len(ids), dim), dtype='float32')
        ts_now = int(time.time())
        for pos, emb_vec in zip(need_idxs, computed):
            vec = emb_vec.astype('float32').tolist()
            if embed_col is not None:
                doc = {
                    "_key": embed_keys[pos],
                    "lesson_id": ids[pos],
                    "model_id": model_id,
                    "content_hash": content_hashes[pos],
                    "dim": len(vec),
                    "embedding": vec,
                    "updated_at": ts_now,
                }
                try:
                    embed_col.insert(doc)
                except Exception as exc:
                    logger.error("embed_col.insert failed, trying update: {}", exc)
                    try:
                        embed_col.update(doc)
                    except Exception as exc2:
                        logger.error("embed_col.update also failed for key={}: {}", doc.get('_key'), exc2)
            emb[pos, : len(vec)] = np.array(vec, dtype='float32')
    # Fill cached vectors for the rest
    if by_key:
        for i, kkey in enumerate(embed_keys):
            if (emb[i] != 0).any():
                continue
            doc = by_key.get(kkey)
            if doc and doc.get('vector'):
                vec = np.array(doc['vector'], dtype='float32')
                if emb.shape[1] != len(vec):
                    emb = np.zeros((len(ids), len(vec)), dtype='float32')
                emb[i, : len(vec)] = vec
    # Track how many embeddings we updated (for logging)
    _embed_updates = len(need_idxs)
    # As a final fallback, compute any remaining zeros
    fallback_idxs = [i for i in range(len(ids)) if not (emb[i] != 0).any()]
    if fallback_idxs:
        to_encode = [texts[i] for i in fallback_idxs]
        fb = model.encode(to_encode, convert_to_numpy=True, normalize_embeddings=True)
        fb = l2_normalize(fb.astype('float32'))
        if emb.shape[1] != fb.shape[1]:
            emb = np.zeros((len(ids), fb.shape[1]), dtype='float32')
        for pos, emb_vec in zip(fallback_idxs, fb):
            emb[pos, : emb_vec.shape[0]] = emb_vec
        _embed_updates += len(fallback_idxs)

    # Initial embedding summary log
    _maybe_log(force=True)

    # KNN search: try cuVS/vector service first, fallback to local FAISS
    D, I = None, None
    from ..config import VECTOR_SERVICE_URL
    if VECTOR_SERVICE_URL:
        try:
            import requests as _req
            url = VECTOR_SERVICE_URL.rstrip('/')
            # Reset for fresh index (transient pattern)
            try:
                _req.delete(f"{url}/reset", timeout=5)
            except Exception as exc:
                logger.error("cuVS reset request failed (non-fatal): {}", exc)
            # Index all embeddings
            payload = {"ids": ids, "vectors": emb.tolist()}
            r = _req.post(f"{url}/index", json=payload, timeout=30)
            r.raise_for_status()
            # Batch search (all-vs-all)
            spayload = {"queries": emb.tolist(), "k": k + 1}
            sr = _req.post(f"{url}/search", json=spayload, timeout=120)
            sr.raise_for_status()
            js = sr.json()
            # Parse batch results: expects {results: [{ids: [...], scores: [...]}, ...]}
            results = js.get('results', [])
            if results and len(results) == len(ids):
                D = np.array([[r['scores'][j] if j < len(r.get('scores', [])) else 0.0 for j in range(k + 1)] for r in results], dtype='float32')
                # Map string ids back to indices
                id_to_idx = {id_: idx for idx, id_ in enumerate(ids)}
                I = np.array([[id_to_idx.get(r['ids'][j], -1) if j < len(r.get('ids', [])) else -1 for j in range(k + 1)] for r in results], dtype='int64')
        except Exception as e:
            # Log fallback to FAISS
            import sys
            print(f"[proposer] cuVS service failed ({e}), falling back to FAISS", file=sys.stderr)
            D, I = None, None

    # Fallback to local FAISS if cuVS unavailable or failed
    if D is None or I is None:
        index = faiss.IndexFlatIP(emb.shape[1])
        try:
            use_gpu = str(os.getenv('GM_USE_GPU', '0')).lower() in {'1','true','yes'}
            if use_gpu and hasattr(faiss, 'StandardGpuResources'):
                dev = int(os.getenv('GM_CUDA_DEVICE', os.getenv('CUDA_DEVICE', '0')) or '0')
                res = faiss.StandardGpuResources()
                index = faiss.index_cpu_to_gpu(res, dev, index)
        except Exception as exc:
            logger.error("FAISS GPU init failed, falling back to CPU: {}", exc)
        index.add(emb)
        D, I = index.search(emb, k + 1)

    ts = int(time.time())
    wrote = 0
    # Incremental filter: only consider sources matching updated_since if provided (>0)
    consider = set(range(len(ids)))
    _updated_since = _as_int(updated_since, 0)
    if _updated_since > 0:
        consider = {i for i, d in enumerate(lessons) if int(d.get("updated_at") or 0) >= _updated_since}
    _total_sources = len(consider) if consider else len(ids)
    _maybe_log(force=True)

    # Optional relation classifier (zero-shot) behind env flag
    use_rel_cls = os.getenv('RELATION_USE_CLASSIFIER') in ('1','true','TRUE')
    rel_model = os.getenv('RELATION_CLASSIFIER_MODEL', 'facebook/bart-large-mnli')
    rel_thresh = float(os.getenv('RELATION_CLASSIFIER_THRESH', '0.6'))
    zsc = None
    if use_rel_cls:
        try:
            from transformers import pipeline  # type: ignore
            zsc = pipeline('zero-shot-classification', model=rel_model)
        except Exception as exc:
            logger.error("Zero-shot classifier pipeline failed to load: {}", exc)
            zsc = None

    for i, (sims, idxs) in enumerate(zip(D, I)):
        if i not in consider:
            _processed_sources += 1
            _skipped_sources += 1
            _maybe_log()
            continue
        src_id = ids[i]
        src_tags = tags_list[i]
        src_scope = scopes[i]
        cands = []
        for j, sim in zip(idxs[1:], sims[1:]):
            cand_id = ids[j]
            if cand_id == src_id:
                continue
            cands.append((cand_id, float(sim), j))
        cands = sorted(cands, key=lambda x: x[1], reverse=True)
        cands_eval = [c for c in cands if c[1] >= sim_thresh]
        if len(cands_eval) < min_top:
            cands_eval = cands[: min_top]

        for cand_id, sim, j in cands_eval:
            pid = pair_id(src_id, cand_id)
            # skip if rejected
            try:
                rej = db.collection("rejected_pairs").get(pid)
                if rej:
                    continue
            except Exception as exc:
                logger.error("rejected_pairs lookup failed for pair {}: {}", pid, exc)
            tgt = lessons[j]
            tgt_tags = tags_list[j]
            tgt_scope = scopes[j]
            tovl = tags_overlap(src_tags, tgt_tags)
            scope_match = 1.0 if src_scope and (src_scope == tgt_scope) else 0.0
            overlaps = []
            if tovl >= 0.01:
                overlaps.append(f"{int(tovl*100)}% tag overlap")
            if scope_match:
                overlaps.append("scope match")
            src_title = (lessons[i].get("title") or "").lower().split()
            tgt_title = (tgt.get("title") or "").lower().split()
            tok_overlap = len(set(src_title) & set(tgt_title))
            if tok_overlap >= 2:
                overlaps.append(f"{tok_overlap} title tokens overlap")
            if len(overlaps) < 2:
                try:
                    existing = db.collection("rejected_pairs").get(pid)
                    if existing:
                        db.collection("rejected_pairs").update({
                            "_key": pid,
                            "attempts": int(existing.get("attempts") or 1) + 1,
                            "last_checked_at": ts,
                            "last_reason": "insufficient concrete overlap",
                        })
                    else:
                        db.collection("rejected_pairs").insert({
                            "_key": pid,
                            "pair_id": pid,
                            "reason": "insufficient concrete overlap",
                            "last_checked_at": ts,
                            "attempts": 1,
                            "last_reason": "insufficient concrete overlap",
                        })
                except Exception as exc:
                    logger.error("rejected_pairs upsert failed for pair {}: {}", pid, exc)
                continue
            weight = 0.60 * sim + 0.15 * tovl + 0.15 * scope_match
            weight = max(0.0, min(1.0, weight))
            # Simple type classification (heuristic + optional zero-shot classifier)
            rtype = 'related'
            if sim >= 0.92 and tok_overlap >= 3 and scope_match == 1.0:
                rtype = 'duplicates'
            elif any(w in (lessons[i].get('title') or '').lower() for w in ('fix', 'gotchas', 'troubleshooting', 'stability')) and any(w in (tgt.get('title') or '').lower() for w in ('issue', 'pitfall', 'problem')):
                rtype = 'solves'
            elif sim >= 0.80 and tok_overlap >= 2:
                rtype = 'similar_to'
            if zsc is not None:
                try:
                    labels = ['solves','duplicates','similar_to','related']
                    hyp = f"The relation from A to B is {{label}}."
                    prem = (lessons[i].get('title') or '') + '\n' + (tgt.get('title') or '')
                    outc = zsc(prem, candidate_labels=labels, hypothesis_template=hyp)
                    lbl = outc['labels'][0]
                    sc = float(outc['scores'][0])
                    if sc >= rel_thresh:
                        rtype = lbl
                except Exception as exc:
                    logger.error("Zero-shot classification failed for pair: {}", exc)
            rationale = f"{rtype} because: {', '.join(overlaps)}"
            use_llm = os.getenv('LLM_ENFORCE','1').lower() in {'1','true','yes'}
            approved = False if use_llm else ((weight >= 0.60) and (scope_match == 1.0) and (sim >= 0.55))
            if dry_run:
                print(f"PAIR {src_id} ~ {cand_id} type={rtype} sim={sim:.2f} weight={weight:.2f} approved={approved} :: {rationale}")
                continue
            provenance_info = {}
            for lesson in (lessons[i], tgt):
                loc = lesson.get('code_location') or {}
                file_path = loc.get('file_path')
                if not file_path:
                    continue
                entry = {
                    'lesson_id': f"lessons/{lesson.get('_key')}",
                    'file_path': file_path,
                    'language': lesson.get('language'),
                    'start_line': loc.get('start_line'),
                    'end_line': loc.get('end_line'),
                }
                provenance_info.setdefault('tree_sitter', []).append(entry)
                try:
                    snap_key = hashlib.sha1(f"{lesson.get('scope') or scope}|{file_path}".encode('utf-8')).hexdigest()
                    snap = db.collection('symbol_snapshots').get(snap_key)
                    if snap and snap.get('lsp_enrichment'):
                        provenance_info.setdefault('lsp', []).extend(snap.get('lsp_enrichment'))
                except Exception as exc:
                    logger.error("symbol_snapshots lookup failed: {}", exc)
            prov_arg = provenance_info if provenance_info else None
            # Symmetric types insert both directions, directional only src->tgt
            dirs = ((src_id, cand_id), (cand_id, src_id)) if rtype in ('duplicates','similar_to','related') else ((src_id, cand_id),)
            for frm, to in dirs:
                aql = (
                    "UPSERT { _from: @from, _to: @to, type: @ty } "
                    "INSERT MERGE({ _from: @from, _to: @to, type: @ty, source: 'faiss', weight: @w, raw_sim: @sim, confidence: @conf, approved: @appr, rationale: @rat, rationales: [ { by: 'agent', text: @rat, at: @ts } ], status: @status, created_at: @ts, updated_at: @ts, last_verified_at: @ts, pair_id: @pid, decay_policy: 'standard', requires_llm: @req_llm }, @prov != null ? { provenance: @prov } : {}) "
                    "UPDATE MERGE({ source: 'faiss', weight: @w, raw_sim: @sim, confidence: @conf, approved: @appr, rationale: @rat, updated_at: @ts, last_verified_at: @ts, status: @status, pair_id: @pid, requires_llm: @req_llm }, @prov != null ? { provenance: MERGE(OLD.provenance, @prov) } : {}) IN lesson_edges "
                    "RETURN { old: OLD, new: NEW }"
                )
                res = list(db.aql.execute(aql, bind_vars={
                    'from': frm, 'to': to, 'ty': rtype, 'w': weight, 'sim': sim, 'conf': weight, 'appr': approved, 'rat': rationale, 'ts': ts, 'status': ('pending' if os.getenv('LLM_ENFORCE','1').lower() in {'1','true','yes'} else ('active' if approved else 'pending')), 'pid': pid, 'prov': prov_arg, 'req_llm': (os.getenv('LLM_ENFORCE','1').lower() in {'1','true','yes'})
                }))
                try:
                    if res and res[0].get('old'):
                        old = res[0]['old']
                        payload = json.dumps(old, sort_keys=True, ensure_ascii=False)
                        commit_id = hashlib.sha1(payload.encode('utf-8')).hexdigest()
                        db.collection('edge_revisions').insert({
                            'edge_id': old.get('_id'),
                            'pair_id': old.get('pair_id'),
                            'rev': int((old.get('rev') or 0)) + 1,
                            'snapshot': old,
                            'commit_id': commit_id,
                            'at': ts,
                            'by': 'faiss_proposer',
                        })
                except Exception as exc:
                    logger.error("edge_revisions insert failed: {}", exc)
                wrote += 1
                _wrote_edges += 1
            _write_limit = _as_int(write_limit, 0)
            if _write_limit and wrote >= _write_limit:
                print(f"Write limit reached: {wrote}")
                print(f"Last pair: {src_id} ~ {cand_id}")
                print("Further candidates suppressed.")
                print(f"Processed sources: {len(consider)} of {len(ids)}")
                print(f"Model: {model_id}")
                _maybe_log(force=True)
                return
        _processed_sources += 1
        _maybe_log()
    _maybe_log(force=True)
    print(f"Proposed/updated {wrote} edges.")
