"""Chutes utilization-aware router middleware.

Queries the Chutes.ai utilization API and routes requests to the least saturated
model variant within the same capability family. Avoids models with high rate
limiting (>10% of requests rate-limited).

Model families:
- deepseek-v3: DeepSeek V3.x variants (TEE and non-TEE)
- deepseek-r1: DeepSeek R1 reasoning variants
- qwen3-large: Qwen3 235B+ models
- qwen3-small: Qwen3 30-32B models

The middleware checks utilization every 5 minutes and caches results. When a
request comes in for a Chutes model, it swaps to the best available variant.
"""
from __future__ import annotations

import os
import time
from typing import Any

import httpx
from loguru import logger

from scillm.proxy.middleware import BaseMiddleware

# Chutes API
_CHUTES_API_BASE = "https://api.chutes.ai"
_CHUTES_API_KEY = os.environ.get("CHUTES_API_KEY") or os.environ.get("CHUTES_API_TOKEN", "")

# Cache settings
_CACHE_TTL_SEC = 300  # 5 minutes
_utilization_cache: dict | None = None
_cache_timestamp: float = 0

# Rate limit threshold - don't use models with >10% rate limiting
_RATE_LIMIT_THRESHOLD = 0.10

# Utilization threshold - prefer models under 80% utilization
_UTILIZATION_PREFERRED = 0.80
_UTILIZATION_MAX = 0.95  # Avoid models over 95% utilization

# Lazy-init httpx client
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
    return _client


# ---------------------------------------------------------------------------
# Model families - groups of equivalent capability models
# ---------------------------------------------------------------------------

MODEL_FAMILIES: dict[str, list[str]] = {
    # DeepSeek V3.x family - general text (best quality)
    "deepseek-v3": [
        "deepseek-ai/DeepSeek-V3.2-TEE",
        "deepseek-ai/DeepSeek-V3.1-TEE",
        "deepseek-ai/DeepSeek-V3-0324-TEE",
    ],
    # DeepSeek R1 family - 671B reasoning models only
    "deepseek-r1": [
        "deepseek-ai/DeepSeek-R1-0528-TEE",
        "deepseek-ai/DeepSeek-R1-0528",
        "tngtech/DeepSeek-TNG-R1T2-Chimera-TEE",
    ],
    # Qwen3 large family - 235B+ models
    "qwen3-large": [
        "Qwen/Qwen3-235B-A22B-Instruct-2507-TEE",
        "Qwen/Qwen3-235B-A22B-Instruct-2507",
        "Qwen/Qwen3.5-397B-A17B-TEE",
    ],
    # Qwen3 small family - 30-32B models
    "qwen3-small": [
        "Qwen/Qwen3-32B-TEE",
        "Qwen/Qwen3-30B-A3B",
    ],
    # Kimi/Moonshot family
    "kimi": [
        "moonshotai/Kimi-K2-Instruct-0905",
        "moonshotai/Kimi-K2.5-TEE",
    ],
}

# Reverse lookup: model -> family
_MODEL_TO_FAMILY: dict[str, str] = {}
for family, models in MODEL_FAMILIES.items():
    for model in models:
        _MODEL_TO_FAMILY[model] = family
        # Also index lowercase and without org prefix
        _MODEL_TO_FAMILY[model.lower()] = family
        if "/" in model:
            _MODEL_TO_FAMILY[model.split("/")[1]] = family
            _MODEL_TO_FAMILY[model.split("/")[1].lower()] = family


# ---------------------------------------------------------------------------
# Utilization API
# ---------------------------------------------------------------------------


async def _fetch_utilization() -> list[dict]:
    """Fetch utilization data from Chutes API."""
    if not _CHUTES_API_KEY:
        logger.debug("chutes_router: No CHUTES_API_KEY, skipping utilization fetch")
        return []

    try:
        client = _get_client()
        resp = await client.get(
            f"{_CHUTES_API_BASE}/chutes/utilization",
            headers={
                "Authorization": f"Bearer {_CHUTES_API_KEY}",
                "Content-Type": "application/json",
            },
        )
        if resp.status_code != 200:
            logger.warning("chutes_router: utilization API returned {}", resp.status_code)
            return []
        return resp.json()
    except Exception as e:
        logger.warning("chutes_router: failed to fetch utilization: {}", e)
        return []


async def _get_utilization() -> dict[str, dict]:
    """Get cached utilization data, refreshing if stale.

    Returns dict mapping model name -> utilization stats.
    """
    global _utilization_cache, _cache_timestamp

    now = time.monotonic()
    if _utilization_cache is not None and now - _cache_timestamp < _CACHE_TTL_SEC:
        return _utilization_cache

    # Fetch fresh data
    raw = await _fetch_utilization()
    if not raw:
        # Keep stale cache if fetch failed
        if _utilization_cache is not None:
            logger.debug("chutes_router: using stale cache after fetch failure")
            return _utilization_cache
        return {}

    # Index by model name
    cache = {}
    for entry in raw:
        name = entry.get("name", "")
        if name:
            cache[name] = entry
            # Also index without org prefix
            if "/" in name:
                cache[name.split("/")[1]] = entry

    _utilization_cache = cache
    _cache_timestamp = now
    logger.info(
        "chutes_router: refreshed utilization cache ({} models)",
        len(cache) // 2,  # Divide by 2 because we index twice
    )
    return cache


def _score_model(stats: dict) -> float:
    """Score a model - lower is better.

    Returns float from 0 (best) to 100 (worst).
    Returns 100 if model should be avoided.
    """
    util = stats.get("utilization_current", 1.0)
    rate_limit = stats.get("rate_limit_ratio_5m", 0.0)

    # Avoid models with high rate limiting
    if rate_limit > _RATE_LIMIT_THRESHOLD:
        return 100.0

    # Avoid models over utilization max
    if util > _UTILIZATION_MAX:
        return 90.0 + (util * 10)

    # Score based on utilization (0-80 range)
    return util * 80


async def _select_best_variant(family: str, util_cache: dict[str, dict]) -> str | None:
    """Select the best model variant from a family based on utilization.

    Returns the model name with lowest score, or None if no good options.
    """
    candidates = MODEL_FAMILIES.get(family, [])
    if not candidates:
        return None

    scored = []
    for model in candidates:
        # Try to find utilization data
        stats = util_cache.get(model) or util_cache.get(model.split("/")[-1] if "/" in model else model)
        if stats:
            score = _score_model(stats)
            util = stats.get("utilization_current", 1.0)
            rate_limit = stats.get("rate_limit_ratio_5m", 0.0)
            scored.append((score, model, util, rate_limit))
            logger.debug(
                "chutes_router: {} score={:.1f} (util={:.1%}, rl={:.1%})",
                model.split("/")[-1] if "/" in model else model,
                score,
                util,
                rate_limit,
            )
        else:
            # No data - assume it's available but unknown
            scored.append((50.0, model, None, None))
            logger.debug("chutes_router: {} score=50.0 (no data)", model)

    if not scored:
        return None

    # Sort by score (lowest first)
    scored.sort(key=lambda x: x[0])
    best_score, best_model, util, rate_limit = scored[0]

    # If best option is still bad, log warning
    if best_score >= 90:
        logger.warning(
            "chutes_router: all {} variants are saturated, best is {} (score={:.1f})",
            family,
            best_model,
            best_score,
        )

    return best_model


def _get_family_for_model(model: str) -> str | None:
    """Get the family for a model string."""
    # Direct lookup
    if model in _MODEL_TO_FAMILY:
        return _MODEL_TO_FAMILY[model]

    # Try lowercase
    model_lower = model.lower()
    if model_lower in _MODEL_TO_FAMILY:
        return _MODEL_TO_FAMILY[model_lower]

    # Try pattern matching for common Chutes patterns
    if "deepseek" in model_lower:
        if "v3" in model_lower:
            return "deepseek-v3"
        if "r1" in model_lower:
            return "deepseek-r1"

    if "qwen3" in model_lower:
        # Check size indicators
        if any(x in model_lower for x in ["235b", "397b"]):
            return "qwen3-large"
        if any(x in model_lower for x in ["30b", "32b"]):
            return "qwen3-small"

    if "kimi" in model_lower or "moonshot" in model_lower:
        return "kimi"

    return None


def _is_chutes_model(model: str) -> bool:
    """Check if model should be routed through Chutes."""
    model_lower = model.lower()

    # Explicit Chutes aliases
    if model in ("text", "text-research"):
        return True

    # Org/Model format typically means Chutes
    if "/" in model and not model.startswith("http"):
        return True

    # Known Chutes patterns
    if any(x in model_lower for x in ["deepseek-ai/", "qwen/", "moonshotai/", "tngtech/"]):
        return True

    return False


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class ChutesRouter(BaseMiddleware):
    """Routes Chutes requests to the least saturated model variant."""

    async def pre_call(self, request: dict) -> dict | None:
        model = (request.get("model") or "").strip()
        if not model:
            return request

        # Only process Chutes models
        if not _is_chutes_model(model):
            return request

        # Get family for this model
        family = _get_family_for_model(model)
        if not family:
            # For "text" alias, default to deepseek-v3 family
            if model.lower() in ("text", "text-research"):
                family = "deepseek-v3"
            else:
                logger.debug("chutes_router: no family for '{}', passing through", model)
                return request

        # Get utilization data
        util_cache = await _get_utilization()
        if not util_cache:
            logger.debug("chutes_router: no utilization data, passing through")
            return request

        # Select best variant
        best = await _select_best_variant(family, util_cache)
        if not best:
            logger.debug("chutes_router: no variant selected for family '{}', passing through", family)
            return request

        # Swap model if different
        if best != model:
            original = model
            request["model"] = best
            # Get stats for logging
            stats = util_cache.get(best) or util_cache.get(best.split("/")[-1] if "/" in best else best)
            util_pct = stats.get("utilization_current", 0) * 100 if stats else 0
            logger.info(
                "chutes_router: '{}' -> '{}' (util={:.0f}%, family={})",
                original,
                best.split("/")[-1] if "/" in best else best,
                util_pct,
                family,
            )

        return request


# ---------------------------------------------------------------------------
# Legacy callback interface (for success_callback in config)
# ---------------------------------------------------------------------------

_router_instance: ChutesRouter | None = None


def get_router() -> ChutesRouter:
    global _router_instance
    if _router_instance is None:
        _router_instance = ChutesRouter()
    return _router_instance


async def chutes_router(data: dict, *args, **kwargs) -> dict:
    """Callback-style interface for success_callback."""
    return await get_router().pre_call(data) or data
