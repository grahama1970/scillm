"""Embedding backfill endpoint.

Handles both text (384d MiniLM) and visual (2048d Qwen3-VL) embedding backfill.

ArangoDB 3.12 sparse vector index bug workaround:
  - Adding a NEW vector field to existing docs fails with "vector field not present"
  - even when sparse=true (which should allow missing fields)
  - Workaround: temporarily drop the blocking index, update, recreate

Usage:
    POST /backfill
    {
        "collection": "datalake_chunks",
        "field": "embedding_visual",      # target field
        "dimension": 2048,                # 384 for text, 2048 for visual
        "service_url": "http://127.0.0.1:8603",  # embedding service
        "batch_size": 16,
        "limit": 0,                       # 0 = all
        "filter": {"asset_type": ["Table", "Figure"]},  # optional
        "text_field": "text",             # source text field
        "min_text_len": 10                # skip docs with shorter text
    }
"""
from __future__ import annotations

import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, BackgroundTasks
from loguru import logger
from pydantic import BaseModel, Field

router = APIRouter(tags=["backfill"])


class BackfillRequest(BaseModel):
    """Request for embedding backfill."""
    collection: str
    field: str = "embedding"
    dimension: int = 384
    service_url: str = "http://127.0.0.1:8602"  # MiniLM default
    model: Optional[str] = None  # For vLLM services (e.g., "Qwen/Qwen3-VL-Embedding-2B")
    batch_size: int = 32
    limit: int = 0  # 0 = all
    filter: Optional[dict] = None  # e.g., {"asset_type": ["Table", "Figure"]}
    text_field: str = "text"
    min_text_len: int = 10
    max_text_chars: int = 4000  # truncate to avoid OOM
    drop_index: bool = True  # workaround for ArangoDB bug


class BackfillStatus(BaseModel):
    """Status of a backfill operation."""
    collection: str
    field: str
    total: int
    processed: int
    skipped: int
    errors: int
    elapsed_s: float
    rate_per_s: float
    complete: bool


# In-memory status tracking (simple, non-persistent)
_backfill_status: dict[str, BackfillStatus] = {}


def _build_filter_aql(req: BackfillRequest) -> tuple[str, dict]:
    """Build AQL filter clause from request."""
    clauses = [f"d.{req.field} == null"]
    bind_vars: dict[str, Any] = {}
    
    if req.filter:
        for i, (key, val) in enumerate(req.filter.items()):
            if isinstance(val, list):
                bind_vars[f"filter_{i}"] = val
                clauses.append(f"d.{key} IN @filter_{i}")
            else:
                bind_vars[f"filter_{i}"] = val
                clauses.append(f"d.{key} == @filter_{i}")
    
    if req.min_text_len > 0:
        bind_vars["min_len"] = req.min_text_len
        clauses.append(f"LENGTH(d.{req.text_field} || \"\") >= @min_len")
    
    return " AND ".join(clauses), bind_vars


def _get_or_drop_index(db, collection: str, field: str, drop: bool) -> Optional[dict]:
    """Find vector index on field. Optionally drop it (returns index params for recreation)."""
    coll = db.collection(collection)
    for idx in coll.indexes():
        if idx.get("type") == "vector" and field in idx.get("fields", []):
            if drop:
                params = idx.copy()
                logger.info(f"Dropping vector index {idx['name']} on {collection}.{field}")
                coll.delete_index(idx["id"])
                return params
            return idx
    return None


def _drop_all_vector_indexes(db, collection: str) -> list[dict]:
    """Drop ALL vector indexes on a collection. Returns list of dropped index params for recreation.
    
    ArangoDB 3.12 bug: ANY vector index blocks updates to docs missing ANY indexed vector field,
    even with sparse=true. Workaround: drop ALL vector indexes, update, recreate.
    """
    coll = db.collection(collection)
    dropped = []
    for idx in coll.indexes():
        if idx.get("type") == "vector":
            params = idx.copy()
            logger.info(f"Dropping vector index {idx['name']} on {collection}")
            coll.delete_index(idx["id"])
            dropped.append(params)
    return dropped


def _recreate_all_indexes(db, collection: str, indexes: list[dict]) -> None:
    """Recreate multiple vector indexes."""
    for idx_params in indexes:
        _recreate_index(db, collection, idx_params)


def _recreate_index(db, collection: str, idx_params: dict) -> None:
    """Recreate a vector index from saved params."""
    coll = db.collection(collection)
    field = idx_params["fields"][0]
    params = idx_params.get("params", {})
    
    logger.info(f"Recreating vector index on {collection}.{field}")
    coll.add_index({
        "type": "vector",
        "fields": [field],
        "name": idx_params.get("name", f"{collection}_vec_{field}"),
        "sparse": idx_params.get("sparse", True),
        "params": {
            "dimension": params.get("dimension", 384),
            "metric": params.get("metric", "cosine"),
            "nLists": params.get("nLists", 100),
            "trainingIterations": params.get("trainingIterations", 25),
            "defaultNProbe": params.get("defaultNProbe", 1),
        }
    })


def _encode_batch(texts: list[str], req: BackfillRequest) -> list[list[float]]:
    """Encode texts via embedding service."""
    texts = [t[:req.max_text_chars] for t in texts]
    
    with httpx.Client(timeout=120.0) as client:
        if req.model:
            # vLLM OpenAI-compatible API
            resp = client.post(
                f"{req.service_url}/v1/embeddings",
                json={"model": req.model, "input": texts}
            )
            resp.raise_for_status()
            return [d["embedding"] for d in resp.json()["data"]]
        else:
            # Custom /embed/batch API (MiniLM service)
            resp = client.post(
                f"{req.service_url}/embed/batch",
                json={"texts": texts}
            )
            resp.raise_for_status()
            return resp.json().get("vectors", [])


def _run_backfill(req: BackfillRequest) -> BackfillStatus:
    """Execute the backfill synchronously."""
    from ...arango_client import get_db
    
    status_key = f"{req.collection}.{req.field}"
    db = get_db()
    coll = db.collection(req.collection)
    
    # Build query
    filter_aql, bind_vars = _build_filter_aql(req)
    
    # Count total
    count_aql = f"""
        RETURN LENGTH(FOR d IN {req.collection} FILTER {filter_aql} RETURN 1)
    """
    total = list(db.aql.execute(count_aql, bind_vars=bind_vars))[0]
    if req.limit > 0:
        total = min(total, req.limit)
    
    logger.info(f"Backfill {req.collection}.{req.field}: {total:,} docs")
    
    # Drop ALL vector indexes (workaround for ArangoDB 3.12 bug)
    # ANY vector index blocks updates to docs missing ANY indexed vector field
    dropped_indexes = []
    if req.drop_index:
        dropped_indexes = _drop_all_vector_indexes(db, req.collection)
    
    start = time.time()
    processed = 0
    skipped = 0
    errors = 0
    
    try:
        while processed + skipped + errors < total:
            remaining = total - processed - skipped - errors
            fetch_size = min(req.batch_size * 10, remaining)
            
            # Fetch docs
            fetch_aql = f"""
                FOR d IN {req.collection}
                FILTER {filter_aql}
                LIMIT @limit
                RETURN {{k: d._key, t: d.{req.text_field}}}
            """
            bind_vars["limit"] = fetch_size
            docs = list(db.aql.execute(fetch_aql, bind_vars=bind_vars))
            
            if not docs:
                break
            
            # Process in batches
            for i in range(0, len(docs), req.batch_size):
                batch = docs[i:i + req.batch_size]
                texts = [d["t"] or "" for d in batch]
                keys = [d["k"] for d in batch]
                
                # Filter empty texts
                valid = [(t, k) for t, k in zip(texts, keys) if t.strip()]
                if not valid:
                    skipped += len(batch)
                    continue
                
                texts, keys = zip(*valid)
                skipped += len(batch) - len(texts)
                
                try:
                    vectors = _encode_batch(list(texts), req)
                    
                    for key, vec in zip(keys, vectors):
                        if len(vec) == req.dimension:
                            coll.update({"_key": key, req.field: vec})
                            processed += 1
                        else:
                            errors += 1
                            logger.warning(f"Wrong dimension: expected {req.dimension}, got {len(vec)}")
                except Exception as e:
                    errors += len(texts)
                    logger.error(f"Batch error: {e}")
            
            # Update status
            elapsed = time.time() - start
            _backfill_status[status_key] = BackfillStatus(
                collection=req.collection,
                field=req.field,
                total=total,
                processed=processed,
                skipped=skipped,
                errors=errors,
                elapsed_s=elapsed,
                rate_per_s=processed / elapsed if elapsed > 0 else 0,
                complete=False,
            )
    finally:
        # Recreate ALL indexes
        if dropped_indexes:
            _recreate_all_indexes(db, req.collection, dropped_indexes)
    
    elapsed = time.time() - start
    final_status = BackfillStatus(
        collection=req.collection,
        field=req.field,
        total=total,
        processed=processed,
        skipped=skipped,
        errors=errors,
        elapsed_s=elapsed,
        rate_per_s=processed / elapsed if elapsed > 0 else 0,
        complete=True,
    )
    _backfill_status[status_key] = final_status
    
    logger.info(
        f"Backfill complete: {processed:,} processed, {skipped:,} skipped, "
        f"{errors:,} errors in {elapsed:.1f}s ({processed/elapsed:.1f}/s)"
    )
    
    return final_status


@router.post("/backfill")
def backfill_embeddings(req: BackfillRequest, background_tasks: BackgroundTasks) -> dict:
    """Start an embedding backfill operation.
    
    For visual embeddings (Qwen3-VL 2048d):
        service_url: "http://127.0.0.1:8603"
        model: "Qwen/Qwen3-VL-Embedding-2B"
        dimension: 2048
        
    For text embeddings (MiniLM 384d):
        service_url: "http://127.0.0.1:8602"
        dimension: 384
    """
    from ...arango_client import get_db
    
    db = get_db()
    if not db.has_collection(req.collection):
        raise HTTPException(status_code=404, detail=f"Collection '{req.collection}' not found")
    
    # For small jobs, run synchronously
    filter_aql, bind_vars = _build_filter_aql(req)
    count_aql = f"RETURN LENGTH(FOR d IN {req.collection} FILTER {filter_aql} RETURN 1)"
    total = list(db.aql.execute(count_aql, bind_vars=bind_vars))[0]
    
    if total == 0:
        return {
            "status": "complete",
            "message": "No documents need backfill",
            "total": 0,
        }
    
    if total <= 1000:
        # Run synchronously for small jobs
        result = _run_backfill(req)
        return {
            "status": "complete",
            "total": result.total,
            "processed": result.processed,
            "skipped": result.skipped,
            "errors": result.errors,
            "elapsed_s": result.elapsed_s,
            "rate_per_s": result.rate_per_s,
        }
    
    # Run in background for large jobs
    status_key = f"{req.collection}.{req.field}"
    _backfill_status[status_key] = BackfillStatus(
        collection=req.collection,
        field=req.field,
        total=total,
        processed=0,
        skipped=0,
        errors=0,
        elapsed_s=0,
        rate_per_s=0,
        complete=False,
    )
    
    background_tasks.add_task(_run_backfill, req)
    
    return {
        "status": "started",
        "message": f"Backfill started for {total:,} documents",
        "total": total,
        "status_endpoint": f"/backfill/status/{req.collection}/{req.field}",
    }


@router.get("/backfill/status/{collection}/{field}")
def get_backfill_status(collection: str, field: str) -> dict:
    """Get status of a running or completed backfill."""
    status_key = f"{collection}.{field}"
    if status_key not in _backfill_status:
        raise HTTPException(status_code=404, detail="No backfill found for this collection/field")
    
    status = _backfill_status[status_key]
    return {
        "collection": status.collection,
        "field": status.field,
        "total": status.total,
        "processed": status.processed,
        "skipped": status.skipped,
        "errors": status.errors,
        "elapsed_s": status.elapsed_s,
        "rate_per_s": status.rate_per_s,
        "complete": status.complete,
        "progress_pct": (status.processed + status.skipped + status.errors) / status.total * 100 if status.total > 0 else 0,
    }
