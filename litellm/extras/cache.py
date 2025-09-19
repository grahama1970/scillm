"""
Tiny helper to configure LiteLLM Redis cache with one call.

Usage:
  from litellm.extras.cache import configure_cache_redis
  configure_cache_redis(host="localhost", port=6379, password=None, ttl=172800)

This is optional; by default LiteLLM runs without cache.
"""
from __future__ import annotations

from typing import Iterable, Optional

import litellm


def configure_cache_redis(
    *,
    host: str = "localhost",
    port: int = 6379,
    password: Optional[str] = None,
    ttl: int = 60 * 60 * 24 * 2,
    supported_call_types: Optional[Iterable[str]] = ("acompletion", "completion"),
) -> None:
    """Configure LiteLLM global cache with Redis.

    This does not open a connection immediately; it wires the cache object and enables caching.
    """
    from litellm.caching.caching import LiteLLMCache, LiteLLMCacheType

    litellm.cache = LiteLLMCache(
        type=LiteLLMCacheType.REDIS,
        host=host,
        port=str(port),
        password=password,
        ttl=ttl,
        supported_call_types=list(supported_call_types or ()),
    )
    litellm.enable_cache()

