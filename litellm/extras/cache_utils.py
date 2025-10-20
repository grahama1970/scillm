"""Helpers for initialising LiteLLM's cache subsystem."""
from __future__ import annotations

import logging
import os
from typing import Dict, Optional, Tuple

import litellm
from dotenv import load_dotenv

try:
    import redis
except ImportError:  # pragma: no cover - optional dependency
    redis = None  # type: ignore

from litellm.caching.caching import Cache as LiteLLMCache, LiteLLMCacheType

from .log_utils import truncate_large_value

__all__ = [
    "initialize_litellm_cache",
    "test_litellm_cache",
]

logger = logging.getLogger(__name__)
load_dotenv()


def _truthy(val: Optional[str]) -> bool:
    return (val or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def initialize_litellm_cache() -> None:
    """Configure LiteLLM's cache using Redis if available; fallback to memory.

    Honours env:
      - REDIS_URL or REDIS_HOST/REDIS_PORT[/REDIS_DB]/REDIS_PASSWORD
      - SCILLM_CACHE_NAMESPACE (namespacing)
      - SCILLM_CACHE_TTL_SEC (TTL override)
      - LITELLM_DISABLE_CACHE / SCILLM_CACHE_DISABLE (disable)
    """

    if _truthy(os.getenv("LITELLM_DISABLE_CACHE")) or _truthy(os.getenv("SCILLM_CACHE_DISABLE")):
        try:
            if hasattr(litellm, "disable_cache"):
                litellm.disable_cache()  # type: ignore[attr-defined]
            setattr(litellm, "cache", None)
        except Exception:  # pragma: no cover - best effort
            pass
        logger.info("LiteLLM caching disabled via LITELLM_DISABLE_CACHE")
        return

    ttl_env = os.getenv("SCILLM_CACHE_TTL_SEC")
    ttl: Optional[float] = float(ttl_env) if ttl_env else None
    namespace = os.getenv("SCILLM_CACHE_NAMESPACE") or os.getenv("RUN_ID")

    url = os.getenv("REDIS_URL")
    host = os.getenv("REDIS_HOST", "localhost")
    port = int(os.getenv("REDIS_PORT", 6379))
    password = os.getenv("REDIS_PASSWORD")
    db = int(os.getenv("REDIS_DB", 0))

    if redis is None:
        logger.warning("redis package not installed; using in-memory cache")
        litellm.cache = LiteLLMCache(type=LiteLLMCacheType.LOCAL, namespace=namespace, ttl=ttl)
        litellm.enable_cache()
        logger.info("cache=in-memory namespace=%s ttl=%s", namespace, ttl)
        return

    try:
        client = redis.from_url(url, socket_timeout=2, decode_responses=True) if url else redis.Redis(host=host, port=port, db=db, password=password, socket_timeout=2, decode_responses=True)
        if not client.ping():
            raise ConnectionError("Redis ping failed")

        keys = client.keys("*")
        if keys:
            logger.debug("Existing Redis keys: %s", truncate_large_value(keys))

        litellm.cache = LiteLLMCache(type=LiteLLMCacheType.REDIS, host=host if not url else None, port=str(port) if not url else None, password=password, namespace=namespace, supported_call_types=["acompletion", "completion"], ttl=ttl if ttl is not None else 60*60*24*3)
        litellm.enable_cache()
        logger.info("cache=redis namespace=%s ttl=%s url=%s host=%s port=%s db=%s", namespace, ttl if ttl is not None else 60*60*24*3, bool(url), host, port, db)

        try:
            test_key = "litellm_cache_test"
            client.set(test_key, "test_value", ex=60)
            assert client.get(test_key) == "test_value"
            client.delete(test_key)
        except Exception as exc:  # pragma: no cover - diagnostics only
            logger.warning("Redis test write/read failed: %s", exc)
    except Exception as exc:
        logger.warning("Redis cache unavailable (%s); falling back to memory", exc)
        litellm.cache = LiteLLMCache(type=LiteLLMCacheType.LOCAL, namespace=namespace, ttl=ttl)
        litellm.enable_cache()
        logger.info("cache=in-memory namespace=%s ttl=%s", namespace, ttl)


def test_litellm_cache() -> Tuple[bool, Dict[str, Optional[bool]]]:
    """Perform a simple cache warm-up to verify the configuration."""

    initialize_litellm_cache()
    from litellm import completion

    try:
        _ = completion(model="gpt-3.5-turbo", messages=[{"role": "user", "content": "ping"}])
    except Exception:
        # We only care that cache is configured; provider may be unavailable
        pass
    cache_hit = getattr(litellm, "cache", None) is not None
    return cache_hit, {"cache_configured": cache_hit}



def cache_stats() -> Dict[str, Optional[str]]:
    """Return lightweight cache configuration/health for doctors.

    Note: hit/miss counters are not yet exposed by LiteLLM Cache; this reports backend and namespace.
    """
    backend = "disabled"
    ns = None
    ttl = None
    try:
        c = getattr(litellm, "cache", None)
        if c is None:
            backend = "disabled"
        else:
            backend = getattr(c, "type", None) or "unknown"
            ns = getattr(c, "namespace", None)
            ttl = getattr(c, "ttl", None)
    except Exception:
        pass
    return {"backend": str(backend), "namespace": ns, "ttl": str(ttl) if ttl is not None else None}
