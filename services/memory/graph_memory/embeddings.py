from __future__ import annotations

from loguru import logger

try:
    from dotenv import load_dotenv, find_dotenv  # type: ignore
    load_dotenv(find_dotenv(usecwd=True), override=False)
except Exception as exc:
    logger.error("dotenv loading skipped: {}", exc)

import math
import os
from typing import Iterable, List, Optional

_MODEL = None


def _is_valid(vec: List[float]) -> bool:
    if not vec:
        return False
    for x in vec:
        try:
            xf = float(x)
        except Exception as exc:
            logger.error("non-numeric embedding value: {}", exc)
            return False
        if math.isnan(xf) or math.isinf(xf):
            return False
    return True


def load_embedder(model_name: Optional[str] = None):
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    name = model_name or os.getenv("SPARTA_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
    # Lazy import to avoid hard deps when unused
    from sentence_transformers import SentenceTransformer  # type: ignore
    device = "cuda" if os.getenv("CUDA_VISIBLE_DEVICES", "") != "" else (os.getenv("SPARTA_EMBED_DEVICE", "cpu"))
    try:
        _MODEL = SentenceTransformer(name, device=device)
    except Exception as e:
        # Common failure: HF fast transfer enabled but package missing; retry with it disabled
        if os.getenv("HF_HUB_ENABLE_HF_TRANSFER", "").strip() == "1":
            os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
            _MODEL = SentenceTransformer(name, device=device)
        else:
            raise
    return _MODEL


def _encode_via_service(texts: List[str], service_url: str) -> List[List[float]]:
    """Encode texts via external embedding service."""
    from graph_memory.http_clients import get_session
    try:
        resp = get_session().post(
            f"{service_url}/embed/batch",
            json={"texts": texts},
            timeout=60
        )
        if resp.status_code == 200:
            return resp.json()["vectors"]
    except Exception as exc:
        logger.error("embedding service request failed: {}", exc)
    return []


def encode_texts(texts: Iterable[str], *, batch_size: int = 64, normalize: bool = True, model_name: Optional[str] = None) -> List[List[float]]:
    ts = [str(t) for t in texts]
    if not ts:
        return []
    
    # Try external service first if URL is set
    service_url = os.getenv("EMBEDDING_SERVICE_URL")
    if service_url:
        result = _encode_via_service(ts, service_url)
        if result and len(result) == len(ts):
            return result
        # Fall through to local if service failed
    
    # Local model fallback
    model = load_embedder(model_name)
    embs = model.encode(ts, batch_size=batch_size, show_progress_bar=False, normalize_embeddings=normalize)
    out: List[List[float]] = [list(map(float, v)) for v in embs]
    # Basic sanitization
    if not all(_is_valid(v) for v in out):
        raise ValueError("invalid embeddings produced (NaN/Inf)")
    return out
