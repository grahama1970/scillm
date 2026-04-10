"""Bifrost gateway helper for routing requests through the local deployment."""

from __future__ import annotations

import os
from typing import Any, AsyncIterator, Dict

import httpx
from loguru import logger

BIFROST_URL = os.environ.get("BIFROST_URL", "http://127.0.0.1:4002").rstrip("/")
BIFROST_ENABLED = os.environ.get("BIFROST_ENABLED", "").lower() in ("true", "1", "yes")
BIFROST_CLIENT_KEY = os.environ.get("BIFROST_CLIENT_KEY") or os.environ.get("SCILLM_MASTER_KEY", "sk-bifrost-proxy")

# Map scillm model patterns to Bifrost provider prefixes
_PROVIDER_MAP = [
    # Exact matches first
    ("deepseek-chat", "deepseek"),
    ("deepseek-coder", "deepseek"),
    ("deepseek-reasoner", "deepseek"),
    # Pattern matches
    ("claude-", "anthropic"),
    ("gpt-", "openai"),
    ("codex", "openai"),
    ("gemini", "gemini"),
    ("glm-", "glm"),
    ("kimi-", "moonshot"),
    ("moonshot", "moonshot"),
    ("qwen", "ollama"),
    ("ollama", "ollama"),
    # Chutes patterns (Org/Model format)
    ("deepseek-ai/", "chutes"),
    ("qwen/", "chutes"),
    ("meta-llama/", "chutes"),
]


def _to_bifrost_model(model: str) -> str:
    """Convert a model name to ``provider/model`` format for Bifrost."""
    if not model:
        return model

    model_lower = model.lower()

    if "/" in model and any(model_lower.startswith(prefix) for prefix in ("anthropic/", "openai/", "gemini/", "chutes/", "ollama/")):
        return model

    for pattern, provider in _PROVIDER_MAP:
        if pattern in model_lower or model.startswith(pattern):
            if provider == "chutes":
                return f"chutes/{model}"
            return f"{provider}/{model}"

    if ":" in model:
        return f"ollama/{model}"

    logger.warning("bifrost_forwarder: unknown model format %s, sending passthrough", model)
    return model


class BifrostForwarder:
    """Utility wrapper around the local Bifrost deployment."""

    def __init__(self) -> None:
        self._base_url = BIFROST_URL
        self._enabled = BIFROST_ENABLED
        self._client_key = (BIFROST_CLIENT_KEY or "bifrost-dev").strip()
        self._client: httpx.AsyncClient | None = None
        if self._enabled:
            logger.info("Bifrost gateway enabled at %s", self.api_base)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def api_base(self) -> str:
        return f"{self._base_url}/v1"

    @property
    def client_key(self) -> str:
        return self._client_key or "bifrost-dev"

    def format_model(self, model: str) -> str:
        return _to_bifrost_model(model)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(120.0, connect=10.0),
                headers=self._auth_headers,
            )
        return self._client

    @property
    def _auth_headers(self) -> Dict[str, str]:
        if not self._client_key:
            return {"Content-Type": "application/json"}
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._client_key}",
        }

    async def forward_to_bifrost(self, request: dict, *, stream: bool = False) -> Any:
        """Forward a chat completion request directly to Bifrost."""

        if not self._enabled:
            raise RuntimeError("Bifrost forwarding requested but BIFROST_ENABLED is false")

        original_model = request.get("model")
        body = {k: v for k, v in request.items() if not k.startswith("_")}
        body["model"] = _to_bifrost_model(body.get("model", ""))
        body["stream"] = bool(stream)

        client = self._get_client()

        if stream:
            async def _stream() -> AsyncIterator[bytes]:
                async with client.stream("POST", "/v1/chat/completions", json=body) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_raw():
                        if chunk:
                            yield chunk

            return _stream()

        resp = await client.post("/v1/chat/completions", json=body)
        resp.raise_for_status()
        data = resp.json()
        if original_model:
            data["model"] = original_model
        return data


_forwarder: BifrostForwarder | None = None


def get_bifrost_forwarder() -> BifrostForwarder:
    global _forwarder
    if _forwarder is None:
        _forwarder = BifrostForwarder()
    return _forwarder
