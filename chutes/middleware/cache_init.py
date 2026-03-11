"""Response caching middleware using Redis or in-memory fallback.

No litellm dependency. Uses redis.asyncio directly for Redis caching
and a simple dict with TTL for in-memory fallback.

Migrated from CustomLogger to BaseMiddleware interface.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Dict, Optional

from loguru import logger

from scillm.proxy.middleware import BaseMiddleware


def _make_cache_key(model: str, messages: list) -> str:
    """Create a deterministic hash key from model + messages."""
    payload = json.dumps({"model": model, "messages": messages}, sort_keys=True)
    return f"scillm:cache:{hashlib.sha256(payload.encode()).hexdigest()}"


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
        if time.monotonic() - ts > self._ttl:
            self._store.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        self._store[key] = (time.monotonic(), value)

    def __len__(self) -> int:
        return len(self._store)


class CacheMiddleware(BaseMiddleware):
    """Response cache: Redis if available, in-memory fallback.

    Requires async init — call ``await middleware.initialize()`` after creation.
    """

    def __init__(self) -> None:
        self._redis: Any = None
        self._memory = _InMemoryCache()
        self._ttl = int(os.environ.get("SCILLM_CACHE_TTL_SEC", "3600"))
        self._disabled = os.environ.get(
            "SCILLM_CACHE_DISABLE", ""
        ).strip().lower() in ("1", "true", "yes")
        self._backend: str = "none"

    async def initialize(self) -> None:
        """Connect to Redis if available, otherwise use in-memory."""
        if self._disabled:
            logger.info("cache_middleware: caching disabled via SCILLM_CACHE_DISABLE")
            return

        redis_url = os.environ.get("REDIS_URL", "").strip()
        redis_host = os.environ.get("REDIS_HOST", "").strip()

        if redis_url or redis_host:
            try:
                import redis.asyncio as aioredis

                if redis_url:
                    self._redis = aioredis.from_url(
                        redis_url, socket_timeout=3, decode_responses=True,
                    )
                else:
                    port = int(os.environ.get("REDIS_PORT", "6379"))
                    db = int(os.environ.get("REDIS_DB", "0"))
                    password = os.environ.get("REDIS_PASSWORD", "").strip() or None
                    self._redis = aioredis.Redis(
                        host=redis_host, port=port, db=db,
                        password=password, socket_timeout=3,
                        decode_responses=True,
                    )

                await self._redis.ping()
                self._backend = "redis"
                logger.info(
                    "cache_middleware: Redis connected (ttl={}s)", self._ttl,
                )
                return
            except Exception as exc:
                logger.warning(
                    "cache_middleware: Redis not reachable ({}), using in-memory", exc,
                )
                self._redis = None

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
        cached = await self._cache_get(key)
        if cached is not None:
            logger.debug("cache_middleware: HIT for {}", model)
            request["_cache_hit"] = True
            request["_cached_response"] = cached
        else:
            request["_cache_key"] = key

        return request

    async def post_call(self, request: dict, response: Any) -> Any:
        if self._disabled:
            return response

        # Return cached response if we had a hit
        if request.get("_cache_hit"):
            return request.get("_cached_response", response)

        # Store response in cache
        cache_key = request.get("_cache_key")
        if cache_key and isinstance(response, dict):
            try:
                await self._cache_set(cache_key, response)
                logger.debug("cache_middleware: STORED {}", request.get("model", "?"))
            except Exception as exc:
                logger.warning("cache_middleware: failed to store: {}", exc)

        return response

    # ── Internal cache operations ────────────────────────────────────────

    async def _cache_get(self, key: str) -> Optional[Any]:
        if self._redis:
            try:
                raw = await self._redis.get(key)
                if raw:
                    return json.loads(raw)
            except Exception:
                pass
            return None
        return await self._memory.get(key)

    async def _cache_set(self, key: str, value: Any) -> None:
        if self._redis:
            try:
                await self._redis.setex(key, self._ttl, json.dumps(value))
            except Exception:
                pass
            return
        await self._memory.set(key, value, self._ttl)
