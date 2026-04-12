"""Response caching middleware using ArangoDB or in-memory fallback.

Uses the memory daemon (:8601) for persistent caching with TTL,
falling back to in-memory dict when ArangoDB is unavailable.

Also provides request deduplication: if identical requests arrive
while one is in-flight, they share the same response.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from typing import Any, Dict, Optional

import httpx
from loguru import logger

from scillm.proxy.middleware import BaseMiddleware

_MEMORY_URL = os.environ.get("MEMORY_URL", "http://127.0.0.1:8601")
_COLLECTION = "scillm_response_cache"


def _make_cache_key(model: str, messages: list) -> str:
    """Create a deterministic hash key from model + messages."""
    payload = json.dumps({"model": model, "messages": messages}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


class _InMemoryCache:
    """Simple TTL dict cache."""

    def __init__(self, ttl: int = 3600) -> None:
        self._store: Dict[str, tuple[float, Any]] = {}
        self._ttl = ttl

    async def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.time() - ts > self._ttl:
            self._store.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        self._store[key] = (time.time(), value)

    def __len__(self) -> int:
        return len(self._store)


# Request deduplication: in-flight requests by cache key
_inflight: Dict[str, asyncio.Future] = {}


class CacheMiddleware(BaseMiddleware):
    """Response cache: ArangoDB if available, in-memory fallback.

    Also deduplicates identical in-flight requests.
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._memory = _InMemoryCache()
        self._ttl = int(os.environ.get("SCILLM_CACHE_TTL_SEC", "3600"))
        self._disabled = os.environ.get(
            "SCILLM_CACHE_DISABLE", ""
        ).strip().lower() in ("1", "true", "yes")
        self._backend: str = "none"
        self._arango_available: bool = False

    async def initialize(self) -> None:
        """Connect to ArangoDB memory daemon, fallback to in-memory."""
        if self._disabled:
            logger.info("cache_middleware: caching disabled via SCILLM_CACHE_DISABLE")
            return

        self._client = httpx.AsyncClient(
            base_url=_MEMORY_URL,
            timeout=httpx.Timeout(5.0, connect=2.0),
        )

        # Test ArangoDB connection
        try:
            resp = await self._client.get("/health")
            if resp.status_code == 200:
                self._arango_available = True
                self._backend = "arangodb"
                logger.info(
                    "cache_middleware: ArangoDB connected (collection={}, ttl={}s)",
                    _COLLECTION, self._ttl,
                )
                return
        except Exception as exc:
            logger.warning("cache_middleware: ArangoDB not reachable ({})", exc)

        self._backend = "memory"
        logger.info("cache_middleware: in-memory cache (ttl={}s)", self._ttl)

    async def pre_call(self, request: dict) -> dict | None:
        if self._disabled:
            return request

        model = request.get("model", "")
        messages = request.get("messages", [])
        if not model or not messages:
            return request

        # Skip cache for streaming requests
        if request.get("stream", False):
            return request

        key = _make_cache_key(model, messages)

        # Check for in-flight duplicate request (deduplication)
        if key in _inflight:
            logger.debug("cache_middleware: DEDUPE waiting for in-flight {}", model)
            request["_cache_dedupe"] = True
            request["_cache_key"] = key
            return request

        # Check cache
        cached = await self._cache_get(key)
        if cached is not None:
            logger.debug("cache_middleware: HIT for {}", model)
            request["_cache_hit"] = True
            request["_cached_response"] = cached
            return request

        # Mark as in-flight for deduplication
        _inflight[key] = asyncio.get_event_loop().create_future()
        request["_cache_key"] = key
        request["_cache_owner"] = True  # This request owns the in-flight slot

        return request

    async def post_call(self, request: dict, response: Any) -> Any:
        if self._disabled:
            return response

        cache_key = request.get("_cache_key")

        # If this was a dedupe request, wait for the owner's response
        if request.get("_cache_dedupe") and cache_key in _inflight:
            try:
                response = await asyncio.wait_for(_inflight[cache_key], timeout=120.0)
                logger.debug("cache_middleware: DEDUPE got response for {}", request.get("model", "?"))
                return response
            except asyncio.TimeoutError:
                logger.warning("cache_middleware: DEDUPE timeout for {}", cache_key)
                return response

        # Return cached response if we had a hit
        if request.get("_cache_hit"):
            return request.get("_cached_response", response)

        # Store response in cache and resolve dedupes
        if cache_key and isinstance(response, dict):
            try:
                await self._cache_set(cache_key, response)
                logger.debug("cache_middleware: STORED {}", request.get("model", "?"))
            except Exception as exc:
                logger.warning("cache_middleware: failed to store: {}", exc)

            # Resolve waiting dedupe requests
            if request.get("_cache_owner") and cache_key in _inflight:
                future = _inflight.pop(cache_key, None)
                if future and not future.done():
                    future.set_result(response)

        return response

    async def on_error(self, request: dict, error: Exception) -> None:
        """Clean up in-flight slot on error."""
        cache_key = request.get("_cache_key")
        if request.get("_cache_owner") and cache_key in _inflight:
            future = _inflight.pop(cache_key, None)
            if future and not future.done():
                future.set_exception(error)

    # ── Internal cache operations ────────────────────────────────────────

    async def _cache_get(self, key: str) -> Optional[Any]:
        if self._arango_available and self._client:
            try:
                resp = await self._client.post(
                    "/query",
                    json={
                        "query": f"FOR doc IN {_COLLECTION} FILTER doc._key == @key AND doc.expires_at > @now RETURN doc.response",
                        "bindVars": {"key": key, "now": time.time()},
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    results = data.get("result", [])
                    if results:
                        return results[0]
            except Exception as exc:
                logger.debug("cache_middleware: ArangoDB get error: {}", exc)
            return None
        return await self._memory.get(key)

    async def _cache_set(self, key: str, value: Any) -> None:
        if self._arango_available and self._client:
            try:
                doc = {
                    "_key": key,
                    "response": value,
                    "created_at": time.time(),
                    "expires_at": time.time() + self._ttl,
                }
                await self._client.post(
                    "/upsert",
                    json={"collection": _COLLECTION, "documents": [doc]},
                )
            except Exception as exc:
                logger.debug("cache_middleware: ArangoDB set error: {}", exc)
            return
        await self._memory.set(key, value, self._ttl)
