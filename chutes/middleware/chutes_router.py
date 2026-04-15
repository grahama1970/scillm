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
import re
import time

import httpx
from loguru import logger


# ---------------------------------------------------------------------------
# Model size extraction from name
# ---------------------------------------------------------------------------

# Regex to extract parameter count from model names (e.g., "235B", "32B", "0.6B")
_SIZE_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*[Bb]", re.IGNORECASE)

# HuggingFace metadata cache: repo_id -> {"size_b": float, "tags": [...]}
_hf_cache: dict[str, dict] = {}
_HF_API_BASE = "https://huggingface.co/api/models"


async def _lookup_hf_metadata(repo_id: str) -> dict | None:
    """Fetch model metadata from HuggingFace API. Cached permanently."""
    if repo_id in _hf_cache:
        return _hf_cache[repo_id]

    # Skip non-HF format names
    if "/" not in repo_id:
        return None

    # Strip -TEE suffix (Chutes-specific) before querying HuggingFace
    hf_repo_id = re.sub(r"-TEE$", "", repo_id)

    try:
        client = _get_client()
        resp = await client.get(f"{_HF_API_BASE}/{hf_repo_id}", timeout=5.0)
        if resp.status_code != 200:
            logger.debug("hf_lookup: {} returned {}", hf_repo_id, resp.status_code)
            _hf_cache[repo_id] = {}  # Cache negative result (with original key)
            return None

        data = resp.json()
        # Extract size from safetensors.parameters (max value across precisions)
        # safetensors.total is FILE SIZE, parameters is PARAM COUNT per precision
        size_b = None
        if data.get("safetensors") and data["safetensors"].get("parameters"):
            params = data["safetensors"]["parameters"]
            # params is {"BF16": N, "F8_E4M3": N, ...} — take max as true param count
            max_params = max(params.values())
            size_b = max_params / 1e9

        metadata = {
            "size_b": size_b,
            "tags": data.get("tags", []),
            "pipeline_tag": data.get("pipeline_tag"),
        }
        _hf_cache[repo_id] = metadata  # Cache with original key (including -TEE)
        logger.debug("hf_lookup: {} → {:.1f}B, tags={}", repo_id, size_b or 0, metadata["tags"][:3])
        return metadata
    except Exception as e:
        logger.debug("hf_lookup: {} failed: {}", repo_id, e)
        _hf_cache[repo_id] = {}  # Cache failure
        return None


def _extract_size_b(model_name: str) -> float | None:
    """Extract model size in billions from name. Returns None if not found."""
    match = _SIZE_PATTERN.search(model_name)
    if match:
        return float(match.group(1))
    return None


async def _get_size_b(model_name: str) -> float | None:
    """Get model size: HuggingFace first (authoritative), regex fallback."""
    # Primary: HuggingFace API (cached after first call)
    if "/" in model_name:
        metadata = await _lookup_hf_metadata(model_name)
        if metadata and metadata.get("size_b"):
            return metadata["size_b"]

    # Fallback: regex extraction from name (for non-HF models or HF failures)
    size = _extract_size_b(model_name)
    if size is not None:
        return size

    return None


async def _is_large_model(model_name: str, threshold_b: float = 100.0) -> bool:
    """Check if model has >= threshold_b billion parameters."""
    size = await _get_size_b(model_name)
    if size is None:
        # DeepSeek V3/R1 without explicit size are 671B (HF may not have safetensors)
        name_lower = model_name.lower()
        if "deepseek" in name_lower and any(x in name_lower for x in ["v3", "r1", "chimera"]):
            return True
        return False
    return size >= threshold_b


def _is_chat_model(model_name: str) -> bool:
    """Check if model is a chat/completion model (not embedding/guard/coder)."""
    name_lower = model_name.lower()
    non_chat = ["embedding", "guard", "coder", "vlm", "vl-", "vision"]
    return not any(x in name_lower for x in non_chat)

# Time-based routing disabled — always use Chutes dynamic routing
_DAYTIME_MODEL = "claude-sonnet-4-6"  # Fallback if re-enabled


def _is_daytime() -> bool:
    """Disabled — always use Chutes dynamic routing."""
    return False


from scillm.proxy.middleware import BaseMiddleware

# Chutes API
_CHUTES_API_BASE = "https://api.chutes.ai"
_CHUTES_API_KEY = os.environ.get("CHUTES_API_KEY") or os.environ.get("CHUTES_API_TOKEN", "")

# Cache settings
_CACHE_TTL_SEC = 300  # 5 minutes
_utilization_cache: dict | None = None
_cache_timestamp: float = 0

# Rate limit threshold - avoid models with high rate limiting
_RATE_LIMIT_THRESHOLD = 0.25
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
# Model family matching with dynamic size extraction
# ---------------------------------------------------------------------------

# Alias to family mapping - enables dynamic routing for family-specific aliases
# When user requests "text-qwen3", they get dynamic routing within the qwen3 family
ALIAS_TO_FAMILY: dict[str, str] = {
    "text": "deepseek-large",           # Default: best available large model
    "text-research": "deepseek-large",  # Harvard research endpoint
    "text-qwen3": "qwen3-large",        # Qwen3 family (235B+)
    "text-qwen3-large": "qwen3-large",  # Explicit large Qwen3
    "text-kimi": "kimi",                # Kimi/Moonshot family
    "text-deepseek": "deepseek-large",  # Explicit DeepSeek family
}


async def _matches_family(model_name: str, family: str) -> bool:
    """Check if model matches family using HuggingFace metadata.

    Family matching is now based on:
    1. Model name patterns (deepseek, qwen3, kimi)
    2. Size from HuggingFace API (>= 100B for "large" families)
    3. Model type (chat vs embedding/guard/coder)
    """
    name_lower = model_name.lower()

    if family == "deepseek-large":
        # Must be DeepSeek and large (V3, R1, Chimera are all 671B)
        if "deepseek" not in name_lower:
            return False
        if "distill" in name_lower or "lite" in name_lower:
            return False
        return await _is_large_model(model_name) and _is_chat_model(model_name)

    elif family == "qwen3-large":
        # Must be Qwen3, large (>=100B), and chat model
        if "qwen3" not in name_lower and "qwen/qwen3" not in name_lower:
            return False
        return await _is_large_model(model_name) and _is_chat_model(model_name)

    elif family == "qwen3-small":
        # Qwen3 under 100B
        if "qwen3" not in name_lower and "qwen/qwen3" not in name_lower:
            return False
        size = await _get_size_b(model_name)
        if size is None:
            return False
        return size < 100 and _is_chat_model(model_name)

    elif family == "kimi":
        return "kimi" in name_lower or "moonshot" in name_lower

    return False


# Known family names for iteration
_FAMILIES = ["deepseek-large", "qwen3-large", "qwen3-small", "kimi"]


async def _build_dynamic_families(util_data: list[dict]) -> dict[str, list[str]]:
    """Build model families dynamically from utilization API data.

    Discovers all available models and groups them by family using
    HuggingFace metadata for size information.
    """
    families: dict[str, list[str]] = {f: [] for f in _FAMILIES}

    for entry in util_data:
        name = entry.get("name", "")
        if not name or name.startswith("[private"):
            continue

        for family in _FAMILIES:
            if await _matches_family(name, family):
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

    # Build dynamic families from the API data (uses HF API for size lookup)
    _dynamic_families = await _build_dynamic_families(raw)
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


async def _build_dynamic_chain(family: str, util_cache: dict[str, dict]) -> list[str]:
    """Build a full fallback chain sorted by utilization score.

    Returns all models in the family sorted best-first, plus static fallbacks.
    """
    # Use dynamic families (populated by _get_utilization)
    candidates = _dynamic_families.get(family, [])
    if not candidates:
        logger.debug("chutes_router: no candidates for family '{}' (families: {})",
                    family, list(_dynamic_families.keys()))
        return []

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
        return []

    # Sort by score (lowest first)
    scored.sort(key=lambda x: x[0])

    # Build chain: sorted Chutes models + static fallbacks (Kimi, Qwen3)
    chain = [m for _, m, _, _ in scored]

    # Add non-DeepSeek fallbacks at the end (these don't have utilization data)
    static_fallbacks = ["text-kimi", "text-qwen3", "text-qwen3-large"]
    chain.extend(static_fallbacks)

    best_score, best_model, util, rate_limit = scored[0]

    # If best option is still bad, log warning
    if best_score >= 90:
        logger.warning(
            "chutes_router: all {} variants are saturated, best is {} (score={:.1f})",
            family,
            best_model,
            best_score,
        )

    return chain


def _get_family_for_model(model: str) -> str | None:
    """Get the family for a model string using dynamic size extraction."""
    for family in _FAMILIES:
        if _matches_family(model, family):
            return family
    return None


def _is_chutes_model(model: str) -> bool:
    """Check if model should be routed through Chutes."""
    model_lower = model.lower()

    # Family-specific aliases get dynamic routing within their family
    if model_lower in ALIAS_TO_FAMILY:
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

        # Only process Chutes models and known aliases
        model_lower = model.lower()
        is_known_alias = model_lower in ALIAS_TO_FAMILY
        if not is_known_alias and not _is_chutes_model(model):
            return request

        # Daytime: route to Claude Sonnet for reliability
        if _is_daytime():
            if is_known_alias or _is_chutes_model(model):
                request["model"] = _DAYTIME_MODEL
                logger.info(
                    "chutes_router: daytime routing '{}' -> '{}' (7AM-10PM ET)",
                    model,
                    _DAYTIME_MODEL,
                )
                return request

        # Nighttime: use Chutes with utilization-aware selection
        # Get family for this model - check alias mapping first, then pattern matching
        model_lower = model.lower()
        if model_lower in ALIAS_TO_FAMILY:
            family = ALIAS_TO_FAMILY[model_lower]
        else:
            family = _get_family_for_model(model)
            if not family:
                logger.debug("chutes_router: no family for '{}', passing through", model)
                return request

        # Get utilization data
        util_cache = await _get_utilization()
        if not util_cache:
            logger.debug("chutes_router: no utilization data, passing through")
            return request

        # Build dynamic fallback chain sorted by utilization
        chain = await _build_dynamic_chain(family, util_cache)
        if not chain:
            logger.debug("chutes_router: no chain built for family '{}', passing through", family)
            return request

        best = chain[0]

        # Swap model to best variant
        if best != model:
            original = model
            request["model"] = best
            # Get stats for logging
            stats = util_cache.get(best) or util_cache.get(best.split("/")[-1] if "/" in best else best)
            util_pct = stats.get("utilization_current", 0) * 100 if stats else 0
            logger.info(
                "chutes_router: '{}' -> '{}' (util={:.0f}%, family={}, chain_len={})",
                original,
                best.split("/")[-1] if "/" in best else best,
                util_pct,
                family,
                len(chain),
            )

        # Inject dynamic fallback chain for router to use
        request["_dynamic_fallback_chain"] = chain
        logger.debug("chutes_router: dynamic chain = {}", [m.split("/")[-1] if "/" in m else m for m in chain])

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


async def chutes_router(data: dict, *_args, **_kwargs) -> dict:
    """Callback-style interface for success_callback."""
    return await get_router().pre_call(data) or data
