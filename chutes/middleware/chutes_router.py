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
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from loguru import logger

# Time-based routing: Claude during day, Chutes at night
_ET_TIMEZONE = ZoneInfo("America/New_York")
_DAYTIME_START = 7   # 7 AM ET
_DAYTIME_END = 22    # 10 PM ET
_DAYTIME_MODEL = "claude-sonnet-4-6"  # Reliable model for daytime


def _is_daytime() -> bool:
    """Check if current time is daytime (7AM-10PM ET).

    DISABLED: Always use Chutes with dynamic routing to find least saturated model.
    Claude OAuth should not be used for batch processing (ToS risk).
    """
    return False  # Disabled - use Chutes dynamic routing instead

from scillm.proxy.middleware import BaseMiddleware

# Chutes API
_CHUTES_API_BASE = "https://api.chutes.ai"
_CHUTES_API_KEY = os.environ.get("CHUTES_API_KEY") or os.environ.get("CHUTES_API_TOKEN", "")

# Cache settings
_CACHE_TTL_SEC = 300  # 5 minutes
_utilization_cache: dict | None = None
_cache_timestamp: float = 0

# Rate limit threshold - avoid models with high rate limiting
# 25% threshold balances avoiding saturated models vs having options
_RATE_LIMIT_THRESHOLD = 0.25

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
# Model family patterns - dynamically match models from utilization API
# ---------------------------------------------------------------------------

# Pattern matchers for 671B DeepSeek models (V3, R1, Chimera variants)
_DEEPSEEK_671B_PATTERNS = [
    "deepseek-v3",
    "deepseek-r1",
    "deepseek-tng",
    "chimera",
]

# Family pattern definitions: family_name -> (include_patterns, exclude_patterns)
# Models matching include AND not matching exclude are added to the family
FAMILY_PATTERNS: dict[str, tuple[list[str], list[str]]] = {
    # DeepSeek 671B: V3, R1, Chimera variants (exclude distilled/small)
    "deepseek-large": (
        ["deepseek"],
        ["distill", "7b", "8b", "14b", "32b", "70b", "lite"],
    ),
    # Qwen3 large: 235B+ models
    "qwen3-large": (
        ["qwen3", "235b", "397b"],
        ["7b", "8b", "14b", "30b", "32b", "72b"],
    ),
    # Qwen3 small: 30-32B models
    "qwen3-small": (
        ["qwen3", "30b", "32b"],
        ["235b", "397b"],
    ),
    # Kimi/Moonshot
    "kimi": (
        ["kimi", "moonshot"],
        [],
    ),
}


def _matches_family(model_name: str, family: str) -> bool:
    """Check if model matches family patterns."""
    patterns = FAMILY_PATTERNS.get(family)
    if not patterns:
        return False
    include, exclude = patterns
    name_lower = model_name.lower()

    # Must match at least one include pattern
    if not any(p in name_lower for p in include):
        return False

    # Must not match any exclude pattern
    if any(p in name_lower for p in exclude):
        return False

    return True


def _build_dynamic_families(util_data: list[dict]) -> dict[str, list[str]]:
    """Build model families dynamically from utilization API data."""
    families: dict[str, list[str]] = {f: [] for f in FAMILY_PATTERNS}

    for entry in util_data:
        name = entry.get("name", "")
        if not name or name.startswith("[private"):
            continue

        for family in FAMILY_PATTERNS:
            if _matches_family(name, family):
                families[family].append(name)
                break  # Model belongs to first matching family only

    # Log discovered families
    for family, models in families.items():
        if models:
            logger.debug("chutes_router: {} family has {} models: {}",
                        family, len(models), [m.split("/")[-1] for m in models])

    return families


# Dynamic family cache (populated from utilization API)
_dynamic_families: dict[str, list[str]] = {}
_families_timestamp: float = 0


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
    Also rebuilds dynamic model families from the API data.
    """
    global _utilization_cache, _cache_timestamp, _dynamic_families, _families_timestamp

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

    # Build dynamic families from the API data
    _dynamic_families = _build_dynamic_families(raw)
    _families_timestamp = now

    logger.info(
        "chutes_router: refreshed utilization cache ({} models, {} in deepseek-large family)",
        len(cache) // 2,  # Divide by 2 because we index twice
        len(_dynamic_families.get("deepseek-large", [])),
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

    # Avoid models with 0% utilization - likely cold/no miners serving
    # A warm model with light load would still show some utilization
    if util < 0.01:
        return 95.0  # Avoid but not as bad as rate-limited

    # Score based on utilization (0-80 range)
    # Prefer models in 10-70% range (warm but not saturated)
    return util * 80


async def _select_best_variant(family: str, util_cache: dict[str, dict]) -> str | None:
    """Select the best model variant from a family based on utilization.

    Returns the model name with lowest score, or None if no good options.
    Uses dynamically discovered families from the utilization API.
    """
    # Use dynamic families (populated by _get_utilization)
    candidates = _dynamic_families.get(family, [])
    if not candidates:
        logger.debug("chutes_router: no candidates for family '{}' (families: {})",
                    family, list(_dynamic_families.keys()))
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
    """Get the family for a model string using pattern matching."""
    # Check against all family patterns
    for family in FAMILY_PATTERNS:
        if _matches_family(model, family):
            return family
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
    """Routes Chutes requests to the least saturated model variant.

    Time-based routing:
    - Daytime (7AM-10PM ET): Route to Claude Sonnet (reliable)
    - Nighttime (10PM-7AM ET): Use Chutes with utilization-aware selection
    """

    async def pre_call(self, request: dict) -> dict | None:
        model = (request.get("model") or "").strip()
        if not model:
            return request

        # Only process Chutes models and text alias
        is_text_alias = model.lower() in ("text", "text-research")
        if not is_text_alias and not _is_chutes_model(model):
            return request

        # Daytime: route to Claude Sonnet for reliability
        if _is_daytime():
            if is_text_alias or _is_chutes_model(model):
                request["model"] = _DAYTIME_MODEL
                logger.info(
                    "chutes_router: daytime routing '{}' -> '{}' (7AM-10PM ET)",
                    model,
                    _DAYTIME_MODEL,
                )
                return request

        # Nighttime: use Chutes with utilization-aware selection
        # Get family for this model
        family = _get_family_for_model(model)
        if not family:
            if is_text_alias:
                family = "deepseek-large"
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
