FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime

# Build arg for gated model access (only needed for gated models like Jina v4)
ARG HF_TOKEN
ARG EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git && \
    rm -rf /var/lib/apt/lists/*

# Python deps — sentence-transformers covers most embedding models;
# transformers + accelerate for larger/custom models (Jina v4, etc.)
# Force upgrade transformers first (base image may have old version)
RUN pip install --no-cache-dir --upgrade \
    "transformers>=4.57,<5" \
    sentence-transformers \
    accelerate \
    einops \
    pillow \
    numpy \
    peft \
    fastapi uvicorn[standard] httpx

# flash-attn for faster inference on large models (optional)
RUN pip install --no-cache-dir flash-attn --no-build-isolation || \
    echo "[WARNING] flash-attn build failed — falling back to eager attention"

WORKDIR /app
COPY service.py /app/service.py
COPY download_model.py /app/download_model.py

# Pre-download model at build time so startup is instant
# HF_TOKEN only needed for gated models
ENV EMBEDDING_MODEL=${EMBEDDING_MODEL}
RUN --mount=type=secret,id=hf_token,env=HF_TOKEN \
    python /app/download_model.py

ENV EMBEDDING_DEVICE=cuda \
    EMBEDDING_TASK=retrieval \
    EMBEDDING_DIMENSIONS=768 \
    EMBEDDING_PORT=8602 \
    EMBEDDING_HOST=0.0.0.0

EXPOSE 8602

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD python -c "import httpx; r=httpx.get('http://127.0.0.1:8602/health',timeout=5); assert r.status_code==200"

CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8602"]
