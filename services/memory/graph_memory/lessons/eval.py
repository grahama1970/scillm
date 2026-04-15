from __future__ import annotations
import os, json, time
from loguru import logger
from typing import List, Dict
import numpy as np
import typer

from ..arango_client import get_db
from .proposer import l2_normalize

app = typer.Typer(add_completion=False)


def _load_ids(path: str) -> List[str]:
    out: List[str] = []
    with open(path, 'r', encoding='utf-8') as f:
        for ln in f:
            s = (ln or '').strip()
            if s:
                out.append(s)
    return out


def _title(db, doc_id: str) -> str:
    try:
        row = list(db.aql.execute("RETURN DOCUMENT(@id).title", bind_vars={'id': doc_id}))
        return (row[0] or '') if row else doc_id
    except Exception as exc:
        logger.error("Failed to fetch title for {}: {}", doc_id, exc)
        return doc_id


def _embed(db, model_id: str, lesson_id: str, text: str) -> np.ndarray:
    try:
        col = db.collection('lesson_embeddings')
    except Exception as exc:
        logger.error("lesson_embeddings collection not available: {}", exc)
        col = None
    k = None
    if col is not None:
        from .proposer import _embed_key as _ek, _text_hash as _th
        k = _ek(model_id, lesson_id)
        doc = None
        try:
            doc = col.get(k)
        except Exception as exc:
            logger.error("Failed to fetch cached embedding for {}: {}", k, exc)
            doc = None
        if doc and doc.get('content_hash') == _th(model_id, text):
            return np.array(doc['vector'], dtype='float32')
    # compute fresh
    from sentence_transformers import SentenceTransformer  # type: ignore
    device = os.getenv('EMBEDDING_DEVICE') or os.getenv('GM_DEVICE') or None
    if (os.getenv('GM_FORCE_CPU') in ('1','true','TRUE')) or device == 'cpu':
        os.environ.setdefault('CUDA_VISIBLE_DEVICES', '')
        device = 'cpu'
    mdl = SentenceTransformer(model_id, device=device)
    vec = mdl.encode([text], convert_to_numpy=True, normalize_embeddings=True)[0]
    vec = l2_normalize(np.array([vec], dtype='float32'))[0]
    if k and col is not None:
        try:
            col.insert({'_key': k, 'lesson_id': lesson_id, 'model_id': model_id, 'content_hash': __import__('hashlib').sha1((model_id+'|'+text).encode('utf-8')).hexdigest(), 'dim': int(vec.shape[0]), 'vector': vec.astype('float32').tolist(), 'updated_at': int(time.time())})
        except Exception as exc:
            logger.error("Embedding insert failed, trying update: {}", exc)
            try:
                col.update({'_key': k, 'vector': vec.astype('float32').tolist(), 'updated_at': int(time.time())})
            except Exception as exc:
                logger.error("Embedding update also failed for {}: {}", k, exc)
    return vec


@app.command('relevance')
def relevance(anchor: str = typer.Option(..., help='Anchor lesson id'), candidates_file: str = typer.Option(..., help='File of candidate lesson ids (one per line)'), k: int = typer.Option(20), json_out: bool = typer.Option(True, '--json')) -> None:
    """Compute non-LLM relevance score using FAISS cosine + token overlap."""
    db = get_db()
    mdl = os.getenv('EMBEDDING_MODEL') or os.getenv('GM_MODEL_ID') or 'all-MiniLM-L6-v2'
    a_title = _title(db, anchor)
    avec = _embed(db, mdl, anchor, a_title)
    ids = _load_ids(candidates_file)
    # Section weights config
    alpha = float(os.getenv('RELEVANCE_ALPHA','0.6') or '0.6')
    beta = float(os.getenv('RELEVANCE_BETA','0.25') or '0.25')
    gamma = float(os.getenv('RELEVANCE_GAMMA','0.15') or '0.15')
    sw_map: Dict[str,float] = {}
    try:
        if os.getenv('SECTION_WEIGHT_JSON'):
            import json as _json
            sw_map = {str(k).lower(): float(v) for k,v in (_json.loads(os.getenv('SECTION_WEIGHT_JSON') or '{}') or {}).items()}
        elif os.getenv('SECTION_WEIGHT_FILE') and os.path.exists(os.getenv('SECTION_WEIGHT_FILE') or ''):
            import json as _json
            sw_map = {str(k).lower(): float(v) for k,v in (_json.loads(open(os.getenv('SECTION_WEIGHT_FILE') or '','r',encoding='utf-8').read()) or {}).items()}
    except Exception as exc:
        logger.error("Failed to load section weight config: {}", exc)
        sw_map = {}
    def _sec_weight(title: str, cid: str) -> float:
        # inspect lesson tags for section:xxx or category:xxx
        try:
            row = list(db.aql.execute("RETURN DOCUMENT(@id)", bind_vars={'id': cid}))
            l = row[0] or {}
            tags = [t.lower() for t in (l.get('tags') or [])]
            keys = []
            for t in tags:
                if t.startswith('section:') or t.startswith('category:') or t.startswith('mitre:'):
                    keys.append(t.split(':',1)[1])
            w = 0.0
            for k_ in keys:
                if k_.lower() in sw_map:
                    w = max(w, float(sw_map[k_.lower()]))
            return w
        except Exception as exc:
            logger.error("Section weight lookup failed for {}: {}", cid, exc)
            return 0.0
    rows = []
    for cid in ids:
        t = _title(db, cid)
        cvec = _embed(db, mdl, cid, t)
        # avec and cvec are normalized; dot equals cosine
        sim = float(avec @ cvec)
        tok_overlap = len(set((a_title.lower().split())) & set((t.lower().split())))
        secw = _sec_weight(t, cid)
        score = alpha*sim + beta*min(1.0, tok_overlap/8.0) + gamma*secw
        score = max(0.0, min(1.0, score))
        rows.append({'id': cid, 'title': t, 'relevance_score': round(score, 4), 'sim': round(sim,4), 'tok_overlap': tok_overlap, 'section_weight': round(secw,4)})
    # Deterministic: sort by (-score, id)
    rows = sorted(rows, key=lambda r: (-r['relevance_score'], r['id']))[:k]
    out = {
        'meta': {
            'anchor_id': anchor,
            'model': mdl,
            'alpha': alpha,
            'beta': beta,
            'gamma': gamma,
        },
        'items': rows,
        'errors': []
    }
    print(json.dumps(out, ensure_ascii=False) if json_out else str(out))


@app.command('relevance-llm')
def relevance_llm(anchor: str = typer.Option(...), batch_file: str = typer.Option(...), json_out: bool = typer.Option(True, '--json')) -> None:
    """LLM relevance wrapper (uses relations.llm-score-anchor under the hood)."""
    from .relations import app as relate_app
    # Re-dispatch to existing tool
    from typer.testing import CliRunner  # type: ignore
    runner = CliRunner()
    res = runner.invoke(relate_app, ['llm-score-anchor', '--batch-file', batch_file, '--json'])
    print(res.stdout.strip())
