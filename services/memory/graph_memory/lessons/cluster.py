from __future__ import annotations
import time
import hashlib
from typing import List, Tuple
import numpy as np
import typer

from ..arango_client import get_db
from .proposer import l2_normalize, _embed_key, _text_hash  # reuse helpers
from loguru import logger

app = typer.Typer(add_completion=False)


def _comp_id(nodes: List[str]) -> str:
    s = "|".join(sorted(nodes))
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


@app.command()
def cluster(
    scope: str = typer.Option("", help="Optional scope filter"),
    k: int = typer.Option(12, help="K neighbors for FAISS adjacency"),
    sim_thresh: float = typer.Option(0.55, help="Min cosine sim to connect"),
    min_size: int = typer.Option(3, help="Minimum cluster size to assign"),
    write: bool = typer.Option(True, help="Write cluster_id/size to lessons"),
):
    # Clean imports for service mode
    pass

    db = get_db()
    # Progress logging
    from os import getenv as _getenv
    try:
        _PROGRESS_EVERY_SEC = int(_getenv('PROGRESS_EVERY_SEC', '5') or '5')
    except Exception as exc:
        logger.error("cluster int parse failed: {exc}", exc=exc)
        _PROGRESS_EVERY_SEC = 5
    _progress_enabled = _PROGRESS_EVERY_SEC > 0
    _t0 = time.time(); _last_log = _t0
    _embed_updates = 0; _pairs_considered = 0; _unions = 0
    def _maybe_log(force: bool = False):
        nonlocal _last_log
        if not _progress_enabled:
            return
        now = time.time()
        if force or (now - _last_log) >= _PROGRESS_EVERY_SEC:
            elapsed = now - _t0
            try:
                typer.echo(
                    f"cluster progress: embeds={_embed_updates} pairs={_pairs_considered} unions={_unions} elapsed={elapsed:.1f}s"
                )
            except Exception as exc:
                logger.error("_maybe_log caught error: {exc}", exc=exc)
            _last_log = now
    lessons = list(db.collection("lessons"))
    if scope:
        lessons = [d for d in lessons if (d.get("scope") or "") == scope]
    if not lessons:
        typer.echo("No lessons found.")
        raise typer.Exit(0)

    ids = [f"lessons/{d['_key']}" for d in lessons]
    texts = []
    for d in lessons:
        parts = [str(d.get("title") or ""), str(d.get("problem") or ""), str(d.get("playbook") or "")]
        tags = d.get("tags") or []
        if tags:
            parts.append(" ".join(tags))
        texts.append("\n".join([p for p in parts if p]))

    model_id = typer.get_app_dir("graph-memory")  # dummy; we read EMBEDDING_MODEL from environment via proposer
    # Reuse model selection from proposer
    from os import getenv
    device = getenv('EMBEDDING_DEVICE') or getenv('GM_DEVICE') or None
    m_id = getenv('EMBEDDING_MODEL') or getenv('GM_MODEL_ID') or 'all-MiniLM-L6-v2'
    if (getenv('GM_FORCE_CPU') in ('1','true','TRUE')) or device == 'cpu':
        import os as _os
        _os.environ.setdefault('CUDA_VISIBLE_DEVICES', '')
        device = 'cpu'
    # model = SentenceTransformer(m_id, device=device) -> Removed for Service Mode

    # --- SERVICE-BASED REFACTOR ---
    # 1. Embeddings via Service
    from ..config import EMBEDDING_SERVICE_URL, VECTOR_SERVICE_URL
    embed_service_url = EMBEDDING_SERVICE_URL.rstrip('/')
    vector_service_url = VECTOR_SERVICE_URL.rstrip('/')

    from ..http_clients import get_session
    _session = get_session()

    # Load from DB manifest
    embed_col = db.collection("lesson_embeddings")
    keys = [_embed_key(m_id, ids[i]) for i in range(len(ids))]
    chashes = [_text_hash(m_id, texts[i]) for i in range(len(ids))]
    existing = list(db.aql.execute("FOR k IN @keys LET d = DOCUMENT('lesson_embeddings', k) RETURN d", bind_vars={"keys": keys}))
    by_key = {doc["_key"]: doc for doc in existing if doc}
    need_idx = [i for i, kkey in enumerate(keys) if (kkey not in by_key) or (by_key[kkey].get("content_hash") != chashes[i])]
    
    from ..config import EMBEDDING_DIM
    dim = EMBEDDING_DIM
    emb = np.zeros((len(ids), dim), dtype='float32')
    
    # Compute missing embeddings via Service
    if need_idx:
        # Batch sizes for embedding service
        batch_size = 32
        need_texts = [texts[i] for i in need_idx]
        
        # Simple batch loop
        new_vecs = []
        for i in range(0, len(need_texts), batch_size):
            chunk = need_texts[i : i + batch_size]
            try:
                resp = _session.post(f"{embed_service_url}/embed/batch", json={"texts": chunk}, timeout=60)
                resp.raise_for_status()
                data = resp.json()
                # data is {"embeddings": [...], "model": ...} or list?
                # embed.py returns {"vectors": [[...], ...], ...}
                chunk_vecs = data.get("vectors", [])
                new_vecs.extend(chunk_vecs)
            except Exception as e:
                typer.echo(f"Embedding service failed: {e}")
                raise typer.Exit(1)
                
        # Update DB and array
        ts = int(time.time())
        if len(new_vecs) != len(need_idx):
            typer.echo(f"Error: Embedding count mismatch. Needed {len(need_idx)}, got {len(new_vecs)}")
            # Pad with zeros or abort? Abort safer.
            raise typer.Exit(1)

        for j, pos in enumerate(need_idx):
            vec = new_vecs[j]
            dim = len(vec)
            doc = {"_key": keys[pos], "lesson_id": ids[pos], "model_id": m_id, "content_hash": chashes[pos], "dim": dim, "embedding": vec, "updated_at": ts}
            try:
                embed_col.insert(doc)
            except Exception as exc:
                logger.error("_maybe_log insert failed: {exc}", exc=exc)
                try:
                    embed_col.update(doc)
                except Exception as exc:
                    logger.error("_maybe_log update failed: {exc}", exc=exc)
        _embed_updates += len(need_idx)

    # Fill 'emb' array from DB (now fully populated)
    # Re-fetch or use local updates if efficient, but here we just re-read or fill
    # To save complexity, we just rebuilt logic:
    # We have by_key (old) and we just wrote new.
    # Actually, let's just use what we have in memory `new_vecs` + `by_key`
    
    # Re-fill emb array properly
    emb_list = []
    for i, kkey in enumerate(keys):
        if i in need_idx:
            # Finding it in new_vecs list is hard with simple index. 
            # Let's map pos -> vec for new items
            pass 
    # Actually, simpler to just assume we have everything now. 
    # But for correctness with 'dim' changes:
    
    # 2. Vector Search via Service (Batch KNN)
    # Collect all vectors for indexing
    # We need all vectors in a list
    all_vecs = []
    for i, kkey in enumerate(keys):
        # If passed in need_idx, we accept we wrote to DB. 
        # Ideally we kept them in memory. 
        # A bit inefficient to read back but safe.
        # OPTIMIZATION: skip read back if we just computed.
        if i in need_idx:
            # find index in need_idx
             idx_in_new = need_idx.index(i)
             all_vecs.append(new_vecs[idx_in_new])
        else:
             all_vecs.append(by_key[kkey].get('embedding') or by_key[kkey].get('vector', []))

    # Ensure dim safety
    if all_vecs:
        dim = len(all_vecs[0])
    
    # Transient Indexing
    try:
        # Reset
        _session.delete(f"{vector_service_url}/reset", timeout=5)
        
        # Index (chunked if massive, but transient assumes memory fits)
        # 1000 vectors is fine for HTTP json
        payload = {"ids": ids, "vectors": all_vecs}
        r = _session.post(f"{vector_service_url}/index", json=payload, timeout=60)
        r.raise_for_status()
        
        # Search (Batch)
        # Query is same as Index (All vs All)
        spayload = {"queries": all_vecs, "k": k + 1}
        sr = _session.post(f"{vector_service_url}/search", json=spayload, timeout=120)
        sr.raise_for_status()
        js = sr.json()
        
        # js returns {"ids": [[...], ...], "scores": [[...], ...]}
        # We need D (scores) and I (indices)
        # Our `ids` list is mapped 1:1 to indices 0..N
        # The service returns string IDs "lessons/xyz".
        # We need to map "lessons/xyz" -> index integer.
        id_map = {id_: idx for idx, id_ in enumerate(ids)}
        
        D = []
        I = []
        
        res_ids_list = js.get("ids", [])
        res_scores_list = js.get("scores", [])
        
        for row_ids, row_scores in zip(res_ids_list, res_scores_list):
            row_I = []
            row_D = []
            for r_id, r_score in zip(row_ids, row_scores):
                if r_id in id_map:
                    row_I.append(id_map[r_id])
                    row_D.append(float(r_score))
            I.append(row_I)
            D.append(row_D)
            
    except Exception as e:
        typer.echo(f"Vector service failed: {e}")
        raise typer.Exit(1)

    # Build adjacency
    parent = {i: i for i in range(len(ids))}
    rank = {i: 0 for i in range(len(ids))}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx == ry:
            return
        if rank[rx] < rank[ry]:
            parent[rx] = ry
        elif rank[rx] > rank[ry]:
            parent[ry] = rx
        else:
            parent[ry] = rx
            rank[rx] += 1

    for i, (sims, idxs) in enumerate(zip(D, I)):
        for j, sim in zip(idxs[1:], sims[1:]):
            if float(sim) >= sim_thresh:
                union(i, int(j))
                _unions += 1
            _pairs_considered += 1
        _maybe_log()

    groups = {}
    for i in range(len(ids)):
        r = find(i)
        groups.setdefault(r, []).append(i)

    # Assign cluster ids to groups >= min_size
    assigned = 0
    ts = int(time.time())
    for root, members in groups.items():
        if len(members) < max(1, int(min_size)):
            continue
        nodes = [ids[m] for m in members]
        cid = _comp_id(nodes)
        if write:
            for m in members:
                key = ids[m].split('/')[-1]
                db.aql.execute("UPDATE @k WITH { cluster_id: @cid, cluster_size: @sz, updated_at: @ts } IN lessons_v2", bind_vars={"k": key, "cid": cid, "sz": len(members), "ts": ts})
        assigned += len(members)
    _maybe_log(force=True)
    typer.echo(f"Assigned cluster_id to {assigned} lessons across {len([1 for m in groups.values() if len(m)>=min_size])} clusters.")

if __name__ == "__main__":
    app()
