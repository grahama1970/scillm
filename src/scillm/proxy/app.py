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
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger

from scillm.proxy.config import ProxyConfig, load_config
from scillm.proxy.errors import ProxyError, proxy_error_handler
from scillm.proxy.middleware import BaseMiddleware, MiddlewareChain, MiddlewareReject
from scillm.proxy.router import Router
from scillm.proxy.router import ProxyError as RouterProxyError
from scillm.proxy.streaming import SSE_HEADERS, collect_response, stream_response
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

# Known good model aliases (checked at startup from config)
_VALID_MODEL_ALIASES: set[str] = set()


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
    if _VALID_MODEL_ALIASES and model not in _VALID_MODEL_ALIASES:
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
    if "max_tokens" in body:
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
            f"tool_choice='required' not supported by Codex — forcing to 'auto'. "
            f"Codex always uses auto tool selection."
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
      4. ChutesRouter — selects least saturated Chutes model variant (pre_call)
      5. VlmRouter — rewrites model before routing (pre_call)
      6. ConcurrencyMiddleware — acquires provider semaphore (pre_call), releases (post_call/on_error)
      7. JsonGuard — validates JSON responses, repairs or raises (post_call)
      8. BudgetMiddleware — tracks spend, exposes via headers (post_call)
      9. CostHeaderMiddleware — injects x-cost-usd headers (post_call)
      10. ArangoLogMiddleware — logs to ArangoDB llm_call_log (post_call, on_error) — LAST

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

    # ChutesRouter selects best model variant based on utilization (before VlmRouter)
    middlewares.extend([ChutesRouter(), VlmRouter(), ConcurrencyMiddleware(), JsonGuard()])
    logger.info("ChutesRouter loaded (utilization-aware routing)")

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

    # Active calls tracking — exposes in-flight requests for live monitoring
    try:
        from chutes.middleware.active_calls import ActiveCallsMiddleware
        middlewares.insert(0, ActiveCallsMiddleware())  # First in chain to track all calls
        logger.info("ActiveCallsMiddleware loaded")
    except (ImportError, Exception) as exc:
        logger.debug("ActiveCallsMiddleware not loaded: {}", exc)

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

    # ── Caller identification enforcement ─────────────────────────────────────
    # Require X-Caller-Skill header to identify who's making requests.
    # Without this, zombie requests can't be traced and queue issues can't be debugged.
    caller_skill = request.headers.get("x-caller-skill", "").strip()
    if not caller_skill:
        raise ProxyError(
            400,
            "Missing X-Caller-Skill header. "
            "Add `headers={'X-Caller-Skill': 'your-skill-name'}` to your request. "
            "This identifies your caller for debugging and cost tracking. "
            "Example: httpx.post(..., headers={'X-Caller-Skill': 'my-skill'})",
            "missing_caller_header",
        )

    # Inject headers for middleware (arango_log.py uses x-caller-skill)
    body["_headers"] = dict(request.headers)

    # Pre-call middleware (can modify request or reject)
    body = await _middleware_chain.run_pre_call(body)
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

    # Pass dynamic fallback chain from ChutesRouter (utilization-aware ordering)
    if "_dynamic_fallback_chain" in body:
        kwargs["_dynamic_fallback_chain"] = body["_dynamic_fallback_chain"]

    start = time.monotonic()

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

    try:
        result = await _router.complete(model, messages, **kwargs)

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
            # content=null + completion_tokens=0 + finish_reason="length"
            # means internal reasoning consumed the entire max_tokens budget.
            choices = response_dict.get("choices", [])
            usage = response_dict.get("usage") or {}
            if (
                choices
                and choices[0].get("finish_reason") == "length"
                and usage.get("completion_tokens", -1) == 0
                and choices[0].get("message", {}).get("content") is None
            ):
                req_max = body.get("max_tokens", "unset")
                total = usage.get("total_tokens", "?")
                raise ProxyError(
                    502,
                    f"Thinking model exhausted token budget — 0 visible tokens "
                    f"produced (max_tokens={req_max}, total_tokens={total}). "
                    f"Increase max_tokens or use a non-thinking model.",
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

            resp_headers = {
                "x-request-id": request_id,
                "x-latency-ms": str(int(elapsed * 1000)),
            }
            resp_headers.update(cost_headers)
            resp_headers.update(timeout_headers)

            return JSONResponse(
                content=response_dict,
                headers=resp_headers,
            )

    except (ProxyError, MiddlewareReject):
        raise
    except RouterProxyError as exc:
        # Convert router's ProxyError to enriched ProxyError for LLM analysis
        raise ProxyError(exc.status_code, exc.message, "router_error")
    except Exception as exc:
        # Import here to avoid circular import at module level
        from chutes.middleware.json_guard import JsonValidationFailed

        if isinstance(exc, JsonValidationFailed):
            logger.warning(
                "[{}] JSON validation failed for model={}, signalling upstream error",
                request_id,
                model,
            )
            raise ProxyError(
                502,
                f"JSON validation failed after repair: {exc.raw_text[:200]}",
                "json_validation_error",
            )
        await _middleware_chain.run_on_error(body, exc)
        raise


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
        "abuse_guard": abuse_guard,
    }


@app.get("/v1/scillm/active-calls")
async def scillm_active_calls(request: Request):
    """Return list of currently in-flight LLM calls for live monitoring."""
    auth_err = _check_auth(request)
    if auth_err:
        raise ProxyError(401, auth_err, "authentication_error")

    try:
        from chutes.middleware.active_calls import get_active_calls
        return {"active": get_active_calls()}
    except ImportError:
        return {"active": [], "error": "ActiveCallsMiddleware not loaded"}


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
    return {"groups": groups, "aliases": _config.aliases}


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
        for m in ["gpt-5.3-codex", "gpt-5.2-codex"]:
            auto_models.append({"id": m, "object": "model", "created": int(_start_time), "owned_by": "codex-oauth"})
    if _config.gemini_api_base:
        for m in ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-3-flash-preview", "gemini-3.1-pro-preview", "gemini-2.5-flash-lite"]:
            auto_models.append({"id": m, "object": "model", "created": int(_start_time), "owned_by": "gemini"})
    if _config.ollama_api_base:
        auto_models.append({"id": "ollama:*", "object": "model", "created": int(_start_time), "owned_by": "ollama-auto"})
    if _config.chutes_api_base:
        auto_models.append({"id": "chutes:Org/Model", "object": "model", "created": int(_start_time), "owned_by": "chutes-auto"})

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
                "examples": ["gpt-5.3-codex", "gpt-5.2-codex"],
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
