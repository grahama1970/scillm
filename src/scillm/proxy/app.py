"""FastAPI application for the scillm proxy.

Thin OpenAI-compatible proxy that routes through middleware chain → router → streaming.
Replaces litellm's proxy_server.py (9,532 lines) with ~350 lines.
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger

from scillm.proxy.config import ProxyConfig, load_config
from scillm.proxy.errors import ProxyError, proxy_error_handler
from scillm.proxy.middleware import BaseMiddleware, MiddlewareChain, MiddlewareReject
from scillm.proxy.router import Router
from scillm.proxy.streaming import collect_response, stream_response

# ---------------------------------------------------------------------------
# Globals (populated during lifespan)
# ---------------------------------------------------------------------------
_config: ProxyConfig | None = None
_router: Router | None = None
_middleware_chain: MiddlewareChain | None = None
_start_time: float = 0.0


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
# Middleware loading
# ---------------------------------------------------------------------------


def _load_middleware(config: ProxyConfig) -> list[BaseMiddleware]:
    """Instantiate middleware from config.

    Order matters:
      1. VlmRouter — rewrites model before routing (pre_call)
      2. ConcurrencyMiddleware — acquires provider semaphore (pre_call), releases (post_call/on_error)
      3. JsonGuard — validates JSON responses, repairs or raises (post_call)
      4. BudgetMiddleware — tracks spend, exposes via headers (post_call)
    """
    from chutes.middleware.vlm_router import VlmRouter
    from chutes.middleware.concurrency_guard import ConcurrencyMiddleware
    from chutes.middleware.json_guard import JsonGuard

    middlewares: list[BaseMiddleware] = [VlmRouter(), ConcurrencyMiddleware(), JsonGuard()]

    # Budget guard is optional — only loads if chutes env vars are set
    try:
        from chutes.middleware.budget_guard import BudgetMiddleware
        middlewares.append(BudgetMiddleware())
        logger.info("BudgetMiddleware loaded")
    except (ImportError, Exception) as exc:
        logger.debug("BudgetMiddleware not loaded: {}", exc)

    return middlewares


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load config, init router and middleware on startup."""
    global _config, _router, _middleware_chain, _start_time

    config_path = os.environ.get("CONFIG_FILE_PATH", "local/proxy_server_config.yaml")
    logger.info("Loading config from {}", config_path)
    _config = load_config(config_path)
    _router = Router(_config)
    _middleware_chain = MiddlewareChain(_load_middleware(_config))
    _start_time = time.monotonic()

    logger.info(
        "scillm proxy started — {} model groups, {} aliases, {} fallback chains",
        len(_config.model_groups),
        len(_config.aliases),
        len(_config.fallbacks),
    )
    yield
    # Graceful shutdown: drain in-flight requests before closing clients
    logger.info("scillm proxy shutting down — draining connections...")
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

try:
    from prometheus_client import make_asgi_app as _make_prom_app

    _metrics_app = _make_prom_app()
    app.mount("/metrics", _metrics_app)
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

    body = await request.json()
    model = body.get("model", "")
    messages = body.get("messages", [])
    stream = body.get("stream", False)

    if not model:
        raise ProxyError(400, "model is required", "invalid_request_error")
    if not messages:
        raise ProxyError(400, "messages is required", "invalid_request_error")

    if _middleware_chain is None or _router is None:
        raise ProxyError(503, "Proxy not ready — startup incomplete", "service_unavailable")

    # Pre-call middleware (can modify request or reject)
    body = await _middleware_chain.run_pre_call(body)
    model = body.get("model", model)
    messages = body.get("messages", messages)

    # Extract kwargs for the openai client
    kwargs: dict[str, Any] = {}
    for key in ("temperature", "max_tokens", "top_p", "frequency_penalty",
                "presence_penalty", "stop", "n", "response_format",
                "tools", "tool_choice", "seed", "logprobs", "top_logprobs"):
        if key in body:
            kwargs[key] = body[key]
    kwargs["stream"] = stream

    start = time.monotonic()

    try:
        result = await _router.complete(model, messages, **kwargs)

        if stream:
            response = await stream_response(result)
            # Post-call middleware (observe only for streaming)
            await _middleware_chain.run_post_call(body, {"stream": True})
            return response
        else:
            # Non-streaming: result is a ChatCompletion object
            response_dict = result.model_dump()
            response_dict = await _middleware_chain.run_post_call(body, response_dict)
            elapsed = time.monotonic() - start
            return JSONResponse(
                content=response_dict,
                headers={
                    "x-request-id": request_id,
                    "x-latency-ms": str(int(elapsed * 1000)),
                },
            )

    except (ProxyError, MiddlewareReject):
        raise
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
    }


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
    return {"object": "list", "data": models}


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


# ---------------------------------------------------------------------------
# Catch-all for unknown routes
# ---------------------------------------------------------------------------


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def catch_all(path: str):
    """Return 404 for unrecognized routes."""
    raise ProxyError(404, f"Unknown endpoint: /{path}", "not_found_error")
