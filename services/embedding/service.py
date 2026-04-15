"""Embedding service — persistent FastAPI server for semantic search.

Model-agnostic: works with sentence-transformers, Jina, or any HuggingFace model.
Set EMBEDDING_MODEL env var to switch models without code changes.

Supported model APIs (auto-detected):
    - sentence-transformers: model.encode(texts) -> numpy array
    - Jina v4+: model.encode_text(texts=...) / model.encode_image(images=...)
    - Raw transformers: tokenizer + model forward pass + mean pooling

Endpoints:
    POST /embed       {text: str} | {image: str}  -> {embedding: [...], model: str, dimensions: int}
    POST /embed/batch {texts: [str], images: [str]} -> {embeddings: [[...]], model: str, count: int, dimensions: int}
    GET  /info                                     -> {model: str, device: str, dimensions: int, status: str}
    GET  /health                                   -> {status: "ok"}
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import asynccontextmanager
from enum import Enum
from typing import List, Optional

import numpy as np
import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

_model = None
_tokenizer = None
_model_name: str | None = None
_device: str | None = None
_default_task: str = "retrieval"
_default_dims: int = 0  # 0 = use model's native dims
_ready_at: float = 0.0
_request_count: int = 0
_COLD_WINDOW_S = 60.0
_COLD_MIN_REQUESTS = 3


class ModelAPI(Enum):
    SENTENCE_TRANSFORMERS = "sentence_transformers"
    JINA = "jina"
    RAW_TRANSFORMERS = "raw_transformers"


_model_api: ModelAPI = ModelAPI.RAW_TRANSFORMERS


def _detect_api(model) -> ModelAPI:
    """Detect which encoding API the model supports."""
    if hasattr(model, "encode_text") and hasattr(model, "encode_image"):
        return ModelAPI.JINA
    if hasattr(model, "encode") and callable(getattr(model, "encode", None)):
        # sentence-transformers SentenceTransformer has .encode()
        # but so do some raw transformers — check for typical ST signature
        import inspect
        sig = inspect.signature(model.encode)
        params = list(sig.parameters.keys())
        if "sentences" in params or "show_progress_bar" in params:
            return ModelAPI.SENTENCE_TRANSFORMERS
        # If .encode exists and accepts a list, treat as sentence-transformers-like
        return ModelAPI.SENTENCE_TRANSFORMERS
    return ModelAPI.RAW_TRANSFORMERS


def load_model():
    global _model, _tokenizer, _model_name, _device, _default_task, _default_dims
    global _ready_at, _model_api

    if _model is not None:
        return _model

    model_name = os.environ.get("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    device = os.environ.get("EMBEDDING_DEVICE", "cuda" if torch.cuda.is_available() else "cpu")
    _default_task = os.environ.get("EMBEDDING_TASK", "retrieval")
    _default_dims = int(os.environ.get("EMBEDDING_DIMENSIONS", "0"))

    print(f"[embedding] Loading model: {model_name}...", file=sys.stderr)
    start = time.time()

    # Try sentence-transformers first (most common for embedding models)
    try:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(model_name, device=device, trust_remote_code=True)
        _model_api = ModelAPI.SENTENCE_TRANSFORMERS
        if _default_dims == 0:
            _default_dims = _model.get_sentence_embedding_dimension() or 384
        print(f"[embedding] Loaded via sentence-transformers (dims={_default_dims})", file=sys.stderr)
    except Exception as st_err:
        print(f"[embedding] sentence-transformers failed: {st_err}, trying transformers...", file=sys.stderr)

        # Fall back to raw transformers
        from transformers import AutoModel, AutoTokenizer

        _model = AutoModel.from_pretrained(
            model_name,
            trust_remote_code=True,
            torch_dtype=torch.float16,
        )
        _model = _model.to(device)
        _model.eval()

        _model_api = _detect_api(_model)

        if _model_api == ModelAPI.RAW_TRANSFORMERS:
            _tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)

        # Infer default dims if not set
        if _default_dims == 0:
            if hasattr(_model, "config"):
                _default_dims = getattr(_model.config, "hidden_size", 768)
            else:
                _default_dims = 768

        print(f"[embedding] Loaded via transformers (api={_model_api.value}, dims={_default_dims})", file=sys.stderr)

    _model_name = model_name
    _device = device
    elapsed = time.time() - start
    _ready_at = time.time()
    print(f"[embedding] Model ready in {elapsed:.1f}s (device: {device}, api: {_model_api.value})", file=sys.stderr)
    return _model


def _to_vectors(output) -> list[list[float]]:
    """Normalize any model output to list of list[float]."""
    if isinstance(output, np.ndarray):
        return output.astype(float).tolist()
    if isinstance(output, torch.Tensor):
        return output.cpu().float().numpy().tolist()
    if isinstance(output, list):
        if output and isinstance(output[0], (list, np.ndarray, torch.Tensor)):
            return [_to_vectors(v)[0] if isinstance(v, (np.ndarray, torch.Tensor)) else v for v in output]
        return [output]  # single vector
    return output


def _encode_texts(texts: list[str], task: str | None = None) -> list[list[float]]:
    """Encode texts using whatever API the model supports."""
    model = load_model()

    # Free fragmented VRAM before each batch
    if _device and _device != "cpu":
        torch.cuda.empty_cache()

    if _model_api == ModelAPI.SENTENCE_TRANSFORMERS:
        kwargs = {}
        effective_task = task or _default_task
        if effective_task:
            # Jina v4 custom ST module accepts `task` as a direct kwarg
            # Standard ST models accept `prompt_name`
            # Try task first (Jina v4), fall back to prompt_name (standard ST)
            kwargs["task"] = effective_task
        result = model.encode(texts, **kwargs)
        return _to_vectors(result)

    elif _model_api == ModelAPI.JINA:
        with torch.no_grad():
            kwargs = {"texts": texts, "task": task or _default_task}
            # Jina v4 supports max_length
            kwargs["max_length"] = 8192
            result = model.encode_text(**kwargs)
        return _to_vectors(result)

    else:  # RAW_TRANSFORMERS
        with torch.no_grad():
            inputs = _tokenizer(texts, padding=True, truncation=True, max_length=512, return_tensors="pt")
            inputs = {k: v.to(_device) for k, v in inputs.items()}
            output = _model(**inputs)
            # Mean pooling over token embeddings
            token_emb = output.last_hidden_state
            mask = inputs["attention_mask"].unsqueeze(-1).expand(token_emb.size()).float()
            sum_emb = torch.sum(token_emb * mask, dim=1)
            count = torch.clamp(mask.sum(dim=1), min=1e-9)
            vectors = (sum_emb / count).cpu().float().numpy().tolist()
        return vectors


def _encode_images(images: list[str], task: str | None = None) -> list[list[float]]:
    """Encode images. Only supported by multimodal models (Jina v4, CLIP, etc.)."""
    model = load_model()

    if _model_api == ModelAPI.JINA:
        with torch.no_grad():
            result = model.encode_image(images=images, task=task or _default_task)
        return _to_vectors(result)

    elif _model_api == ModelAPI.SENTENCE_TRANSFORMERS:
        # Some sentence-transformers models (CLIP) support images via .encode()
        from PIL import Image
        import httpx

        pil_images = []
        for img in images:
            if img.startswith(("http://", "https://")):
                resp = httpx.get(img, timeout=30)
                from io import BytesIO
                pil_images.append(Image.open(BytesIO(resp.content)))
            else:
                pil_images.append(Image.open(img))

        result = model.encode(pil_images)
        return _to_vectors(result)

    else:
        raise HTTPException(status_code=400, detail=f"Model {_model_name} does not support image encoding")


def _truncate_dims(vectors: list, dims: int) -> list:
    """Truncate vectors to requested dimensions (Matryoshka)."""
    if dims and vectors and dims < len(vectors[0]):
        return [v[:dims] for v in vectors]
    return vectors


@asynccontextmanager
async def lifespan(app):
    load_model()
    port = os.environ.get("EMBEDDING_PORT", "8602")
    print(f"[embedding] Service ready on http://0.0.0.0:{port}", file=sys.stderr)
    yield
    print("[embedding] Shutting down...", file=sys.stderr)


app = FastAPI(title="Embedding Service", version="3.0.0", lifespan=lifespan)


def _is_cold() -> bool:
    """True if service is still warming up."""
    if _ready_at == 0.0:
        return True
    elapsed = time.time() - _ready_at
    return elapsed < _COLD_WINDOW_S or _request_count < _COLD_MIN_REQUESTS


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class EmbedRequest(BaseModel):
    text: Optional[str] = None
    image: Optional[str] = None
    task: Optional[str] = None
    dimensions: Optional[int] = None


class EmbedBatchRequest(BaseModel):
    texts: Optional[List[str]] = None
    images: Optional[List[str]] = None
    task: Optional[str] = None
    dimensions: Optional[int] = None


# ---------------------------------------------------------------------------
# POST /embed — single text or image
# ---------------------------------------------------------------------------

@app.post("/embed")
async def embed_endpoint(req: EmbedRequest):
    global _request_count

    if req.text is None and req.image is None:
        raise HTTPException(status_code=422, detail="Provide 'text' or 'image'")

    cold = _is_cold()
    task = req.task or _default_task
    dims = req.dimensions or _default_dims
    t0 = time.time()

    if req.text is not None:
        vectors = _encode_texts([req.text], task=task)
    else:
        vectors = _encode_images([req.image], task=task)

    latency_ms = (time.time() - t0) * 1000
    _request_count += 1
    vectors = _truncate_dims(vectors, dims)
    vector = vectors[0]

    return {
        "embedding": vector,
        "model": _model_name,
        "dimensions": len(vector),
        "latency_ms": round(latency_ms, 1),
        "cold": cold,
    }


# ---------------------------------------------------------------------------
# POST /embed/batch — multiple texts and/or images
# ---------------------------------------------------------------------------

@app.post("/embed/batch")
async def embed_batch_endpoint(req: EmbedBatchRequest):
    global _request_count

    if not req.texts and not req.images:
        raise HTTPException(status_code=422, detail="Provide 'texts' and/or 'images'")

    cold = _is_cold()
    task = req.task or _default_task
    dims = req.dimensions or _default_dims
    t0 = time.time()

    all_vectors: list = []

    if req.texts:
        all_vectors.extend(_encode_texts(req.texts, task=task))

    if req.images:
        all_vectors.extend(_encode_images(req.images, task=task))

    latency_ms = (time.time() - t0) * 1000
    _request_count += 1
    all_vectors = _truncate_dims(all_vectors, dims)

    return {
        "embeddings": all_vectors,
        "model": _model_name,
        "count": len(all_vectors),
        "dimensions": len(all_vectors[0]) if all_vectors else 0,
        "latency_ms": round(latency_ms, 1),
        "cold": cold,
    }


# ---------------------------------------------------------------------------
# GET /info, GET /health
# ---------------------------------------------------------------------------

@app.get("/info")
async def info_endpoint():
    load_model()
    cold = _is_cold()
    uptime_s = round(time.time() - _ready_at, 1) if _ready_at else 0.0
    supports_images = _model_api in (ModelAPI.JINA,)
    # Also check if sentence-transformers model has image support
    if _model_api == ModelAPI.SENTENCE_TRANSFORMERS:
        try:
            supports_images = hasattr(_model, "encode") and "clip" in (_model_name or "").lower()
        except Exception:
            pass

    return {
        "model": _model_name,
        "device": _device,
        "dimensions": _default_dims,
        "task": _default_task,
        "api": _model_api.value,
        "supports_images": supports_images,
        "status": "warming" if cold else "ready",
        "cold": cold,
        "uptime_s": uptime_s,
        "requests_served": _request_count,
    }


@app.get("/health")
async def health():
    cold = _is_cold()
    return {
        "status": "warming" if cold else "ok",
        "cold": cold,
        "requests_served": _request_count,
    }
