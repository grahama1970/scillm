"""scillm.proxy.errors — OpenAI-compatible error mapping for the scillm proxy.

Maps openai SDK exceptions to structured JSON error responses with correct
HTTP status codes, retry classification, and FastAPI exception handling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import openai
from loguru import logger
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.requests import Request


class ProxyError(Exception):
    """Base proxy exception carrying HTTP status, message, and error type."""

    __slots__ = ("status_code", "message", "error_type")

    def __init__(self, status_code: int, message: str, error_type: str) -> None:
        self.status_code = status_code
        self.message = message
        self.error_type = error_type
        super().__init__(message)

    def to_dict(self) -> dict:
        """Return OpenAI-format error dict."""
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "code": self.status_code,
            }
        }


# ---------------------------------------------------------------------------
# Exception classification
# ---------------------------------------------------------------------------

_OPENAI_ERROR_MAP: list[tuple[type[Exception], int, str]] = [
    (openai.AuthenticationError, 401, "authentication_error"),
    (openai.PermissionDeniedError, 403, "permission_error"),
    (openai.NotFoundError, 404, "not_found_error"),
    (openai.RateLimitError, 429, "rate_limit_error"),
    (openai.BadRequestError, 400, "invalid_request_error"),
    (openai.InternalServerError, 500, "server_error"),
    (openai.APITimeoutError, 504, "timeout_error"),
    (openai.APIConnectionError, 502, "connection_error"),
]


def classify_openai_error(exc: Exception) -> ProxyError:
    """Map an openai SDK exception (or any exception) to a ``ProxyError``."""
    for exc_type, status, error_type in _OPENAI_ERROR_MAP:
        if isinstance(exc, exc_type):
            msg = str(exc)
            logger.debug("Classified {} as {} ({})", type(exc).__name__, error_type, status)
            return ProxyError(status, msg, error_type)

    # Catch-all for other openai.APIStatusError subclasses
    if isinstance(exc, openai.APIStatusError):
        logger.warning("Unmapped APIStatusError (status={}): {}", exc.status_code, exc)
        return ProxyError(exc.status_code, str(exc), "api_error")

    # Anything else is a 500
    logger.error("Unhandled exception: {}", exc)
    return ProxyError(500, str(exc), "server_error")


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_RETRYABLE_TYPES = (
    openai.RateLimitError,
    openai.InternalServerError,
    openai.APITimeoutError,
    openai.APIConnectionError,
)

# Chutes-specific: 503 "No instances available" means cold model, don't retry
# (retrying won't help — miners need minutes to spin up, cascade to fallback instead)
_COLD_MODEL_PATTERNS = (
    "no instances available",
    "no workers available",
    "model not loaded",
)


def _is_cold_model_error(exc: Exception) -> bool:
    """Detect Chutes cold model errors that should skip retries."""
    msg = str(exc).lower()
    return any(pattern in msg for pattern in _COLD_MODEL_PATTERNS)


def is_retryable(exc: Exception) -> bool:
    """Return True if the error is worth retrying (5xx, 429, timeout, connection).

    Exception: Chutes "No instances available" (503) is NOT retryable because
    miners need minutes to spin up. Better to cascade to fallback immediately.
    """
    # Fast-fail cold model errors — cascade to fallback instead of retrying
    if _is_cold_model_error(exc):
        logger.warning("Cold model detected ({}), skipping retries → fallback", str(exc)[:80])
        return False

    if isinstance(exc, _RETRYABLE_TYPES):
        return True
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code in _RETRYABLE_STATUS_CODES
    if isinstance(exc, ProxyError):
        return exc.status_code in _RETRYABLE_STATUS_CODES
    return False


# Mapping from error types to retry-policy category names
_RETRY_CATEGORY_MAP: list[tuple[type[Exception], str]] = [
    (openai.RateLimitError, "rate_limit_error"),
    (openai.APITimeoutError, "timeout_error"),
    (openai.AuthenticationError, "authentication_error"),
    (openai.BadRequestError, "bad_request_error"),
]


def get_retry_category(exc: Exception) -> str:
    """Return the retry-policy category for router retry-count lookup.

    Returns one of: ``"internal_server_error"``, ``"rate_limit_error"``,
    ``"timeout_error"``, ``"authentication_error"``, ``"bad_request_error"``,
    ``"content_policy_violation_error"``.
    """
    for exc_type, category in _RETRY_CATEGORY_MAP:
        if isinstance(exc, exc_type):
            return category

    # Content-policy violations surface as BadRequestError with specific messages
    if isinstance(exc, openai.BadRequestError) and "content" in str(exc).lower():
        return "content_policy_violation_error"

    # 5xx / connection errors default to internal_server_error
    if isinstance(exc, (openai.InternalServerError, openai.APIConnectionError)):
        return "internal_server_error"

    if isinstance(exc, openai.APIStatusError) and exc.status_code >= 500:
        return "internal_server_error"

    return "internal_server_error"


# ---------------------------------------------------------------------------
# FastAPI exception handler
# ---------------------------------------------------------------------------


async def proxy_error_handler(request: Request, exc: ProxyError) -> JSONResponse:
    """FastAPI/Starlette exception handler for ``ProxyError``.

    Register via ``app.add_exception_handler(ProxyError, proxy_error_handler)``.
    """
    logger.warning(
        "ProxyError {status} on {method} {path}: {msg}",
        status=exc.status_code,
        method=request.method,
        path=request.url.path,
        msg=exc.message,
    )

    # Track 4xx errors for abuse detection
    if 400 <= exc.status_code < 500:
        try:
            from chutes.middleware.abuse_guard import _track_client_error
            auth = request.headers.get("authorization", "")
            client_ip = request.client.host if request.client else "unknown"
            _track_client_error(auth, client_ip, exc.status_code)
        except ImportError:
            pass  # Abuse guard not loaded

    return JSONResponse(status_code=exc.status_code, content=exc.to_dict())
