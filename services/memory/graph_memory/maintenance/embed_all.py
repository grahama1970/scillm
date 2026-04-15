"""Embed all documents in collections that are missing embeddings.

Naturally resumable: queries for docs WITHOUT embeddings, so re-running
after a crash picks up exactly where it left off.

Service-resilient: retries batches on service failure with exponential
backoff and optional Docker container restart.

Usage:
    uv run python -m graph_memory.maintenance.embed_all --dry-run
    uv run python -m graph_memory.maintenance.embed_all
    uv run python -m graph_memory.maintenance.embed_all status
    uv run python -m graph_memory.maintenance.embed_all --collection lessons --field embedding_2
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import typer
from typing import Callable, Dict, List, Optional
from tqdm import tqdm

app = typer.Typer()

# ---------------------------------------------------------------------------
# Text extractors per collection
# ---------------------------------------------------------------------------

COLLECTION_TEXT_EXTRACTORS: Dict[str, Callable[[dict], str]] = {
    # Core knowledge
    "lessons": lambda d: f"{d.get('title', '')} {d.get('problem', '')} {d.get('solution', '')}".strip(),
    "doc_chunks": lambda d: f"{d.get('title', '')} {d.get('content', '')}".strip(),
    "code_symbols": lambda d: f"{d.get('name', '')} {d.get('kind', '')} {d.get('signature', '')} {d.get('docstring', '')}".strip(),
    "agent_conversations": lambda d: f"{d.get('topic', '')} {d.get('body', '')} {d.get('summary', '')}".strip(),
    "lean_theorems": lambda d: f"{d.get('lean_code', '')} {d.get('notes', '')} {d.get('error', '')}".strip(),
    "task_states": lambda d: f"{d.get('task', '')} {d.get('status', '')} {d.get('notes', '')}".strip(),
    # SPARTA
    "sparta_qra": lambda d: f"{d.get('question', '')} {d.get('answer', '')} {d.get('reasoning', '')}".strip(),
    "sparta_url_knowledge": lambda d: f"{d.get('text', '')} {d.get('topic', '')} {d.get('name', '')} {d.get('description', '')}".strip(),
    "sparta_controls": lambda d: f"{d.get('name', '')} {d.get('description', '')}".strip(),
}

CONTAINER_NAME = os.environ.get("EMBEDDING_CONTAINER", "memory-embedding")
SERVICE_URL = os.environ.get("EMBEDDING_SERVICE_URL", "http://127.0.0.1:8602")
MAX_RETRIES = 5
RETRY_BACKOFF_S = 10.0
# Truncate texts to avoid CUDA OOM on long docs (1024 chars ≈ 250 tokens — plenty for semantic search)
MAX_TEXT_CHARS = 1024


# ---------------------------------------------------------------------------
# Service-resilient encoding
# ---------------------------------------------------------------------------


def _restart_container() -> bool:
    """Restart the Docker embedding container. Returns True if healthy."""
    print(f"\n  [embed_all] Restarting container '{CONTAINER_NAME}'...", file=sys.stderr)
    try:
        result = subprocess.run(
            ["docker", "restart", CONTAINER_NAME],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"  [embed_all] docker restart failed: {result.stderr.strip()}", file=sys.stderr)
            return False
        # Wait for healthy — Jina v4 takes ~90s to load on GPU
        for attempt in range(45):
            time.sleep(3)
            try:
                import httpx
                resp = httpx.get(f"{SERVICE_URL}/health", timeout=3.0)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "ok":
                        print(f"  [embed_all] Container healthy after {(attempt + 1) * 3}s", file=sys.stderr)
                        return True
            except Exception as exc:  # was bare
                continue
        print("  [embed_all] Container restarted but not healthy after 135s", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  [embed_all] Failed to restart container: {e}", file=sys.stderr)
        return False


def _encode_batch_resilient(texts: List[str], retries: int = MAX_RETRIES) -> List[List[float]]:
    """Encode texts via the embedding service with retry + container restart."""
    import httpx

    # Truncate long texts to prevent CUDA OOM
    texts = [t[:MAX_TEXT_CHARS] for t in texts]

    for attempt in range(retries):
        try:
            resp = httpx.post(
                f"{SERVICE_URL}/embed/batch",
                json={"texts": texts},
                timeout=120.0,
            )
            if resp.status_code == 200:
                vectors = resp.json().get("vectors", [])
                if len(vectors) == len(texts):
                    return vectors
                raise ValueError(f"Expected {len(texts)} vectors, got {len(vectors)}")
            raise ConnectionError(f"Service returned {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            remaining = retries - attempt - 1
            if remaining > 0:
                wait = RETRY_BACKOFF_S * (attempt + 1)
                print(f"\n  [embed_all] Batch failed: {e}. Retrying in {wait:.0f}s ({remaining} left)...", file=sys.stderr)
                time.sleep(wait)
                # Try restarting container on second failure
                if attempt >= 1:
                    _restart_container()
            else:
                raise RuntimeError(
                    f"Embedding service failed after {retries} attempts: {e}\n"
                    f"Container: {CONTAINER_NAME} | URL: {SERVICE_URL}\n"
                    f"Run: docker logs {CONTAINER_NAME}"
                ) from e
    return []  # unreachable


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _count_without_embedding(db, collection_name: str, field: str) -> int:
    """Count docs missing the embedding field."""
    aql = """
    RETURN LENGTH(
        FOR doc IN @@collection
            FILTER NOT HAS(doc, @field) OR doc.@field == null
            RETURN 1
    )
    """
    return list(db.aql.execute(aql, bind_vars={"@collection": collection_name, "field": field}))[0]


def _get_page_without_embedding(db, collection_name: str, field: str, page_size: int) -> List[dict]:
    """Get one page of docs missing embeddings. Returns lightweight docs (no embedding fields)."""
    # Exclude all embedding fields to avoid loading huge float arrays into memory
    aql = """
    FOR doc IN @@collection
        FILTER NOT HAS(doc, @field) OR doc.@field == null
        LIMIT @page_size
        RETURN UNSET(doc, "embedding", "embedding_2", "embedding_old")
    """
    bind = {
        "@collection": collection_name,
        "field": field,
        "page_size": page_size,
    }
    return list(db.aql.execute(aql, bind_vars=bind))


def update_doc_embedding(db, collection_name: str, doc_key: str, embedding: List[float], field: str = "embedding") -> bool:
    """Update a document with its embedding in the specified field."""
    coll = db.collection(collection_name)
    try:
        coll.update({"_key": doc_key, field: embedding})
        return True
    except Exception as e:
        print(f"  Error updating {collection_name}/{doc_key}: {e}")
        return False


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def embed(
    collection: Optional[str] = typer.Option(None, "--collection", "-c", help="Specific collection (default: all)"),
    batch_size: int = typer.Option(32, "--batch-size", "-b", help="Batch size for embedding"),
    limit: int = typer.Option(0, "--limit", "-l", help="Max docs per collection (0=all)"),
    field: str = typer.Option("embedding", "--field", "-f", help="Embedding field name (e.g. embedding_2 for migration)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be done"),
):
    """Embed all documents missing embeddings.

    Naturally resumable — queries for docs missing the embedding field,
    so re-running after a crash picks up where it left off.
    """
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(usecwd=True), override=False)

    from graph_memory.arango_client import get_db

    db = get_db()

    # Verify service is reachable before starting
    try:
        import httpx
        resp = httpx.get(f"{SERVICE_URL}/info", timeout=5.0)
        info = resp.json()
        print(f"Embedding service: {info.get('model', '?')} (dims={info.get('dimensions', '?')}, api={info.get('api', '?')})")
    except Exception as e:
        print(f"WARNING: Embedding service unreachable at {SERVICE_URL}: {e}")
        print("Will attempt Docker restart on first batch failure.")

    collections = [collection] if collection else list(COLLECTION_TEXT_EXTRACTORS.keys())

    total_embedded = 0
    total_skipped = 0
    total_errors = 0

    # Page size for AQL queries — fetch this many docs per query to avoid OOM
    page_size = batch_size * 20  # ~640 docs per page

    for coll_name in collections:
        if not db.has_collection(coll_name):
            print(f"  Collection '{coll_name}' does not exist, skipping")
            continue

        extractor = COLLECTION_TEXT_EXTRACTORS.get(coll_name)
        if not extractor:
            print(f"  No text extractor for '{coll_name}', skipping")
            continue

        need_count = _count_without_embedding(db, coll_name, field)
        if need_count == 0:
            print(f"  {coll_name}: all docs have '{field}'")
            continue

        if limit:
            need_count = min(need_count, limit)

        print(f"  {coll_name}: {need_count} docs need '{field}'")

        if dry_run:
            sample = _get_page_without_embedding(db, coll_name, field, 3)
            for doc in sample:
                text = extractor(doc)
                print(f"    Would embed: {text[:80]}...")
            if need_count > 3:
                print(f"    ... and {need_count - 3} more")
            continue

        embedded = 0
        skipped = 0
        errors = 0
        pbar = tqdm(total=need_count, desc=f"  {coll_name}")

        while embedded + skipped + errors < need_count:
            # Fetch a page of docs missing embeddings (lightweight — no embedding arrays)
            remaining = need_count - embedded - skipped - errors
            fetch_size = min(page_size, remaining)
            docs = _get_page_without_embedding(db, coll_name, field, fetch_size)
            if not docs:
                break  # No more docs to process

            # Process in batches
            for i in range(0, len(docs), batch_size):
                batch = docs[i:i + batch_size]
                texts = []
                valid_docs = []

                for doc in batch:
                    text = extractor(doc)
                    if text.strip():
                        texts.append(text)
                        valid_docs.append(doc)
                    else:
                        skipped += 1
                        pbar.update(1)

                if not texts:
                    continue

                try:
                    embeddings = _encode_batch_resilient(texts)

                    for doc, emb in zip(valid_docs, embeddings):
                        if update_doc_embedding(db, coll_name, doc["_key"], emb, field=field):
                            embedded += 1
                        else:
                            errors += 1
                        pbar.update(1)
                except Exception as e:
                    print(f"\n  FATAL batch error in {coll_name}: {e}")
                    errors += len(texts)
                    pbar.update(len(texts))
                    break
            else:
                continue
            break  # Break outer while if inner for broke

        pbar.close()
        print(f"  Done: {embedded} embedded, {skipped} skipped, {errors} errors")
        total_embedded += embedded
        total_skipped += skipped
        total_errors += errors

    print(f"\n{'DRY RUN - ' if dry_run else ''}Total: {total_embedded} embedded, {total_skipped} skipped, {total_errors} errors")
    if total_errors > 0:
        print("Re-run to retry failed documents (naturally resumable).")


@app.command()
def status(
    field: str = typer.Option("embedding", "--field", "-f", help="Embedding field to check"),
):
    """Show embedding coverage for all collections."""
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(usecwd=True), override=False)

    from graph_memory.arango_client import get_db

    db = get_db()

    # Check service
    try:
        import httpx
        resp = httpx.get(f"{SERVICE_URL}/info", timeout=3.0)
        info = resp.json()
        print(f"Service: {info.get('model', '?')} | dims={info.get('dimensions', '?')} | api={info.get('api', '?')} | {info.get('status', '?')}")
    except Exception as exc:
        print(f"Service: UNREACHABLE at {SERVICE_URL}: {exc}")
    print()

    print(f"Embedding Coverage (field: '{field}'):")
    print("-" * 65)

    total_docs = 0
    total_with = 0

    for coll_name in COLLECTION_TEXT_EXTRACTORS.keys():
        if not db.has_collection(coll_name):
            print(f"  {coll_name:25} NOT EXISTS")
            continue

        coll = db.collection(coll_name)
        count = coll.count()
        total_docs += count

        aql = """
        RETURN LENGTH(
            FOR doc IN @@collection
                FILTER doc.@field != null AND HAS(doc, @field)
                RETURN 1
        )
        """
        with_emb = list(db.aql.execute(aql, bind_vars={"@collection": coll_name, "field": field}))[0]
        total_with += with_emb

        pct = (with_emb / count * 100) if count > 0 else 0
        icon = "OK" if pct == 100 else "PARTIAL" if pct > 0 else "NONE"
        print(f"  {coll_name:25} {with_emb:6}/{count:<6} ({pct:5.1f}%) {icon}")

    print("-" * 65)
    total_pct = (total_with / total_docs * 100) if total_docs > 0 else 0
    print(f"  {'TOTAL':25} {total_with:6}/{total_docs:<6} ({total_pct:5.1f}%)")

    # Check vector indexes
    print(f"\nVector Indexes:")
    print("-" * 65)
    for coll_name in COLLECTION_TEXT_EXTRACTORS.keys():
        if not db.has_collection(coll_name):
            continue
        coll = db.collection(coll_name)
        indexes = coll.indexes()
        vec_indexes = [idx for idx in indexes if idx.get("type") == "vector"]
        if vec_indexes:
            for idx in vec_indexes:
                fields = idx.get("fields", [])
                params = idx.get("params", {})
                dim = params.get("dimension", "?")
                metric = params.get("metric", "?")
                print(f"  {coll_name:25} {','.join(fields):15} dim={dim} metric={metric}")
        else:
            print(f"  {coll_name:25} NO VECTOR INDEX")


@app.command()
def verify():
    """Verify embedding service is working and vectors are queryable."""
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(usecwd=True), override=False)

    import httpx
    from graph_memory.arango_client import get_db

    db = get_db()
    errors = []

    # 1. Service health
    print("1. Service health...")
    try:
        resp = httpx.get(f"{SERVICE_URL}/info", timeout=5.0)
        info = resp.json()
        print(f"   OK: {info.get('model')} (dims={info.get('dimensions')}, api={info.get('api')})")
    except Exception as e:
        errors.append(f"Service unreachable: {e}")
        print(f"   FAIL: {e}")

    # 2. Encode a test text
    print("2. Test encoding...")
    try:
        resp = httpx.post(f"{SERVICE_URL}/embed", json={"text": "test query"}, timeout=10.0)
        vec = resp.json().get("embedding") or resp.json()["vector"]
        dims = len(vec)
        print(f"   OK: got {dims}-dim vector")
    except Exception as e:
        errors.append(f"Encoding failed: {e}")
        print(f"   FAIL: {e}")
        dims = 0

    # 3. Check each collection with vector index
    print("3. Vector index queries...")
    for coll_name in COLLECTION_TEXT_EXTRACTORS.keys():
        if not db.has_collection(coll_name):
            continue
        coll = db.collection(coll_name)
        indexes = coll.indexes()
        vec_indexes = [idx for idx in indexes if idx.get("type") == "vector"]
        if not vec_indexes:
            continue

        idx = vec_indexes[0]
        idx_dim = idx.get("params", {}).get("dimension", 0)
        idx_field = idx.get("fields", ["embedding"])[0]

        if dims > 0 and dims != idx_dim:
            print(f"   WARN: {coll_name} index expects {idx_dim} dims, service produces {dims}")

        # Try a vector search
        try:
            # Use a zero vector of the right dimension for testing
            test_vec = [0.0] * idx_dim
            aql = f"""
            FOR doc IN {coll_name}
                LET score = COSINE_SIMILARITY(doc.{idx_field}, @vec)
                SORT score DESC
                LIMIT 1
                RETURN {{_key: doc._key, score: score}}
            """
            results = list(db.aql.execute(aql, bind_vars={"vec": test_vec}))
            print(f"   OK: {coll_name} vector search returned {len(results)} result(s)")
        except Exception as e:
            errors.append(f"{coll_name} vector search failed: {e}")
            print(f"   FAIL: {coll_name}: {e}")

    if errors:
        print(f"\n{len(errors)} error(s) found:")
        for err in errors:
            print(f"  - {err}")
        raise typer.Exit(1)
    else:
        print("\nAll checks passed.")


if __name__ == "__main__":
    app()
