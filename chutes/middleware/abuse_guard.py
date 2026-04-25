"""Client abuse guard - blocks clients after repeated bad requests.

Tracks 4xx errors per client and temporarily blocks after threshold.
Prevents runaway scripts from spamming the proxy with invalid requests.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Dict

from loguru import logger

from scillm.proxy.middleware import BaseMiddleware, MiddlewareReject

# Config
MAX_ERRORS_BEFORE_BLOCK = 5      # Block after 5 consecutive errors
BLOCK_DURATION_S = 60            # Block for 60 seconds
ERROR_WINDOW_S = 30              # Errors must occur within 30s window

# State: track errors per client
_client_errors: Dict[str, list] = defaultdict(list)  # client_id -> [(timestamp, error_code)]
_blocked_clients: Dict[str, float] = {}               # client_id -> unblock_time


def _get_client_id(request: dict) -> str:
    """Extract client identifier from request."""
    # Try API key first (most specific)
    auth = request.get("_auth_header", "")
    if auth.startswith("Bearer "):
        key = auth[7:]
        # Use first 16 chars of key as ID
        return f"key:{key[:16]}" if len(key) > 16 else f"key:{key}"
    # Fall back to IP if no auth
    return f"ip:{request.get('_client_ip', 'unknown')}"


def _get_client_id_from_headers(auth: str, client_ip: str) -> str:
    """Extract client identifier from raw headers."""
    if auth.startswith("Bearer "):
        key = auth[7:]
        return f"key:{key[:16]}" if len(key) > 16 else f"key:{key}"
    return f"ip:{client_ip}"


def _track_client_error(auth: str, client_ip: str, status_code: int) -> None:
    """Track a 4xx error for a client (called from error handler)."""
    if status_code == 429:
        return  # Rate limits are provider-side, don't blame the client
    if not (400 <= status_code < 500):
        return

    client_id = _get_client_id_from_headers(auth, client_ip)
    now = time.monotonic()
    errors = _client_errors[client_id]

    # Add this error
    errors.append((now, status_code))

    # Prune old errors outside window
    cutoff = now - ERROR_WINDOW_S
    _client_errors[client_id] = [(t, s) for t, s in errors if t > cutoff]
    errors = _client_errors[client_id]

    # Check if threshold exceeded
    if len(errors) >= MAX_ERRORS_BEFORE_BLOCK:
        _blocked_clients[client_id] = now + BLOCK_DURATION_S
        logger.warning(
            "abuse_guard: blocking {} for {}s after {} errors in {}s",
            client_id, BLOCK_DURATION_S, len(errors), ERROR_WINDOW_S,
        )


class AbuseGuardMiddleware(BaseMiddleware):
    """Blocks clients after repeated 4xx errors.

    DISABLED for batch workloads: authenticated callers (with master key) are
    legitimate. Abuse guard causes more problems than it solves when a batch
    has errors — it blocks the client, causing ALL subsequent requests to fail.
    """

    async def pre_call(self, request: dict) -> dict | None:
        # DISABLED: Let all requests through. Authenticated callers are legitimate.
        # Abuse guard was blocking batch callers after transient errors.
        return request

        # --- Original logic (disabled) ---
        client_id = _get_client_id(request)
        now = time.monotonic()

        # Check if client is blocked
        if client_id in _blocked_clients:
            unblock_time = _blocked_clients[client_id]
            if now < unblock_time:
                remaining = int(unblock_time - now)
                logger.warning(
                    "abuse_guard: {} blocked for {}s more (repeated bad requests)",
                    client_id, remaining,
                )
                raise MiddlewareReject(
                    f"Too many invalid requests. Blocked for {remaining}s. "
                    "Common causes: (1) invalid model name - use 'text', 'text-gemini', or 'vlm'; "
                    "(2) deprecated model in env var - scillm handles model selection now; "
                    "(3) malformed request body. Check /v1/scillm/health for available models.",
                    status_code=429,
                )
            else:
                # Block expired
                del _blocked_clients[client_id]
                _client_errors.pop(client_id, None)

        request["_abuse_guard_client_id"] = client_id
        return request

    async def post_call(self, request: dict, response: Any) -> Any:
        # Success - clear error history
        client_id = request.get("_abuse_guard_client_id")
        if client_id and client_id in _client_errors:
            _client_errors.pop(client_id, None)
        return response

    async def on_error(self, request: dict, error: Exception) -> None:
        client_id = request.get("_abuse_guard_client_id")
        if not client_id:
            return

        # Check if this is a client error (4xx) - but NOT 429 (rate limits aren't client's fault)
        status = getattr(error, "status_code", 0) or getattr(error, "status", 0)
        if status == 429:
            return  # Rate limits are provider-side, don't blame the client
        if not (400 <= status < 500):
            return  # Only track client errors

        now = time.monotonic()
        errors = _client_errors[client_id]

        # Add this error
        errors.append((now, status))

        # Prune old errors outside window
        cutoff = now - ERROR_WINDOW_S
        _client_errors[client_id] = [(t, s) for t, s in errors if t > cutoff]
        errors = _client_errors[client_id]

        # Check if threshold exceeded
        if len(errors) >= MAX_ERRORS_BEFORE_BLOCK:
            _blocked_clients[client_id] = now + BLOCK_DURATION_S
            logger.warning(
                "abuse_guard: blocking {} for {}s after {} errors in {}s",
                client_id, BLOCK_DURATION_S, len(errors), ERROR_WINDOW_S,
            )


def check_client_blocked(auth: str, client_ip: str) -> None:
    """Check if client is blocked. Raises MiddlewareReject if blocked.

    DISABLED: Authenticated callers are legitimate. Abuse guard was blocking
    batch callers after transient provider errors.
    """
    return  # DISABLED - let all requests through

    # --- Original logic (disabled) ---
    client_id = _get_client_id_from_headers(auth, client_ip)
    now = time.monotonic()

    if client_id in _blocked_clients:
        unblock_time = _blocked_clients[client_id]
        if now < unblock_time:
            remaining = int(unblock_time - now)
            logger.warning(
                "abuse_guard: {} blocked for {}s more",
                client_id, remaining,
            )
            raise MiddlewareReject(
                f"Too many invalid requests. Blocked for {remaining}s. "
                "Common causes: (1) invalid model name - use 'text', 'text-gemini', or 'vlm'; "
                "(2) deprecated model in env var - scillm handles model selection now; "
                "(3) malformed request body. Check /v1/scillm/health for available models.",
                status_code=429,
            )
        else:
            # Block expired
            del _blocked_clients[client_id]
            _client_errors.pop(client_id, None)


def get_abuse_status() -> Dict[str, Any]:
    """Return current abuse guard state for health endpoint."""
    now = time.monotonic()
    blocked = {
        cid: int(unblock - now)
        for cid, unblock in _blocked_clients.items()
        if unblock > now
    }
    return {
        "blocked_clients": len(blocked),
        "clients_with_errors": len(_client_errors),
        "blocked": blocked,
    }


def reset_abuse_guard() -> Dict[str, Any]:
    """Clear all blocked clients and error history. Returns stats before reset."""
    now = time.monotonic()
    blocked_count = len([c for c, t in _blocked_clients.items() if t > now])
    error_count = len(_client_errors)

    _blocked_clients.clear()
    _client_errors.clear()

    return {
        "ok": True,
        "clients_unblocked": blocked_count,
        "error_history_cleared": error_count,
    }
