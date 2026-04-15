FROM python:3.13-slim

WORKDIR /app

# System deps for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency resolution
RUN pip install --no-cache-dir uv

# Copy dependency files first (layer cache)
COPY pyproject.toml uv.lock ./

# Install dependencies (frozen from lockfile, no dev deps)
RUN uv sync --frozen --no-dev

# Download spaCy model + NLTK data (spaCy/nltk installed by uv sync)
RUN uv pip install pip && \
    uv run python -m spacy download en_core_web_sm && \
    uv run python -c "import nltk; nltk.download('punkt_tab', quiet=True); nltk.download('wordnet', quiet=True); nltk.download('omw-1.4', quiet=True)"

# Copy source code
COPY graph_memory/ ./graph_memory/

# Set PYTHONPATH so graph_memory is importable
ENV PYTHONPATH=/app

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -f http://localhost:8601/health || exit 1

EXPOSE 8601

CMD ["uv", "run", "uvicorn", "graph_memory.service.app:app", "--host", "0.0.0.0", "--port", "8601", "--workers", "8"]
