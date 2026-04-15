from __future__ import annotations
import time
import os
import json
from typing import List, Dict, Any
import typer

from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view
from .recall import bm25_rank
from loguru import logger

app = typer.Typer(add_completion=False)


def _token_overlap(q: str, t: str) -> float:
    qt = {w.lower() for w in q.split() if w}
    tt = {w.lower() for w in t.split() if w}
    if not qt or not tt:
        return 0.0
    return len(qt & tt) / max(1, len(qt))


def _cross_encode_scores(q: str, texts: List[str], model_id: str) -> List[float] | None:
    try:
        from sentence_transformers import CrossEncoder  # type: ignore
    except Exception as exc:
        logger.error("_cross_encode_scores import failed: {exc}", exc=exc)
        return None
    try:
        ce = CrossEncoder(model_id)
        pairs = [(q, t) for t in texts]
        scores = ce.predict(pairs)
        return [float(s) for s in scores]
    except Exception as exc:
        logger.error("_cross_encode_scores caught error: {exc}", exc=exc)
        return None


@app.command()
def run(
    q: str = typer.Option(..., help="Query to rerank for"),
    scope: str = typer.Option("", help="Optional scope filter for candidates"),
    k: int = typer.Option(20, help="How many candidates to score"),
    model: str = typer.Option("bge-reranker-base", help="Cross-encoder model id"),
    json_out: bool = typer.Option(True, "--json", help="Output envelope"),
):
    """Compute reranker scores for top BM25 candidates and cache them.

    On environments without a cross-encoder, falls back to token overlap to avoid heavy deps.
    """
    ensure_collections_and_view()
    db = get_db()
    t0 = time.time()
    cands = bm25_rank(db, q=q, scope=scope, tags=[], k=k)
    texts = [ (c.get('title') or '') + '\n' + (c.get('problem') or '') for c in cands ]
    scores = _cross_encode_scores(q, texts, model)
    if scores is None:
        scores = [_token_overlap(q, t) for t in texts]
    wrote: List[Dict[str, Any]] = []
    ts = int(time.time())
    for c, s in zip(cands, scores):
        try:
            db.aql.execute(
                """
                UPSERT { q:@q, model:@m, lesson_key:@k }
                INSERT { q:@q, model:@m, lesson_key:@k, score:@s, created_at:@ts }
                UPDATE { score:@s, updated_at:@ts }
                IN reranker_cache
                """,
                bind_vars={'q': q, 'm': model, 'k': c.get('_key'), 's': float(s), 'ts': ts}
            )
            wrote.append({'key': c.get('_key'), 'score': float(s)})
        except Exception as exc:
            logger.error("run get failed: {exc}", exc=exc)
    took = int((time.time() - t0) * 1000)
    out = {'meta': {'q': q, 'model': model, 'count': len(wrote), 'took_ms': took}, 'items': wrote, 'errors': []}
    if json_out:
        print(json.dumps(out, ensure_ascii=False))
    else:
        typer.echo(f"reranked {len(wrote)}")

