"""FastAPI application for the scillm proxy.

Thin OpenAI-compatible proxy that routes through middleware chain → router → streaming.
FastAPI application for the scillm proxy (~350 lines).
"""

from __future__ import annotations

import asyncio
import difflib
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from loguru import logger

from scillm.proxy.config import ProxyConfig, load_config
from scillm.proxy.errors import ProxyError, proxy_error_handler
from scillm.proxy.middleware import BaseMiddleware, MiddlewareChain, MiddlewareReject
from scillm.proxy.providers.opencode_go import (
    OPENCODE_GO_PROVIDER,
    describe_opencode_go_model,
    is_opencode_go_model,
    list_opencode_go_models_from_cli,
    list_opencode_go_models_from_server,
    static_opencode_go_models,
)
from scillm.proxy.router import Router
from scillm.proxy.router import ProxyError as RouterProxyError
from scillm.proxy.streaming import SSE_HEADERS, stream_response
from starlette.responses import StreamingResponse

# ---------------------------------------------------------------------------
# Globals (populated during lifespan)
# ---------------------------------------------------------------------------
_config: ProxyConfig | None = None
_router: Router | None = None
_middleware_chain: MiddlewareChain | None = None
_start_time: float = 0.0
_embedding_client: httpx.AsyncClient | None = None

EMBEDDING_SERVICE_URL = os.environ.get("EMBEDDING_SERVICE_URL", "http://127.0.0.1:8602")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _check_auth(request: Request) -> str | None:
    """Validate Bearer token. Returns None if OK, error message if not."""
    if not _config or not _config.general.master_key:
        return None
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return "Missing Bearer token"
    token = auth[7:].strip()
    if token != _config.general.master_key:
        return "Invalid API key"
    return None


def _is_empty_length_response(response_dict: dict[str, Any]) -> bool:
    """Return true when a provider spent its budget without visible output."""
    choices = response_dict.get("choices", [])
    if not choices:
        return False
    choice = choices[0] if isinstance(choices[0], dict) else {}
    if choice.get("finish_reason") != "length":
        return False

    message = choice.get("message") or {}
    content = message.get("content")
    if content is None:
        return True
    if isinstance(content, str) and not content.strip():
        return True
    return False


# ---------------------------------------------------------------------------
# Request Validation (fail loudly on common mistakes)
# ---------------------------------------------------------------------------

# Deprecated models — also added to alias map for auto-remap
# This dict provides error messages for startup env var warnings
_DEPRECATED_MODELS: dict[str, str] = {
    "deepseek-ai/DeepSeek-V3": "Model removed from Chutes. Use 'text' alias instead.",
    "deepseek-ai/DeepSeek-V3.1-TEE": "Model deprecated. Use 'text' alias instead.",
    "deepseek-ai/DeepSeek-V3-0324": "Model deprecated. Use 'text' alias instead.",
    "deepseek-ai/DeepSeek-V3-0324-TEE": "Model deprecated. Use 'text' alias instead.",
}

# Env vars that agents might set with model names — validate at startup
_MODEL_ENV_VARS = [
    "CHUTES_TEXT_MODEL",
    "CHUTES_VLM_MODEL",
    "CHUTES_MODEL_ID",
    "CHUTES_MODEL",
]


def _check_env_for_deprecated_models() -> None:
    """Warn at startup if env vars reference deprecated models."""
    for var in _MODEL_ENV_VARS:
        val = os.environ.get(var, "").strip()
        if val in _DEPRECATED_MODELS:
            logger.error(
                "ENV VAR DEPRECATED: {}='{}' — {}. "
                "Update .env to use 'text' or 'vlm' proxy aliases.",
                var, val, _DEPRECATED_MODELS[val],
            )
        elif val and val not in _VALID_MODEL_ALIASES and "/" in val:
            # Direct provider model name — warn but don't block
            logger.warning(
                "ENV VAR DIRECT MODEL: {}='{}' — consider using proxy alias 'text' or 'vlm' instead",
                var, val,
            )

# Direct provider model names that should use aliases instead
_DIRECT_MODEL_PATTERNS = {
    "claude-": "Use 'text' or 'vlm-claude' alias for Claude models (has fallbacks).",
    "gpt-4": "Use 'text' or 'vlm-codex' alias for GPT models (has fallbacks).",
    "gemini-": "Use 'text-gemini' alias for Gemini models (has fallbacks).",
}

_CHUTES_PROVIDER_PREFIXES = (
    "deepseek-ai/",
    "qwen/",
    "moonshotai/",
    "tngtech/",
    "zai-org/",
)

_CODEX_OAUTH_PREFIXES = ("codex", "gpt", "o1", "o3", "o4")

# Known good model aliases (checked at startup from config)
_VALID_MODEL_ALIASES: set[str] = set()

_BATCH_FORWARD_KEYS = (
    "temperature",
    "top_p",
    "frequency_penalty",
    "presence_penalty",
    "stop",
    "n",
    "response_format",
    "tools",
    "tool_choice",
    "seed",
    "logprobs",
    "top_logprobs",
)

_DEFAULT_MODEL_POOLS: dict[str, dict[str, Any]] = {
    "qra-deepseek-pool": {
        "description": "Concurrent QRA extraction across independent Chutes and OpenCode Go DeepSeek lanes.",
        "strategy": "weighted_round_robin",
        "lanes": [
            {
                "name": "chutes-deepseek",
                "provider": "chutes",
                "model": "deepseek-ai/DeepSeek-V3-0324-TEE",
                "weight": 3,
                "max_concurrency": 5,
                "timeout": 420.0,
            },
            {
                "name": "opencode-go-deepseek-v4-flash",
                "provider": OPENCODE_GO_PROVIDER,
                "model": "opencode-go/deepseek-v4-flash",
                "weight": 2,
                "max_concurrency": 4,
                "timeout": 620.0,
            },
        ],
    }
}


def _is_direct_chutes_model(model: str) -> bool:
    """Return true for direct Chutes provider/model ids."""
    return model.lower().startswith(_CHUTES_PROVIDER_PREFIXES)


def _is_codex_oauth_model(model: str) -> bool:
    """Return true for model ids handled by the Codex OAuth router."""
    return model.lower().startswith(_CODEX_OAUTH_PREFIXES) and "/" not in model


def _codex_oauth_available() -> bool:
    """Return true when Codex OAuth credentials are available."""
    try:
        from scillm.proxy.providers.auth import is_codex_available
        return is_codex_available()
    except Exception:
        return False


def _message_has_multimodal_content(message: dict[str, Any]) -> bool:
    content = message.get("content")
    if isinstance(content, list):
        return any(
            isinstance(part, dict)
            and (
                part.get("type") in {"image_url", "image", "document"}
                or "inlineData" in part
            )
            for part in content
        )
    return isinstance(content, dict) and (
        "image_url" in content
        or "inlineData" in content
        or content.get("type") in {"image_url", "image", "document"}
    )


def _model_pool(pool_name: str) -> dict[str, Any] | None:
    pool = _DEFAULT_MODEL_POOLS.get(pool_name)
    if pool is None:
        return None
    return {
        **pool,
        "lanes": [dict(lane) for lane in pool["lanes"]],
    }


def _model_pool_status(
    pool_name: str,
    pool: dict[str, Any],
    concurrency: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build pool-aware lane status from provider concurrency state."""
    concurrency = concurrency or {}
    lane_statuses = []
    seen_providers: set[str] = set()
    aggregate_in_flight = 0
    aggregate_limit = 0
    aggregate_queued = 0

    for lane in pool.get("lanes", []):
        provider = str(lane.get("provider") or "")
        provider_state = concurrency.get(provider, {}) if isinstance(concurrency, dict) else {}
        effective_limit = int(provider_state.get("effective_limit") or lane.get("max_concurrency") or 1)
        configured_limit = int(provider_state.get("configured_limit") or lane.get("max_concurrency") or effective_limit)
        semaphore_in_flight = int(
            provider_state.get("semaphore_in_flight")
            or provider_state.get("actual_in_flight")
            or provider_state.get("in_flight")
            or 0
        )
        live_in_flight = int(provider_state.get("live_in_flight") or semaphore_in_flight)
        queued = int(provider_state.get("queued") or 0)
        registry_in_flight = int(provider_state.get("registry_in_flight") or 0)
        stale_active_calls = int(provider_state.get("stale_active_calls") or 0)
        registry_drift = int(
            provider_state.get("registry_drift")
            if provider_state.get("registry_drift") is not None
            else registry_in_flight - semaphore_in_flight
        )
        status = {
            "name": lane.get("name"),
            "provider": provider,
            "model": lane.get("model"),
            "weight": lane.get("weight", 1),
            "lane_limit": int(lane.get("max_concurrency") or effective_limit),
            "configured_limit": configured_limit,
            "effective_limit": effective_limit,
            "in_flight": live_in_flight,
            "actual_in_flight": semaphore_in_flight,
            "live_in_flight": live_in_flight,
            "queued": queued,
            "available": int(provider_state.get("available") or max(0, effective_limit - semaphore_in_flight)),
            "paused": bool(provider_state.get("paused", False)),
            "backoff_active": bool(provider_state.get("backoff_active", False)),
            "pause_remaining_s": provider_state.get("pause_remaining_s", 0),
            "registry_in_flight": registry_in_flight,
            "stale_active_calls": stale_active_calls,
            "semaphore_in_flight": semaphore_in_flight,
            "registry_drift": registry_drift,
            "drift": registry_drift,
        }
        lane_statuses.append(status)
        if provider and provider not in seen_providers:
            seen_providers.add(provider)
            aggregate_in_flight += live_in_flight
            aggregate_limit += effective_limit
            aggregate_queued += queued

    return {
        "name": pool_name,
        "strategy": pool.get("strategy"),
        "in_flight": aggregate_in_flight,
        "actual_in_flight": aggregate_in_flight,
        "live_in_flight": aggregate_in_flight,
        "limit": aggregate_limit,
        "queued": aggregate_queued,
        "available": max(0, aggregate_limit - aggregate_in_flight),
        "lanes": lane_statuses,
    }


def _weighted_lane_sequence(lanes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expand lane weights into a deterministic weighted round-robin sequence."""
    sequence: list[dict[str, Any]] = []
    for lane in lanes:
        weight = int(lane.get("weight") or 1)
        sequence.extend([lane] * max(1, weight))
    if not sequence:
        raise ProxyError(400, "model pool must include at least one lane", "invalid_request_error")
    return sequence


def _lane_for_index(lanes: list[dict[str, Any]], index: int) -> dict[str, Any]:
    sequence = _weighted_lane_sequence(lanes)
    return sequence[index % len(sequence)]


def _messages_for_batch_item(item: dict[str, Any]) -> list[dict[str, Any]]:
    messages = item.get("messages")
    if isinstance(messages, list) and messages:
        return messages
    for key in ("prompt", "input", "content"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return [{"role": "user", "content": value}]
    raise ProxyError(400, "each batch item needs messages, prompt, input, or content", "invalid_request_error")


def _item_id(item: dict[str, Any], index: int) -> str:
    return str(item.get("item_id") or item.get("id") or f"item-{index + 1}")


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _request_deadline_timeout_s(body: dict[str, Any]) -> float | None:
    """Return caller-facing end-to-end timeout, not provider estimate."""
    explicit_timeout = _float_or_none(body.get("timeout"))
    if explicit_timeout is not None:
        return explicit_timeout
    return _float_or_none(body.get("_policy_max_timeout_s"))


def _timeout_error_details(
    body: dict[str, Any],
    *,
    model: str,
    started_at: float,
    timeout_s: float,
    final_provider_error: Exception | str | None = None,
) -> dict[str, Any]:
    metadata = body.get("_scillm_metadata")
    if not isinstance(metadata, dict):
        metadata = body.get("scillm_metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "caller": body.get("_caller_skill") or body.get("_headers", {}).get("x-caller-skill") or "unknown",
        "item_id": metadata.get("item_id"),
        "batch_id": metadata.get("batch_id"),
        "provider": body.get("_concurrency_provider"),
        "model": model,
        "elapsed_ms": int((time.monotonic() - started_at) * 1000),
        "timeout_s": timeout_s,
        "cascade_attempts": body.get("_cascade_attempts", 0),
        "final_provider_error": str(final_provider_error) if final_provider_error else None,
    }


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_adapter_manifests() -> dict[str, dict[str, Any]]:
    """Load local read-only adapter capability manifests."""
    manifests: dict[str, dict[str, Any]] = {}
    manifest_dir = _repo_root() / "registry" / "adapters"
    if not manifest_dir.exists():
        return manifests
    for path in sorted(manifest_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except Exception as exc:
            logger.warning("failed to read adapter manifest {}: {}", path, exc)
            continue
        if not isinstance(data, dict):
            continue
        adapter_id = str(data.get("id") or path.stem)
        manifests[adapter_id] = data
    return manifests


def _adapter_id_for_deployment(dep: Any, group_name: str = "") -> str:
    provider = (dep.custom_llm_provider or "").lower()
    model = (dep.model or "").lower()
    api_base = (dep.api_base or "").lower()
    group = group_name.lower()

    if provider == "anthropic-oauth" or model.startswith("claude") or "anthropic" in api_base:
        return "claude_oauth"
    if provider == "codex-oauth" or model.startswith(("gpt", "codex")):
        return "codex_oauth"
    if provider in {"gemini-oauth", "gemini"} or model.startswith("gemini") or "generativelanguage.googleapis.com" in api_base:
        return "gemini"
    if provider.startswith("opencode-go") or group.startswith("opencode-go/"):
        return "opencode_go"
    if "ollama" in api_base or ":" in model:
        return "ollama"
    if "chutes" in api_base or "/" in dep.model:
        return "chutes"
    return "openai_compatible"


def _deployment_supports(dep: Any, group_name: str, adapters: dict[str, dict[str, Any]]) -> dict[str, bool]:
    adapter_id = _adapter_id_for_deployment(dep, group_name)
    adapter_supports = adapters.get(adapter_id, {}).get("supports", {})
    supports = {
        "text": True,
        "image": bool(adapter_supports.get("image", False)),
        "pdf": bool(adapter_supports.get("pdf", False)),
        "zip": bool(adapter_supports.get("zip", False)),
        "streaming": bool(adapter_supports.get("streaming", True)),
        "tools": bool(adapter_supports.get("tools", True)),
    }
    model = (dep.model or "").lower()
    group = group_name.lower()
    if group.startswith("vlm") or "glm-4.6v" in model or "vl" in model:
        supports["image"] = True
    if adapter_id == "opencode_go":
        supports["image"] = False
        supports["pdf"] = False
        supports["zip"] = False
    return supports


def _merge_supports(items: list[dict[str, bool]]) -> dict[str, bool]:
    keys = {"text", "image", "pdf", "zip", "streaming", "tools"}
    return {key: any(item.get(key, False) for item in items) for key in sorted(keys)}


def _suggest_model(unknown: str, candidates: set[str], n: int = 3) -> list[str]:
    """Suggest closest matching model names using fuzzy matching.

    Returns up to n suggestions, sorted by similarity (best first).
    """
    # Filter out internal model names (deepseek-ai/, Qwen/, etc.)
    user_facing = [m for m in candidates if "/" not in m]
    matches = difflib.get_close_matches(unknown.lower(), [m.lower() for m in user_facing], n=n, cutoff=0.4)
    # Map back to original case
    lower_to_orig = {m.lower(): m for m in user_facing}
    return [lower_to_orig[m] for m in matches]


def _validate_model_request(model: str, body: dict, request: Request) -> None:
    """Validate model request and auto-fix common issues.

    Guardrails (in order):
    1. Reject deprecated models with clear error
    2. Reject unknown models with helpful suggestions
    3. Reject empty messages
    4. Warn on direct model names (should use aliases)
    5. Auto-enable x-expect-json when response_format: json_object is used
    6. Strip max_tokens (causes empty output on reasoning models)
    7. Clamp temperature to valid range
    8. Warn on very long prompts
    """
    messages = body.get("messages", [])

    # 0a. Handle string messages — common agent mistake
    # Agents often pass messages="prompt" instead of messages=[{"role": "user", "content": "prompt"}]
    if isinstance(messages, str):
        logger.warning(
            "Auto-wrapping string messages as list — caller passed string instead of array. "
            "Correct format: messages=[{\"role\": \"user\", \"content\": \"...\"}]"
        )
        body["messages"] = [{"role": "user", "content": messages}]
        messages = body["messages"]

    # 0b. Reject empty messages — crashes some providers
    if not messages:
        raise ProxyError(
            400,
            "messages is required and cannot be empty. "
            "Provide at least one message: [{\"role\": \"user\", \"content\": \"...\"}]. "
            "scillm is an HTTP API — call via httpx, not import.",
            "invalid_request_error",
        )

    # 1. Log deprecated models but let alias map handle the remap
    if model in _DEPRECATED_MODELS:
        caller = request.headers.get("x-caller-skill", "unknown")
        logger.info(
            "Deprecated model '{}' requested by '{}' — auto-remapping via alias. {}",
            model, caller, _DEPRECATED_MODELS[model],
        )

    # 2. Reject unknown models with helpful error + suggestions
    # Only check if we have loaded valid aliases (after startup)
    if (
        _VALID_MODEL_ALIASES
        and model not in _VALID_MODEL_ALIASES
        and not is_opencode_go_model(model)
        and not _is_direct_chutes_model(model)
        and not (_is_codex_oauth_model(model) and _codex_oauth_available())
    ):
        suggestions = _suggest_model(model, _VALID_MODEL_ALIASES)
        available = ", ".join(sorted(m for m in _VALID_MODEL_ALIASES if "/" not in m))
        msg = f"Unknown model '{model}'."
        if suggestions:
            msg += f" Did you mean: {', '.join(suggestions)}?"
        msg += f" Available: {available}."
        raise ProxyError(400, msg, "invalid_request_error")

    # 3. Warn on direct model names (log only, don't block)
    model_lower = model.lower()
    for pattern, hint in _DIRECT_MODEL_PATTERNS.items():
        if model_lower.startswith(pattern) and "/" not in model:
            logger.warning(
                f"Direct model name '{model}' used without fallbacks. {hint}"
            )
            break

    # 4. Auto-enable JSON repair when response_format: json_object is used
    # This is the #1 cause of failures — Claude ignores response_format and
    # returns markdown-wrapped JSON. Auto-fix makes it "just work".
    rf = body.get("response_format")
    if isinstance(rf, dict) and rf.get("type") in ("json_object", "json_schema"):
        # Always enable JSON repair when JSON is requested
        body["_expect_json"] = True
        logger.debug("Auto-enabled JSON repair for response_format: json_object")

    # 5. Strip max_tokens — causes 90% empty responses on reasoning models
    # Reasoning models (DeepSeek-R1, o1, etc) spend tokens on internal reasoning.
    # If max_tokens is set too low, all tokens go to reasoning and output is empty.
    # Better to let the model decide than risk empty responses.
    if body.get("max_tokens") is None:
        body.pop("max_tokens", None)
    elif "max_tokens" in body:
        logger.warning(
            f"Stripping max_tokens={body['max_tokens']} — causes empty output on reasoning models. "
            f"See MEMORY.md: 'Never use max_tokens'."
        )
        del body["max_tokens"]

    # Also honor explicit x-expect-json header
    if request.headers.get("x-expect-json", "").lower() in ("true", "1", "yes"):
        body["_expect_json"] = True

    # 6. Clamp temperature to valid range (0-2)
    # Values outside this range break most providers
    temp = body.get("temperature")
    if temp is not None:
        if not isinstance(temp, (int, float)):
            logger.warning(f"Invalid temperature type {type(temp).__name__}, removing")
            del body["temperature"]
        elif temp < 0 or temp > 2:
            clamped = max(0, min(2, temp))
            logger.warning(f"Clamping temperature {temp} to valid range: {clamped}")
            body["temperature"] = clamped

    # 7. Warn on very long prompts (>100k chars ~ 25k tokens)
    # These can timeout or OOM on some providers
    total_chars = sum(len(str(m.get("content", ""))) for m in messages)
    if total_chars > 100000:
        logger.warning(
            f"Very long prompt ({total_chars:,} chars, ~{total_chars//4:,} tokens). "
            f"May timeout on some providers."
        )

    # 8. Detect empty content in messages — crashes some providers
    for i, msg in enumerate(messages):
        content = msg.get("content")
        if content is None or (isinstance(content, str) and not content.strip()):
            logger.warning(
                f"Message {i} has empty content — may crash some providers. "
                f"Ensure all messages have non-empty content."
            )

    # 9. Detect inlineData with non-Gemini model — silent failure
    # inlineData only works with Gemini's native API
    has_inline_data = any(
        isinstance(msg.get("content"), list) and
        any(isinstance(p, dict) and "inlineData" in p for p in msg.get("content", []))
        for msg in messages
    )
    has_multimodal_content = any(
        isinstance(msg, dict) and _message_has_multimodal_content(msg)
        for msg in messages
    )
    if is_opencode_go_model(model) and has_multimodal_content:
        raise ProxyError(
            400,
            f"OpenCode Go model '{model}' is text-only through /scillm today. "
            "Live `opencode models opencode-go --verbose` reports "
            "attachment=false and input.image=false/input.pdf=false for DeepSeek V4. "
            "Use model='vlm' for image/PDF work, or target a confirmed vision-capable provider.",
            "invalid_request_error",
        )
    if has_inline_data and not model_lower.startswith(("gemini", "text-gemini")):
        raise ProxyError(
            400,
            f"inlineData parts only work with Gemini models (text-gemini, text-gemini-3). "
            f"You used model='{model}'. For images with other models, use image_url format. "
            f"For PDFs with Claude, use document blocks. See SKILL.md 'Sending Multiple Files'.",
            "invalid_request_error",
        )

    # 10. Detect tool_choice: "required" with Codex — Codex rejects it
    tool_choice = body.get("tool_choice")
    if tool_choice == "required" and model_lower.startswith(("gpt-", "codex")):
        logger.warning(
            "tool_choice='required' not supported by Codex — forcing to 'auto'. "
            "Codex always uses auto tool selection."
        )
        body["tool_choice"] = "auto"


# ---------------------------------------------------------------------------
# Middleware loading
# ---------------------------------------------------------------------------


async def _load_middleware(config: ProxyConfig) -> list[BaseMiddleware]:
    """Instantiate middleware from config.

    Order matters:
      1. AbuseGuard — blocks clients after repeated 4xx errors (pre_call)
      2. CacheMiddleware — returns cached responses, dedupes in-flight (pre_call) — BEFORE concurrency
      3. TimeoutEstimator — queries /latency-stats, sets _dynamic_timeout_ms (pre_call)
      4. CallerPolicy — per-caller blast-radius controls (pre_call)
      5. ChutesRouter — selects least saturated Chutes model variant (pre_call)
      6. VlmRouter — rewrites model before routing (pre_call)
      7. ConcurrencyMiddleware — acquires provider semaphore (pre_call), releases (post_call/on_error)
      8. JsonGuard — validates JSON responses, repairs or raises (post_call)
      9. BudgetMiddleware — tracks spend, exposes via headers (post_call)
      10. CostHeaderMiddleware — injects x-cost-usd headers (post_call)
      11. ArangoLogMiddleware — logs to ArangoDB llm_call_log (post_call, on_error) — LAST

    All persistence uses ArangoDB (no Redis):
      - CacheMiddleware → scillm_response_cache
      - ConcurrencyMiddleware → scillm_concurrency_state
      - ArangoLogMiddleware → llm_call_log
    """
    from chutes.middleware.vlm_router import VlmRouter
    from chutes.middleware.concurrency_guard import ConcurrencyMiddleware
    from chutes.middleware.json_guard import JsonGuard
    from chutes.middleware.abuse_guard import AbuseGuardMiddleware
    from chutes.middleware.chutes_router import ChutesRouter

    middlewares: list[BaseMiddleware] = [AbuseGuardMiddleware()]

    # Cache middleware — returns cached responses, dedupes in-flight requests
    # Must be BEFORE ConcurrencyMiddleware so cache hits don't consume concurrency slots
    try:
        from chutes.middleware.cache_init import CacheMiddleware
        cache_mw = CacheMiddleware()
        await cache_mw.initialize()
        middlewares.append(cache_mw)
        logger.info("CacheMiddleware loaded (backend: {})", cache_mw._backend)
    except (ImportError, Exception) as exc:
        logger.debug("CacheMiddleware not loaded: {}", exc)

    # Timeout estimator — queries /latency-stats, sets _dynamic_timeout_ms
    try:
        from chutes.middleware.timeout_estimator import TimeoutEstimatorMiddleware
        middlewares.append(TimeoutEstimatorMiddleware())
        logger.info("TimeoutEstimatorMiddleware loaded")
    except (ImportError, Exception) as exc:
        logger.debug("TimeoutEstimatorMiddleware not loaded: {}", exc)

    if config.caller_profiles:
        try:
            from chutes.middleware.caller_policy import CallerPolicyMiddleware
            middlewares.append(CallerPolicyMiddleware(config))
            logger.info("CallerPolicyMiddleware loaded ({} profiles)", len(config.caller_profiles))
        except (ImportError, Exception) as exc:
            logger.debug("CallerPolicyMiddleware not loaded: {}", exc)

    # ChutesRouter selects best model variant based on utilization (before VlmRouter)
    middlewares.extend([ChutesRouter(), VlmRouter(), ConcurrencyMiddleware(), JsonGuard()])
    logger.info("ChutesRouter loaded (utilization-aware routing)")

    # Grounding guard — verifies response is grounded in provided source text
    # Supports course correction: retries with helpful error messages
    try:
        from chutes.middleware.grounding_guard import GroundingGuard
        middlewares.append(GroundingGuard())
        logger.info("GroundingGuard loaded (source grounding with course correction)")
    except (ImportError, Exception) as exc:
        logger.debug("GroundingGuard not loaded: {}", exc)

    # Schema guard — validates JSON against schema with course correction
    # Supports course correction: retries with detailed field-level errors
    try:
        from chutes.middleware.schema_guard import SchemaGuard
        middlewares.append(SchemaGuard())
        logger.info("SchemaGuard loaded (JSON schema validation with course correction)")
    except (ImportError, Exception) as exc:
        logger.debug("SchemaGuard not loaded: {}", exc)

    # Budget guard is optional — only loads if chutes env vars are set
    try:
        from chutes.middleware.budget_guard import BudgetMiddleware
        middlewares.append(BudgetMiddleware())
        logger.info("BudgetMiddleware loaded")
    except (ImportError, Exception) as exc:
        logger.debug("BudgetMiddleware not loaded: {}", exc)

    # Cost header middleware — injects x-cost-usd headers
    try:
        from chutes.middleware.pricing import CostHeaderMiddleware
        middlewares.append(CostHeaderMiddleware())
        logger.info("CostHeaderMiddleware loaded")
    except (ImportError, Exception) as exc:
        logger.debug("CostHeaderMiddleware not loaded: {}", exc)

    # ArangoDB logging — writes to llm_call_log for /learn-timeout and /orchestrate
    try:
        from chutes.middleware.arango_log import ArangoLogMiddleware
        middlewares.append(ArangoLogMiddleware())
        logger.info("ArangoLogMiddleware loaded")
    except (ImportError, Exception) as exc:
        logger.debug("ArangoLogMiddleware not loaded: {}", exc)

    # Active calls tracking — exposes in-flight requests for live monitoring.
    # It must register after ConcurrencyMiddleware acquires a provider slot so
    # active-calls and health semaphore state describe the same live lifecycle.
    try:
        from chutes.middleware.active_calls import ActiveCallsMiddleware, start_active_call_janitor
        active_mw = ActiveCallsMiddleware()
        start_active_call_janitor()
        insert_at = next(
            (
                index + 1
                for index, middleware in enumerate(middlewares)
                if type(middleware).__name__ == "ConcurrencyMiddleware"
            ),
            0,
        )
        middlewares.insert(insert_at, active_mw)
        logger.info("ActiveCallsMiddleware loaded")
    except (ImportError, Exception) as exc:
        logger.debug("ActiveCallsMiddleware not loaded: {}", exc)

    # Batch progress tracking — broadcasts to WebSocket subscribers
    try:
        from chutes.middleware.batch_ws import BatchProgressMiddleware
        middlewares.append(BatchProgressMiddleware())
        logger.info("BatchProgressMiddleware loaded (WebSocket batch tracking)")
    except (ImportError, Exception) as exc:
        logger.debug("BatchProgressMiddleware not loaded: {}", exc)

    return middlewares


# ---------------------------------------------------------------------------
# Chutes warm model auto-detection
# ---------------------------------------------------------------------------

CHUTES_API_BASE = os.environ.get("CHUTES_API_BASE", "https://llm.chutes.ai")
CHUTES_MGMT_API = "https://api.chutes.ai"


async def _find_warm_chutes_variant(model: str, client: httpx.AsyncClient, headers: dict) -> str | None:
    """Find a hot variant in the same model family.

    E.g., if model is deepseek-ai/DeepSeek-V3.1-TEE and V3.2-TEE is hot, return V3.2-TEE.
    Returns None if no better variant found or on error.
    """
    import re

    try:
        logger.debug("Checking warm variants for {}", model)
        # Get all available models from inference API
        resp = await client.get(f"{CHUTES_API_BASE}/v1/models", headers=headers)
        if resp.status_code != 200:
            logger.warning("Chutes /v1/models returned {}: {}", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        all_models = data.get("data", [])
        logger.debug("Got {} models from Chutes API", len(all_models))

        # Find the family using the same logic as ops-chutes find_family()
        org = model.split("/")[0]
        # Strip TEE suffix and version qualifiers to get core family
        base = re.sub(r"-TEE$", "", model)
        base_name = base.split("/")[-1]
        # Strip version suffixes: .1, .2, -0528, -Speciale, -Terminus, -FP8, -Instruct-2507
        core = re.sub(r"[-.](\\d+|Speciale|Terminus|FP8|Instruct|Thinking)(-\\d{4})?$", "", base_name)
        core = re.sub(r"\\.\\d+$", "", core)

        family = []
        for m in all_models:
            mid = m.get("id", "")
            if not mid.startswith(org + "/"):
                continue
            m_name = mid.split("/")[-1]
            m_stripped = re.sub(r"-TEE$", "", m_name)
            m_core = re.sub(r"[-.](\\d+|Speciale|Terminus|FP8|Instruct|Thinking)(-\\d{4})?$", "", m_stripped)
            m_core = re.sub(r"\\.\\d+$", "", m_core)
            if m_core == core:
                family.append(m)

        logger.debug("Found {} models in {} family", len(family), core)
        if not family:
            return None

        # Check each variant's hot status via management API
        hot_variants = []
        for m in family:
            mid = m["id"]
            chute_id = m.get("chute_id", "")
            if not chute_id:
                continue
            try:
                detail_resp = await client.get(f"{CHUTES_MGMT_API}/chutes/{chute_id}", headers=headers)
                if detail_resp.status_code == 200:
                    detail = detail_resp.json()
                    is_hot = detail.get("hot")
                    logger.debug("Model {} chute_id={} hot={}", mid, chute_id, is_hot)
                    if is_hot:
                        hot_variants.append(mid)
            except Exception as exc:
                logger.debug("Failed to get chute details for {}: {}", mid, exc)
                continue

        if not hot_variants:
            logger.debug("No hot variants found for family {}", core)
            return None

        # Prefer the originally requested model if it's hot
        if model in hot_variants:
            logger.debug("Configured model {} is already hot", model)
            return None  # Already using the right one

        # Return first hot variant (could add latency ranking later)
        logger.info("Found hot Chutes variant: {} (configured: {})", hot_variants[0], model)
        return hot_variants[0]

    except Exception as exc:
        logger.warning("Warm variant check failed: {}", exc)
        return None


async def _auto_select_warm_models(config: ProxyConfig, router) -> None:
    """Check Chutes models and switch to hot variants if available.

    Modifies router deployments in-place to use warm models.
    """
    logger.info("Checking for warm Chutes model variants...")
    chutes_base = os.environ.get("CHUTES_API_BASE", CHUTES_API_BASE)
    chutes_key = os.environ.get("CHUTES_API_KEY") or os.environ.get("CHUTES_API_TOKEN")
    if not chutes_key:
        logger.debug("No CHUTES_API_KEY — skipping warm model check")
        return

    headers = {"Authorization": f"Bearer {chutes_key}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        for group_name, group in config.model_groups.items():
            for dep in group.deployments:
                if not dep.api_base or chutes_base not in dep.api_base:
                    continue

                warm = await _find_warm_chutes_variant(dep.model, client, headers)
                if warm and warm != dep.model:
                    old_model = dep.model
                    dep.model = warm
                    logger.warning(
                        "AUTO-SWITCH: {} deployment {} → {} (hot)",
                        group_name, old_model, warm,
                    )


async def _warmup_chutes_models(config: ProxyConfig) -> None:
    """Call the Chutes warmup API for each Chutes deployment on startup.

    Runs in the background — doesn't block proxy readiness.
    Uses GET /chutes/warmup/{model_name}?quick=true which returns immediately
    and creates a bounty for miners to spin up instances.
    """
    chutes_base = os.environ.get("CHUTES_API_BASE", CHUTES_API_BASE)
    chutes_key = os.environ.get("CHUTES_API_KEY") or os.environ.get("CHUTES_API_TOKEN")
    if not chutes_key:
        logger.debug("No CHUTES_API_KEY — skipping warmup")
        return

    # Collect unique Chutes model names from config
    models_to_warm: set[str] = set()
    for group in config.model_groups.values():
        for dep in group.deployments:
            if dep.api_base and chutes_base in dep.api_base:
                models_to_warm.add(dep.model)

    if not models_to_warm:
        return

    headers = {"Authorization": f"Bearer {chutes_key}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        for model in models_to_warm:
            try:
                resp = await client.get(
                    f"{CHUTES_MGMT_API}/chutes/warmup/{model}",
                    params={"quick": "true"},
                    headers=headers,
                )
                if resp.status_code == 200:
                    logger.info("Chutes warmup sent for {} — miners notified", model)
                else:
                    logger.warning("Chutes warmup {} returned {}: {}", model, resp.status_code, resp.text[:200])
            except Exception as exc:
                logger.warning("Chutes warmup {} failed: {}", model, exc)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load config, init router and middleware on startup."""
    global _config, _router, _middleware_chain, _start_time, _embedding_client

    config_path = os.environ.get("CONFIG_FILE_PATH", "local/proxy_server_config.yaml")
    logger.info("Loading config from {}", config_path)
    _config = load_config(config_path)
    _router = Router(_config)
    _middleware_chain = MiddlewareChain(await _load_middleware(_config))
    _embedding_client = httpx.AsyncClient(base_url=EMBEDDING_SERVICE_URL, timeout=30.0)
    _start_time = time.monotonic()

    # Populate valid model aliases for request validation
    _VALID_MODEL_ALIASES.clear()
    _VALID_MODEL_ALIASES.update(_config.model_groups.keys())
    _VALID_MODEL_ALIASES.update(_config.aliases.keys())
    logger.debug("Valid model aliases: {}", sorted(_VALID_MODEL_ALIASES))

    # Validate env vars don't reference deprecated models
    _check_env_for_deprecated_models()

    # Load persisted concurrency backoff state from ArangoDB (survives restarts)
    from chutes.middleware.concurrency_guard import load_state_from_arango
    await load_state_from_arango()

    logger.info(
        "scillm proxy started — {} model groups, {} aliases, {} fallback chains",
        len(_config.model_groups),
        len(_config.aliases),
        len(_config.fallbacks),
    )

    # Auto-detect warm Chutes models and switch to them (runs at startup, blocks briefly)
    await _auto_select_warm_models(_config, _router)

    # Background warmup: fire cheap requests to cold-start-prone providers
    asyncio.create_task(_warmup_chutes_models(_config))

    yield
    # Graceful shutdown: drain in-flight requests before closing clients
    logger.info("scillm proxy shutting down — draining connections...")
    if _embedding_client:
        await _embedding_client.aclose()
    if _router:
        await _router.close()
    logger.info("scillm proxy shut down cleanly")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="scillm proxy", version="2.0.0", lifespan=lifespan)
app.add_exception_handler(ProxyError, proxy_error_handler)


@app.exception_handler(MiddlewareReject)
async def _reject_handler(request: Request, exc: MiddlewareReject):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": exc.message, "type": "middleware_reject", "code": exc.status_code}},
    )


# ---------------------------------------------------------------------------
# Prometheus metrics (optional)
# ---------------------------------------------------------------------------

_prom_available = False
try:
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

    _prom_available = True
except ImportError:
    logger.warning("prometheus_client not installed, /metrics disabled")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    """Simple health check — no auth required. Returns status and uptime."""
    uptime = time.monotonic() - _start_time if _start_time else 0
    return {
        "status": "ok" if _config is not None else "starting",
        "uptime_seconds": round(uptime, 1),
    }


@app.get("/health/liveliness")
async def health_liveliness():
    """Basic liveness probe for Docker healthcheck."""
    return {"status": "ok"}


@app.get("/health/readiness")
async def health_readiness():
    """Readiness probe — checks config loaded."""
    if _config is None:
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    return {"status": "ready", "model_groups": len(_config.model_groups)}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI-compatible chat completions endpoint."""
    # Request tracing
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())[:8])

    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")

    # Check if client is blocked for repeated bad requests (early rejection)
    try:
        from chutes.middleware.abuse_guard import check_client_blocked
        auth = request.headers.get("authorization", "")
        client_ip = request.client.host if request.client else "unknown"
        check_client_blocked(auth, client_ip)
    except ImportError:
        pass  # Abuse guard not loaded

    body = await request.json()
    model = body.get("model", "")
    messages = body.get("messages", [])
    stream = body.get("stream", False)

    if not model:
        raise ProxyError(400, "model is required", "invalid_request_error")
    if not messages:
        raise ProxyError(400, "messages is required", "invalid_request_error")

    # -------------------------------------------------------------------------
    # GUARDRAILS: Make common mistakes fail loudly or auto-fix
    # -------------------------------------------------------------------------
    _validate_model_request(model, body, request)

    if _middleware_chain is None or _router is None:
        raise ProxyError(503, "Proxy not ready — startup incomplete", "service_unavailable")

    # ── Caller identification (optional but recommended) ────────────────────────
    # X-Caller-Skill header helps trace requests and debug queue issues.
    # Warn if missing but don't reject — maintains OpenAI SDK compatibility.
    caller_skill = request.headers.get("x-caller-skill", "").strip()
    if not caller_skill:
        caller_skill = "unknown"
        logger.warning(
            "[{}] Missing X-Caller-Skill header — add headers={{'X-Caller-Skill': 'your-skill'}} "
            "for better debugging and cost tracking",
            request_id,
        )

    # Inject headers for middleware (arango_log.py uses x-caller-skill)
    body["_headers"] = dict(request.headers)
    body["_caller_skill"] = caller_skill
    start = time.monotonic()
    deadline_timeout_s = _request_deadline_timeout_s(body)

    # Pre-call middleware (can modify request or reject). If a later
    # pre_call hook rejects, earlier hooks such as ActiveCallsMiddleware and
    # ConcurrencyMiddleware must still see on_error and clean up.
    try:
        if deadline_timeout_s is not None and deadline_timeout_s > 0:
            remaining = (start + deadline_timeout_s) - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("request deadline expired before middleware")
            async with asyncio.timeout(remaining):
                body = await _middleware_chain.run_pre_call(body)
        else:
            body = await _middleware_chain.run_pre_call(body)
    except TimeoutError as exc:
        proxy_exc = ProxyError(
            504,
            f"Request timed out after {deadline_timeout_s}s before provider call completed",
            "timeout_error",
            details=_timeout_error_details(
                body,
                model=model,
                started_at=start,
                timeout_s=deadline_timeout_s or 0,
                final_provider_error=exc,
            ),
        )
        await _middleware_chain.run_on_error(body, proxy_exc)
        raise proxy_exc
    except BaseException as exc:
        await _middleware_chain.run_on_error(body, exc if isinstance(exc, Exception) else Exception(type(exc).__name__))
        raise
    model = body.get("model", model)
    messages = body.get("messages", messages)

    # Extract metadata AFTER pre_call but BEFORE routing (LLM never sees it).
    # Stash under private key so post_call middleware can access it.
    caller_metadata = body.pop("scillm_metadata", None)
    if caller_metadata is not None:
        logger.debug("scillm_metadata received: {}", caller_metadata)
        body["_scillm_metadata"] = caller_metadata  # for arango_log.py

    # Extract kwargs for the openai client
    kwargs: dict[str, Any] = {}
    for key in ("temperature", "max_tokens", "top_p", "frequency_penalty",
                "presence_penalty", "stop", "n", "response_format",
                "tools", "tool_choice", "seed", "logprobs", "top_logprobs"):
        if key in body:
            kwargs[key] = body[key]
    kwargs["stream"] = stream

    # Pass dynamic timeout from TimeoutEstimatorMiddleware to router
    if "_dynamic_timeout_ms" in body:
        kwargs["_dynamic_timeout_ms"] = body["_dynamic_timeout_ms"]
    if "_policy_max_timeout_s" in body:
        kwargs["_policy_max_timeout_s"] = body["_policy_max_timeout_s"]

    policy_deadline_timeout_s = _request_deadline_timeout_s(body)
    if deadline_timeout_s is None or (
        policy_deadline_timeout_s is not None
        and policy_deadline_timeout_s < deadline_timeout_s
    ):
        deadline_timeout_s = policy_deadline_timeout_s
    if deadline_timeout_s is not None and deadline_timeout_s > 0:
        kwargs["_deadline_started_at"] = start
        kwargs["_deadline_timeout_s"] = deadline_timeout_s
        kwargs["_deadline_at"] = start + deadline_timeout_s
        kwargs["_caller_skill"] = caller_skill
        kwargs["_scillm_metadata"] = caller_metadata if isinstance(caller_metadata, dict) else {}

    # Pass dynamic fallback chain from ChutesRouter (utilization-aware ordering)
    if "_dynamic_fallback_chain" in body:
        kwargs["_dynamic_fallback_chain"] = body["_dynamic_fallback_chain"]

    # -------------------------------------------------------------------------
    # BATCH RESUME: Check if this work item already completed successfully
    # -------------------------------------------------------------------------
    # Skills pass scillm_metadata: {"batch_id": "X", "item_id": "123"}
    # If found in llm_call_log with status=ok, return cached response
    if caller_metadata and not stream:
        try:
            from chutes.middleware.batch_resume import check_batch_resume
            cached_response = await check_batch_resume(caller_metadata)
            if cached_response:
                elapsed = time.monotonic() - start
                body["_cache_hit"] = True  # Skip arango_log for cache hits
                return JSONResponse(
                    content=cached_response,
                    headers={
                        "x-request-id": request_id,
                        "x-latency-ms": str(int(elapsed * 1000)),
                        "x-batch-resumed": "true",
                    },
                )
        except ImportError:
            pass  # batch_resume module not available

    # -------------------------------------------------------------------------
    # COURSE CORRECTION RETRY LOOP
    # Schema and Grounding guards may request retries with correction prompts.
    # Max 3 correction attempts to prevent infinite loops.
    # -------------------------------------------------------------------------
    MAX_CORRECTION_ATTEMPTS = 3
    correction_attempt = 0
    working_messages = list(messages)  # Copy to allow modification

    while True:
        try:
            if deadline_timeout_s is not None and deadline_timeout_s > 0:
                remaining = (start + deadline_timeout_s) - time.monotonic()
                if remaining <= 0:
                    details = _timeout_error_details(
                        body,
                        model=model,
                        started_at=start,
                        timeout_s=deadline_timeout_s,
                    )
                    raise ProxyError(
                        504,
                        f"Request timed out after {deadline_timeout_s}s before provider call completed",
                        "timeout_error",
                        details=details,
                    )
                async with asyncio.timeout(remaining):
                    result = await _router.complete(model, working_messages, **kwargs)
            else:
                result = await _router.complete(model, working_messages, **kwargs)

            if stream:
                # OAuth providers return AsyncIterator[bytes] (already SSE-formatted).
                # The openai SDK returns its own async stream type.
                if hasattr(result, "__aiter__") and not hasattr(result, "response"):
                    # Raw byte stream from OAuth providers — pipe directly
                    response = StreamingResponse(
                        result,
                        media_type="text/event-stream",
                        headers=SSE_HEADERS,
                    )
                else:
                    # OpenAI SDK async stream — use existing SSE wrapper
                    response = await stream_response(result, model=model)
                # Post-call middleware (observe only for streaming)
                await _middleware_chain.run_post_call(body, {"stream": True})
                return response
            else:
                # Non-streaming: result is a ChatCompletion object
                response_dict = result.model_dump()

                # Detect thinking-model token exhaustion:
                # finish_reason="length" with no visible content means the
                # provider spent its output budget internally without producing
                # an answer. OpenCode Go DeepSeek V4 can report thousands of
                # output tokens in this state, so do not require usage=0.
                usage = response_dict.get("usage") or {}
                if _is_empty_length_response(response_dict):
                    req_max = body.get("max_tokens", "unset")
                    total = usage.get("total_tokens", "?")
                    completion = usage.get("completion_tokens", "?")
                    raise ProxyError(
                        502,
                        f"Provider exhausted output budget with no visible response "
                        f"(max_tokens={req_max}, completion_tokens={completion}, "
                        f"total_tokens={total}). Use a shorter prompt, a stricter "
                        f"final-only JSON prompt, or a non-thinking model.",
                        "thinking_budget_exhausted",
                    )

                response_dict = await _middleware_chain.run_post_call(body, response_dict)

                # Echo opaque metadata back — LLM never saw it, can't fabricate it
                if caller_metadata is not None:
                    response_dict["scillm_metadata"] = caller_metadata

                elapsed = time.monotonic() - start

                # Extract cost headers stashed by CostHeaderMiddleware
                cost_headers = response_dict.pop("_cost_headers", {})
                # Extract timeout headers stashed by TimeoutEstimatorMiddleware
                timeout_headers = response_dict.pop("_timeout_headers", {})
                # Extract grounding headers stashed by GroundingGuard
                grounding_headers = response_dict.pop("_grounding_headers", {})

                resp_headers = {
                    "x-request-id": request_id,
                    "x-latency-ms": str(int(elapsed * 1000)),
                }
                resp_headers.update(cost_headers)
                resp_headers.update(timeout_headers)
                resp_headers.update(grounding_headers)

                return JSONResponse(
                    content=response_dict,
                    headers=resp_headers,
                )

        except asyncio.CancelledError as exc:
            await _middleware_chain.run_on_error(body, exc)
            raise
        except (ProxyError, MiddlewareReject) as exc:
            await _middleware_chain.run_on_error(body, exc)
            raise
        except RouterProxyError as exc:
            # Convert router's ProxyError to enriched ProxyError for LLM analysis
            error_type = "timeout_error" if exc.status_code == 504 else "router_error"
            proxy_exc = ProxyError(
                exc.status_code,
                exc.message,
                error_type,
                details=getattr(exc, "details", {}) or {},
            )
            await _middleware_chain.run_on_error(body, proxy_exc)
            raise proxy_exc
        except TimeoutError as exc:
            details = _timeout_error_details(
                body,
                model=model,
                started_at=start,
                timeout_s=deadline_timeout_s or 0,
                final_provider_error=exc,
            )
            proxy_exc = ProxyError(
                504,
                f"Request timed out after {deadline_timeout_s}s",
                "timeout_error",
                details=details,
            )
            await _middleware_chain.run_on_error(body, proxy_exc)
            raise proxy_exc
        except Exception as exc:
            # Import retry exceptions here to avoid circular import at module level
            from chutes.middleware.json_guard import JsonValidationFailed

            # Check for schema/grounding retry requests (course correction)
            try:
                from chutes.middleware.schema_guard import SchemaRetryNeeded, SchemaValidationFailed
                from chutes.middleware.grounding_guard import GroundingRetryNeeded
            except ImportError:
                SchemaRetryNeeded = None
                SchemaValidationFailed = None
                GroundingRetryNeeded = None

            # Handle Schema course correction retry
            if SchemaRetryNeeded and isinstance(exc, SchemaRetryNeeded):
                correction_attempt += 1
                if correction_attempt >= MAX_CORRECTION_ATTEMPTS:
                    logger.error(
                        "[{}] Schema validation max corrections exceeded ({})",
                        request_id, correction_attempt,
                    )
                    proxy_exc = ProxyError(
                        422,
                        f"Schema validation failed after {correction_attempt} correction attempts. "
                        f"Last correction hint: {exc.hint[:300]}",
                        "schema_validation_failed",
                    )
                    await _middleware_chain.run_on_error(body, proxy_exc)
                    raise proxy_exc
                logger.info(
                    "[{}] Schema validation failed, retrying with correction (attempt {})",
                    request_id, correction_attempt,
                )
                # Append correction prompt and retry
                working_messages = list(working_messages) + [
                    {"role": "user", "content": exc.hint}
                ]
                body["messages"] = working_messages
                continue  # Retry the completion

            # Handle Schema validation final failure
            if SchemaValidationFailed and isinstance(exc, SchemaValidationFailed):
                error_details = "; ".join(
                    e.get("message", str(e)) for e in exc.errors[:5]
                )
                proxy_exc = ProxyError(
                    422,
                    f"Schema validation failed: {error_details}",
                    "schema_validation_failed",
                    advice="Fix your prompt to return JSON matching the schema, or remove json_schema from request.",
                )
                await _middleware_chain.run_on_error(body, proxy_exc)
                raise proxy_exc

            # Handle Grounding course correction retry
            if GroundingRetryNeeded and isinstance(exc, GroundingRetryNeeded):
                correction_attempt += 1
                if correction_attempt >= MAX_CORRECTION_ATTEMPTS:
                    logger.error(
                        "[{}] Grounding max corrections exceeded ({})",
                        request_id, correction_attempt,
                    )
                    proxy_exc = ProxyError(
                        422,
                        f"Grounding verification failed after {correction_attempt} correction attempts. "
                        f"Response not sufficiently grounded in source text.",
                        "grounding_failed",
                    )
                    await _middleware_chain.run_on_error(body, proxy_exc)
                    raise proxy_exc
                logger.info(
                    "[{}] Grounding failed, retrying with correction (attempt {})",
                    request_id, correction_attempt,
                )
                # Append correction prompt and retry
                working_messages = list(working_messages) + [
                    {"role": "user", "content": exc.hint}
                ]
                body["messages"] = working_messages
                continue  # Retry the completion

            if isinstance(exc, JsonValidationFailed):
                logger.warning(
                    "[{}] JSON validation failed for model={}, signalling upstream error",
                    request_id,
                    model,
                )
                proxy_exc = ProxyError(
                    502,
                    f"JSON validation failed after repair: {exc.raw_text[:200]}",
                    "json_validation_error",
                )
                await _middleware_chain.run_on_error(body, proxy_exc)
                raise proxy_exc
            await _middleware_chain.run_on_error(body, exc)
            raise


@app.get("/v1/scillm/model-pools")
async def scillm_model_pools(request: Request):
    """List server-side model pools for concurrent batch routing."""
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")

    concurrency = {}
    try:
        from chutes.middleware.concurrency_guard import get_concurrency_status
        concurrency = get_concurrency_status()
    except ImportError:
        pass

    return {
        "pools": {
            name: {
                **pool,
                "lanes": [dict(lane) for lane in pool["lanes"]],
                "status": _model_pool_status(name, pool, concurrency),
            }
            for name, pool in _DEFAULT_MODEL_POOLS.items()
        }
    }


@app.get("/v1/scillm/model-pools/{pool_name}/status")
async def scillm_model_pool_status(request: Request, pool_name: str):
    """Return live aggregate and per-lane concurrency for one model pool."""
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")

    pool = _model_pool(pool_name)
    if pool is None:
        raise ProxyError(
            404,
            f"Unknown model_pool '{pool_name}'. Available: {', '.join(sorted(_DEFAULT_MODEL_POOLS))}.",
            "not_found",
        )

    concurrency = {}
    try:
        from chutes.middleware.concurrency_guard import get_concurrency_status
        concurrency = get_concurrency_status()
    except ImportError:
        pass

    return _model_pool_status(pool_name, pool, concurrency)


@app.post("/v1/scillm/batch/completions")
async def scillm_batch_completions(request: Request):
    """Run a batch across a weighted provider pool using ``asyncio.as_completed``."""
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")
    if _config is None or _router is None or _middleware_chain is None:
        raise ProxyError(503, "Proxy not ready — startup incomplete", "service_unavailable")

    body = await request.json()
    pool_name = str(body.get("model_pool") or body.get("model") or "qra-deepseek-pool")
    pool = _model_pool(pool_name)
    if pool is None:
        raise ProxyError(
            400,
            f"Unknown model_pool '{pool_name}'. Available: {', '.join(sorted(_DEFAULT_MODEL_POOLS))}.",
            "invalid_request_error",
        )

    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise ProxyError(400, "items must be a non-empty list", "invalid_request_error")
    max_items = int(body.get("max_items") or 500)
    if len(items) > max_items:
        raise ProxyError(400, f"batch has {len(items)} items; max_items is {max_items}", "invalid_request_error")

    batch_id = str(body.get("batch_id") or f"batch-{uuid.uuid4().hex[:12]}")
    lanes = pool["lanes"]
    lane_semaphores = {
        lane["name"]: asyncio.Semaphore(int(lane.get("max_concurrency") or 1))
        for lane in lanes
    }
    auth_header = request.headers.get("authorization", "")
    caller_skill = request.headers.get("x-caller-skill", "scillm-batch-pool")
    shared_params = {key: body[key] for key in _BATCH_FORWARD_KEYS if key in body}
    shared_params["stream"] = False
    total_started = time.monotonic()

    async def run_one(client: httpx.AsyncClient, item: dict[str, Any], index: int) -> dict[str, Any]:
        lane = _lane_for_index(lanes, index)
        item_id = _item_id(item, index)
        started = time.monotonic()
        async with lane_semaphores[lane["name"]]:
            try:
                payload = {
                    **shared_params,
                    **{key: item[key] for key in _BATCH_FORWARD_KEYS if key in item},
                    "model": lane["model"],
                    "messages": _messages_for_batch_item(item),
                    "_scillm_pool": pool_name,
                    "_scillm_pool_lane": lane["name"],
                }
                item_metadata = item.get("scillm_metadata") if isinstance(item.get("scillm_metadata"), dict) else {}
                payload["scillm_metadata"] = {
                    **item_metadata,
                    "batch_id": batch_id,
                    "item_id": item_id,
                    "model_pool": pool_name,
                    "lane": lane["name"],
                    "selected_model": lane["model"],
                    "provider": lane.get("provider"),
                }
                timeout = float(item.get("timeout") or body.get("timeout") or lane.get("timeout") or 300.0)
                payload["timeout"] = timeout
                response = await client.post(
                    "/v1/chat/completions",
                    headers={
                        "Authorization": auth_header,
                        "X-Caller-Skill": caller_skill,
                        "X-Scillm-Batch-Id": batch_id,
                        "X-Scillm-Batch-Total": str(len(items)),
                        "X-Scillm-Call-Key": item_id,
                    },
                    json=payload,
                    timeout=timeout,
                )
                response.raise_for_status()
                data = response.json()
                return {
                    "ok": True,
                    "item_id": item_id,
                    "index": index,
                    "lane": lane["name"],
                    "provider": lane.get("provider"),
                    "model": lane["model"],
                    "served_model": data.get("model"),
                    "content": data.get("choices", [{}])[0].get("message", {}).get("content"),
                    "response": data,
                    "latency_s": round(time.monotonic() - started, 2),
                }
            except httpx.HTTPStatusError as exc:
                error = str(exc)
                try:
                    error_body = exc.response.json()
                    if isinstance(error_body, dict) and isinstance(error_body.get("error"), dict):
                        error = error_body["error"].get("message") or error
                except Exception:
                    error = exc.response.text or error
                return {
                    "ok": False,
                    "item_id": item_id,
                    "index": index,
                    "lane": lane["name"],
                    "provider": lane.get("provider"),
                    "model": lane["model"],
                    "error": error,
                    "status_code": exc.response.status_code,
                    "latency_s": round(time.monotonic() - started, 2),
                }
            except Exception as exc:
                return {
                    "ok": False,
                    "item_id": item_id,
                    "index": index,
                    "lane": lane["name"],
                    "provider": lane.get("provider"),
                    "model": lane["model"],
                    "error": str(exc) or type(exc).__name__,
                    "error_type": type(exc).__name__,
                    "latency_s": round(time.monotonic() - started, 2),
                }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://scillm.internal") as client:
        tasks = [
            asyncio.create_task(run_one(client, item, index))
            for index, item in enumerate(items)
            if isinstance(item, dict)
        ]
        if len(tasks) != len(items):
            raise ProxyError(400, "each item must be an object", "invalid_request_error")
        results = []
        for task in asyncio.as_completed(tasks):
            results.append(await task)

    completed = sum(1 for result in results if result.get("ok"))
    failed = len(results) - completed
    return {
        "batch_id": batch_id,
        "model_pool": pool_name,
        "strategy": pool["strategy"],
        "lanes": lanes,
        "total": len(results),
        "completed": completed,
        "failed": failed,
        "elapsed_s": round(time.monotonic() - total_started, 2),
        "results": results,
        "ordered": False,
        "completion_order": "as_completed",
    }


@app.get("/v1/scillm/health")
async def scillm_health(request: Request):
    """Detailed health: router status, fallback config, uptime."""
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")

    if _config is None or _router is None:
        raise ProxyError(503, "Proxy not ready — startup incomplete", "service_unavailable")
    uptime = time.monotonic() - _start_time

    # Concurrency status (optional)
    concurrency = {}
    try:
        from chutes.middleware.concurrency_guard import get_concurrency_status
        concurrency = get_concurrency_status()
    except ImportError:
        pass

    # Abuse guard status (optional)
    abuse_guard = {}
    try:
        from chutes.middleware.abuse_guard import get_abuse_status
        abuse_guard = get_abuse_status()
    except ImportError:
        pass

    model_pools = {
        name: _model_pool_status(name, pool, concurrency)
        for name, pool in _DEFAULT_MODEL_POOLS.items()
    }

    return {
        "status": "ok",
        "uptime_seconds": round(uptime, 1),
        "model_groups": list(_config.model_groups.keys()),
        "fallbacks": _config.fallbacks,
        "retry_policy": {
            "internal_server_error": _config.retry_policy.internal_server_error,
            "rate_limit_error": _config.retry_policy.rate_limit_error,
            "timeout_error": _config.retry_policy.timeout_error,
        },
        "routing_strategy": _config.routing_strategy,
        "circuit_breaker": _router.circuit_status(),
        "concurrency": concurrency,
        "model_pools": model_pools,
        "abuse_guard": abuse_guard,
    }


@app.get("/v1/scillm/active-calls")
async def scillm_active_calls(request: Request):
    """Return list of currently in-flight LLM calls for live monitoring."""
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")

    try:
        from chutes.middleware.active_calls import get_active_calls, get_recent_stale_active_calls
        active = get_active_calls()
        stale = get_recent_stale_active_calls()
        return {
            "active": active,
            "live_in_flight": len(active),
            "stale_active_calls": len(stale),
            "stale": stale,
        }
    except ImportError:
        return {
            "active": [],
            "live_in_flight": 0,
            "stale_active_calls": 0,
            "stale": [],
            "error": "ActiveCallsMiddleware not loaded",
        }


@app.post("/v1/scillm/active-calls/purge")
async def scillm_active_calls_purge(
    request: Request,
    older_than_s: float | None = 600.0,
    caller: str | None = None,
    model_contains: str | None = None,
):
    """Purge stale active-call rows from the in-memory live registry."""
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")

    try:
        from chutes.middleware.active_calls import clear_active_calls
        purged = clear_active_calls(
            older_than_s=older_than_s,
            caller=caller,
            model_contains=model_contains,
        )
        return {"purged": len(purged), "active": purged}
    except ImportError:
        return {"purged": 0, "active": [], "error": "ActiveCallsMiddleware not loaded"}


@app.get("/v1/scillm/activity")
async def scillm_activity(request: Request):
    """Return activity graph data (last 5 minutes, bucketed for charting)."""
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")

    try:
        from chutes.middleware.active_calls import get_activity_graph
        return get_activity_graph()
    except ImportError:
        return {"buckets": [], "error": "ActiveCallsMiddleware not loaded"}


@app.get("/v1/scillm/concurrency")
async def scillm_concurrency(request: Request, model: str = "text"):
    """Get concurrency info for a model (for batch sizing).

    Skills call this to determine optimal chunk_size for batch processing.
    Returns effective_limit accounting for adaptive backoff from 429s.

    Example: GET /v1/scillm/concurrency?model=text
    Response: {"model": "text", "provider": "chutes", "chunk_size": 4, ...}
    """
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")

    try:
        from chutes.middleware.concurrency_guard import get_concurrency_for_model
        return get_concurrency_for_model(model)
    except ImportError:
        # Fallback if concurrency guard not loaded
        return {
            "model": model,
            "provider": "unknown",
            "chunk_size": 4,  # Safe default
            "error": "ConcurrencyGuard not loaded",
        }


@app.post("/v1/scillm/concurrency/reset")
async def scillm_concurrency_reset(request: Request, provider: str = ""):
    """Reset concurrency state for a provider or all providers.

    Use when batch failures have corrupted state or queue is stuck.
    Clears in-flight counters, queue depth, pauses, and backoff state.

    Example: POST /v1/scillm/concurrency/reset?provider=chutes
    Example: POST /v1/scillm/concurrency/reset  (reset all)
    """
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")

    try:
        from chutes.middleware.concurrency_guard import reset_concurrency
        result = reset_concurrency(provider if provider else None)
        return {
            "ok": True,
            "message": f"Reset concurrency for {len(result['providers_reset'])} providers",
            **result,
        }
    except ImportError:
        return {"ok": False, "error": "ConcurrencyGuard not loaded"}


@app.post("/v1/scillm/abuse-guard/reset")
async def scillm_abuse_guard_reset(request: Request):
    """Reset abuse guard — unblock all clients and clear error history.

    Use when a failed batch has incorrectly blocked your API key.
    Abuse guard blocks clients after 5+ 4xx errors in 30 seconds.

    Example: POST /v1/scillm/abuse-guard/reset
    """
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")

    try:
        from chutes.middleware.abuse_guard import reset_abuse_guard
        result = reset_abuse_guard()
        return result
    except ImportError:
        return {"ok": False, "error": "AbuseGuard not loaded"}


@app.get("/v1/scillm/models")
async def scillm_models(request: Request):
    """List model groups and aliases."""
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")

    if _config is None:
        raise ProxyError(503, "Proxy not ready — startup incomplete", "service_unavailable")
    groups = {}
    for name, group in _config.model_groups.items():
        groups[name] = {
            "deployments": len(group.deployments),
            "models": [d.model for d in group.deployments],
        }
    groups["embedding"] = {
        "deployments": 1,
        "models": ["all-MiniLM-L6-v2"],
        "endpoint": EMBEDDING_SERVICE_URL,
    }
    return {
        "groups": groups,
        "aliases": _config.aliases,
        "auto_providers": {
            "codex": {
                "model_prefixes": ["gpt-", "codex-", "o1", "o3", "o4"],
                "examples": ["gpt-5.5", "gpt-5.3-codex", "gpt-5.2-codex"],
                "key_configured": _codex_oauth_available(),
            },
            OPENCODE_GO_PROVIDER: {
                "model_prefix": "opencode-go/",
                "models_endpoint": "/v1/scillm/opencode-go/models",
                "key_configured": bool(_config.opencode_go_api_key),
            }
        },
    }


@app.get("/v1/scillm/capabilities")
async def scillm_capabilities(request: Request):
    """Read-only capability facts for callers, adapters, pools, and policies."""
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")
    if _config is None:
        raise ProxyError(503, "Proxy not ready", "service_unavailable")

    adapters = _load_adapter_manifests()
    model_groups: dict[str, dict[str, Any]] = {}
    for name, group in _config.model_groups.items():
        deployments = []
        support_items = []
        for dep in group.deployments:
            adapter_id = _adapter_id_for_deployment(dep, name)
            supports = _deployment_supports(dep, name, adapters)
            support_items.append(supports)
            deployments.append({
                "model": dep.model,
                "provider": adapter_id,
                "custom_llm_provider": dep.custom_llm_provider,
                "supports": supports,
                "timeout": dep.timeout,
            })
        model_groups[name] = {
            "supports": _merge_supports(support_items),
            "deployments": deployments,
            "fallbacks": _config.fallbacks.get(name, []),
        }

    pools = {}
    for name, pool in _DEFAULT_MODEL_POOLS.items():
        pools[name] = {
            "strategy": pool.get("strategy"),
            "description": pool.get("description"),
            "supports": {
                "batch": True,
                "text": True,
                "image": False,
                "pdf": False,
                "zip": False,
                "streaming": False,
                "tools": False,
            },
            "lanes": [dict(lane) for lane in pool.get("lanes", [])],
        }

    try:
        from chutes.middleware.caller_policy import caller_profiles_for_capabilities
        caller_profiles = caller_profiles_for_capabilities(_config)
    except Exception:
        caller_profiles = {}

    return {
        "version": 1,
        "model_groups": model_groups,
        "aliases": _config.aliases,
        "fallbacks": _config.fallbacks,
        "model_pools": pools,
        "adapters": adapters,
        "caller_profiles": caller_profiles,
    }


@app.get("/v1/scillm/opencode-go/models")
async def scillm_opencode_go_models(
    request: Request,
    source: str = "auto",
    refresh: bool = True,
    verbose: bool = False,
):
    """List OpenCode Go models available to scillm.

    ``source=auto`` defaults to live CLI refresh, then a running
    ``opencode serve`` instance, then the built-in registry.  ``source=server``,
    ``source=cli``, and ``source=static`` force one path.
    """
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")

    if _config is None:
        raise ProxyError(503, "Proxy not ready — startup incomplete", "service_unavailable")

    source = source.lower()
    if source not in {"auto", "server", "cli", "static"}:
        raise ProxyError(400, "source must be one of: auto, server, cli, static", "invalid_request_error")

    errors: list[str] = []
    models: list[str] = []
    actual_source = "static"

    if source in {"auto", "cli"}:
        try:
            models = await list_opencode_go_models_from_cli(refresh=refresh, verbose=verbose)
            actual_source = "cli"
        except Exception as exc:
            errors.append(f"cli: {exc}")
            if source == "cli":
                raise ProxyError(502, f"opencode models failed: {exc}", "provider_error")

    if not models and source in {"auto", "server"}:
        try:
            models = await list_opencode_go_models_from_server()
            if models:
                actual_source = "server"
        except Exception as exc:
            errors.append(f"server: {exc}")
            if source == "server":
                raise ProxyError(502, f"OpenCode server model listing failed: {exc}", "provider_error")

    if not models:
        models = static_opencode_go_models()
        actual_source = "static"

    key_configured = bool(_config.opencode_go_api_key)
    return {
        "provider": OPENCODE_GO_PROVIDER,
        "source": actual_source,
        "refresh": refresh,
        "verbose": verbose,
        "key_configured": key_configured,
        "model_count": len(models),
        "models": [describe_opencode_go_model(model, key_configured=key_configured) for model in models],
        "errors": errors,
    }


@app.get("/v1/models")
async def openai_models(request: Request):
    """OpenAI-compatible /v1/models endpoint."""
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")

    if _config is None:
        raise ProxyError(503, "Proxy not ready — startup incomplete", "service_unavailable")
    models = []
    for name in _config.model_groups:
        models.append({
            "id": name,
            "object": "model",
            "created": int(_start_time),
            "owned_by": "scillm",
        })
    for alias in _config.aliases:
        models.append({
            "id": alias,
            "object": "model",
            "created": int(_start_time),
            "owned_by": "scillm",
        })
    # Embedding model (served by local embedding service)
    models.append({
        "id": "embedding",
        "object": "model",
        "created": int(_start_time),
        "owned_by": "scillm",
    })

    # Auto-routable providers — these don't need config entries
    from scillm.proxy.providers.auth import is_anthropic_available, is_codex_available
    auto_models = []
    if is_anthropic_available():
        for m in ["claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5"]:
            auto_models.append({"id": m, "object": "model", "created": int(_start_time), "owned_by": "anthropic-oauth"})
    if is_codex_available():
        for m in ["gpt-5.5", "gpt-5.3-codex", "gpt-5.2-codex"]:
            auto_models.append({"id": m, "object": "model", "created": int(_start_time), "owned_by": "codex-oauth"})
    if _config.gemini_api_base:
        for m in ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-3-flash-preview", "gemini-3.1-pro-preview", "gemini-2.5-flash-lite"]:
            auto_models.append({"id": m, "object": "model", "created": int(_start_time), "owned_by": "gemini"})
    if _config.ollama_api_base:
        auto_models.append({"id": "ollama:*", "object": "model", "created": int(_start_time), "owned_by": "ollama-auto"})
    if _config.chutes_api_base:
        auto_models.append({"id": "chutes:Org/Model", "object": "model", "created": int(_start_time), "owned_by": "chutes-auto"})
    if _config.opencode_go_api_key:
        for model in static_opencode_go_models():
            auto_models.append({"id": model, "object": "model", "created": int(_start_time), "owned_by": OPENCODE_GO_PROVIDER})

    # Deduplicate (auto models might overlap with configured ones)
    existing_ids = {m["id"] for m in models}
    for m in auto_models:
        if m["id"] not in existing_ids:
            models.append(m)

    return {"object": "list", "data": models}


@app.get("/v1/scillm/providers")
async def scillm_providers(request: Request):
    """List all available providers and how to call them."""
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")
    if _config is None:
        raise ProxyError(503, "Proxy not ready", "service_unavailable")

    from scillm.proxy.providers.auth import is_anthropic_available, is_codex_available

    providers = {
        "configured": {
            name: {
                "models": [d.model for d in group.deployments],
                "api_base": group.deployments[0].api_base if group.deployments else None,
            }
            for name, group in _config.model_groups.items()
        },
        "auto_routing": {
            "claude": {
                "available": is_anthropic_available(),
                "pattern": "claude-*",
                "examples": ["claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5"],
                "auth": "OAuth via ~/.claude/.credentials.json",
                "note": "System prompt locked to Claude Code prefix",
            },
            "codex": {
                "available": is_codex_available(),
                "pattern": "gpt-* | codex-*",
                "examples": ["gpt-5.5", "gpt-5.3-codex", "gpt-5.2-codex"],
                "auth": "OAuth via ~/.codex/auth.json",
                "note": "temperature/max_tokens not supported",
            },
            "gemini": {
                "available": bool(_config.gemini_api_base),
                "pattern": "gemini-*",
                "examples": ["gemini-2.5-flash", "gemini-3-flash-preview", "gemini-3.1-pro-preview"],
                "auth": "API key",
                "note": "Supports inlineData for PDFs/images/ZIP",
            },
            "chutes": {
                "available": bool(_config.chutes_api_base),
                "pattern": "Org/Model (contains /)",
                "examples": ["Qwen/Qwen3-30B-A3B", "deepseek-ai/DeepSeek-V3"],
                "auth": "API key",
            },
            "ollama": {
                "available": bool(_config.ollama_api_base),
                "pattern": "model:tag or unknown names",
                "examples": ["qwen2.5:7b", "qwen3:0.6b", "llama3:8b"],
                "auth": "none (local)",
                "note": "response_format auto-stripped",
            },
        },
        "fallback_chains": _config.fallbacks,
    }
    return providers


@app.get("/v1/scillm/logs")
async def scillm_auth(request: Request):
    """Check OAuth token health for Claude and Codex providers."""
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")

    import time
    from scillm.proxy.providers.auth import (
        _read_claude_code_credentials,
        _read_codex_auth,
        _read_auth,
        CLAUDE_CODE_CREDENTIALS,
        CODEX_AUTH_FILE,
        AUTH_FILE,
    )

    now_ms = int(time.time() * 1000)
    result: dict = {"timestamp": now_ms}

    # Claude
    cc = _read_claude_code_credentials()
    if cc:
        expires = cc.get("expiresAt", 0)
        remaining_s = max(0, (expires - now_ms) // 1000)
        result["claude"] = {
            "status": "valid" if now_ms < expires else "expired",
            "source": str(CLAUDE_CODE_CREDENTIALS),
            "expires_in_s": remaining_s,
            "subscription": cc.get("subscriptionType", "unknown"),
            "rate_tier": cc.get("rateLimitTier", "unknown"),
        }
    else:
        # Check Pi fallback
        pi_data = _read_auth()
        pi_cred = pi_data.get("anthropic", {})
        if pi_cred.get("type") == "oauth":
            expires = pi_cred.get("expires", 0)
            remaining_s = max(0, (expires - now_ms) // 1000)
            result["claude"] = {
                "status": "valid" if now_ms < expires else "expired",
                "source": str(AUTH_FILE),
                "expires_in_s": remaining_s,
            }
        else:
            result["claude"] = {"status": "not_configured"}

    # Codex
    codex = _read_codex_auth()
    if codex:
        result["codex"] = {
            "status": "configured",
            "source": str(CODEX_AUTH_FILE),
            "account_id": (codex.get("account_id") or "")[:12] + "...",
        }
    else:
        pi_data = _read_auth() if "pi_data" not in dir() else pi_data
        pi_codex = pi_data.get("openai-codex", {})
        if pi_codex.get("type") == "oauth":
            expires = pi_codex.get("expires", 0)
            result["codex"] = {
                "status": "valid" if now_ms < expires else "expired",
                "source": str(AUTH_FILE),
                "expires_in_s": max(0, (expires - now_ms) // 1000),
            }
        else:
            result["codex"] = {"status": "not_configured"}

    return result


@app.get("/v1/scillm/auth")
async def scillm_auth_endpoint(request: Request):
    """Check OAuth token health. Alias for the auth check."""
    return await scillm_auth(request)


@app.get("/v1/scillm/logs")
async def scillm_logs(request: Request, date: str = "", limit: int = 100):
    """Query request logs. Returns cost summary + recent records.

    Usage: GET /v1/scillm/logs?date=2026-03-13&limit=50
    Default: today's date, last 100 records.
    """
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")

    from datetime import datetime, timezone
    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        from chutes.middleware.request_log import get_cost_summary
        return await get_cost_summary(date)
    except (ImportError, Exception) as exc:
        return {"error": f"Request logging not available: {exc}"}


@app.get("/v1/budget")
async def budget_snapshot(request: Request):
    """Chutes budget snapshot (delegated to budget_guard if available)."""
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")

    try:
        from chutes.middleware.budget_guard import get_budget_snapshot
        return get_budget_snapshot()
    except (ImportError, AttributeError):
        return {"status": "budget_guard_not_loaded"}


@app.get("/v1/scillm/debug/{call_id}")
async def debug_call_by_id(request: Request, call_id: str):
    """Analyze a specific call by its ID and return diagnosis with best practices.

    Returns structured JSON with:
    - analysis: LLM-generated diagnosis
    - call details (caller, model, status, timestamp)
    - best_practices_included: true

    Usage: GET /v1/scillm/debug/abc123
    """
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")

    try:
        from chutes.middleware.request_log import get_log_by_key, analyze_call

        log = await get_log_by_key(call_id)
        if not log:
            raise ProxyError(404, f"Call {call_id} not found", "not_found")

        return await analyze_call(log)
    except ProxyError:
        raise
    except Exception as exc:
        return {"success": False, "error": f"Debug failed: {exc}"}


@app.get("/v1/scillm/debug")
async def debug_recent_calls(request: Request, caller: str = "", limit: int = 1):
    """Analyze recent calls for a caller.

    Usage: GET /v1/scillm/debug?caller=pdf_oxide.clone&limit=3

    Returns analysis for the most recent `limit` calls from the specified caller.
    If caller is empty, returns instructions for usage.
    """
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")

    if not caller:
        return {
            "usage": "GET /v1/scillm/debug?caller=YOUR_SKILL_NAME&limit=1",
            "example": "GET /v1/scillm/debug?caller=pdf_oxide.clone&limit=3",
            "or": "GET /v1/scillm/debug/{call_id} for specific call",
        }

    try:
        from chutes.middleware.request_log import get_recent_logs_by_caller, analyze_call

        logs = await get_recent_logs_by_caller(caller, limit=min(limit, 10))
        if not logs:
            return {"caller": caller, "message": "No recent calls found", "analyses": []}

        analyses = []
        for log in logs:
            result = await analyze_call(log)
            analyses.append(result)

        return {"caller": caller, "count": len(analyses), "analyses": analyses}
    except Exception as exc:
        return {"success": False, "error": f"Debug failed: {exc}"}


# ---------------------------------------------------------------------------
# Batch status (for resume/retry workflows)
# ---------------------------------------------------------------------------


@app.get("/v1/scillm/batch/{batch_id}")
async def batch_status(request: Request, batch_id: str):
    """Get status of a batch job: success/failure counts and failed item IDs.

    Usage: GET /v1/scillm/batch/create-qras-cwe-20260413-123456

    Returns:
    {
        "batch_id": "...",
        "total": 969,
        "success": 950,
        "failed": 19,
        "failed_items": ["CWE-79", "CWE-89", ...]
    }

    Callers can use failed_items to retry only failed work items.
    """
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")

    try:
        from chutes.middleware.batch_resume import get_batch_status
        status = await get_batch_status(batch_id)
        return status
    except ImportError:
        return {"error": "batch_resume module not available"}
    except Exception as exc:
        return {"error": f"Failed to get batch status: {exc}"}


@app.websocket("/v1/scillm/ws/batch/{batch_id}")
async def websocket_batch(websocket: WebSocket, batch_id: str):
    """WebSocket endpoint for real-time batch progress notifications.

    Clients subscribe to receive progress events as calls in the batch complete.

    Events sent to client:
    - subscribed: Initial connection with current progress
    - call_complete: Each call completion with status, duration, cost
    - batch_complete: Final summary when all calls finish
    - ping: Keep-alive (every 30s)

    Client can send:
    - {"type": "set_total", "total": N}: Set expected total calls

    Headers for HTTP requests in the batch:
    - X-Scillm-Batch-Id: Batch identifier (required)
    - X-Scillm-Call-Key: Unique key for each call (optional, defaults to model)
    - X-Scillm-Batch-Total: Total expected calls (optional)

    Example usage (Python):
        import asyncio
        import websockets
        import json

        async def monitor_batch(batch_id):
            uri = f"ws://localhost:4001/v1/scillm/ws/batch/{batch_id}"
            async with websockets.connect(uri) as ws:
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    print(f"Progress: {data}")
                    if data.get("event") == "batch_complete":
                        break

        asyncio.run(monitor_batch("my-batch-123"))
    """
    # Note: WebSocket auth is optional — batch_id acts as the secret.
    # For production, consider requiring Bearer token in query param.
    try:
        from chutes.middleware.batch_ws import websocket_batch_handler
        await websocket_batch_handler(websocket, batch_id)
    except ImportError:
        await websocket.accept()
        await websocket.send_json({"error": "batch_ws module not available"})
        await websocket.close(code=1011, reason="module_not_available")


@app.get("/v1/scillm/ws/batch/{batch_id}/status")
async def websocket_batch_status(request: Request, batch_id: str):
    """REST endpoint to check batch progress (alternative to WebSocket).

    Returns current progress for a batch without subscribing.

    Usage: GET /v1/scillm/ws/batch/my-batch-123/status
    """
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")

    try:
        from chutes.middleware.batch_ws import get_batch_tracker
        tracker = get_batch_tracker()
        status = await tracker.get_status(batch_id)
        if status is None:
            return {"batch_id": batch_id, "status": "not_found"}
        return status
    except ImportError:
        return {"error": "batch_ws module not available"}


# ---------------------------------------------------------------------------
# Embeddings (proxied to local embedding service)
# ---------------------------------------------------------------------------


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    """OpenAI-compatible embeddings endpoint.

    Translates to the local embedding service at EMBEDDING_SERVICE_URL
    and returns results in OpenAI format.
    """
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")

    body = await request.json()
    raw_input = body.get("input")
    if raw_input is None:
        raise ProxyError(400, "input is required", "invalid_request_error")

    # Normalize input to list of strings (OpenAI spec allows string or list)
    if isinstance(raw_input, str):
        texts = [raw_input]
    elif isinstance(raw_input, list):
        texts = [str(t) for t in raw_input]
    else:
        raise ProxyError(400, "input must be a string or list of strings", "invalid_request_error")

    if not texts:
        raise ProxyError(400, "input must not be empty", "invalid_request_error")

    if _embedding_client is None:
        raise ProxyError(503, "Proxy not ready — startup incomplete", "service_unavailable")

    try:
        resp = await _embedding_client.post("/embed/batch", json={"texts": texts})
        resp.raise_for_status()
    except httpx.ConnectError:
        raise ProxyError(502, "Embedding service unreachable", "upstream_error")
    except httpx.HTTPStatusError as exc:
        raise ProxyError(
            502,
            f"Embedding service returned {exc.response.status_code}",
            "upstream_error",
        )
    except httpx.TimeoutException:
        raise ProxyError(502, "Embedding service timed out", "upstream_error")

    result = resp.json()
    vectors = result.get("vectors", [])
    model_name = result.get("model", "unknown")

    data = [
        {"object": "embedding", "index": i, "embedding": vec}
        for i, vec in enumerate(vectors)
    ]

    return JSONResponse(content={
        "object": "list",
        "data": data,
        "model": model_name,
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    })


# ---------------------------------------------------------------------------
# Prometheus /metrics endpoint
# ---------------------------------------------------------------------------


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    if not _prom_available:
        raise ProxyError(404, "prometheus_client not installed", "not_found_error")
    from starlette.responses import Response

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Catch-all for unknown routes
# ---------------------------------------------------------------------------


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def catch_all(path: str):
    """Return 404 for unrecognized routes."""
    raise ProxyError(404, f"Unknown endpoint: /{path}", "not_found_error")
