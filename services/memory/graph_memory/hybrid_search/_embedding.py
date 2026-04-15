"""Query embedding via the embedding HTTP service."""
from typing import List, Optional
from loguru import logger
import os


def get_query_embedding(text: str) -> Optional[List[float]]:
    """Generate embedding for query text via the embedding HTTP service (port 8602).

    Uses a 2s connect timeout so callers degrade to BM25-only search
    instead of hanging for 120s when the embedding service is down.
    """
    import httpx

    embedding_url = os.getenv("EMBEDDING_SERVICE_URL") or os.getenv("EMBEDDING_URL", "http://localhost:8602")
    try:
        resp = httpx.post(
            f"{embedding_url}/embed",
            json={"text": text},
            timeout=httpx.Timeout(10.0, connect=2.0),
        )
        resp.raise_for_status()
        data = resp.json()
        # Log cold start so callers know latency is expected
        if data.get("cold"):
            server_ms = data.get("latency_ms", "?")
            logger.info(f"Embedding service cold start (server encode: {server_ms}ms) — expect slower first queries")
        # Single text endpoint returns vector directly
        vec = data.get("embedding") or data.get("vector")
        if vec:
            return vec
        # Fallback: check if it returned a list
        vectors = data.get("vectors") or data.get("embeddings")
        if vectors and len(vectors) > 0:
            return vectors[0]
        return None
    except (httpx.ConnectError, httpx.ConnectTimeout):
        logger.warning("Embedding service unreachable — falling back to BM25-only search")
        return None
    except Exception as e:
        logger.warning(f"Embedding request failed — falling back to BM25-only: {e}")
        return None
