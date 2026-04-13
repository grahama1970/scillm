"""scillm.proxy.errors — OpenAI-compatible error mapping for the scillm proxy.

Maps openai SDK exceptions to structured JSON error responses with correct
HTTP status codes, retry classification, and FastAPI exception handling.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import openai
from loguru import logger
from pydantic import BaseModel, ValidationError
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.requests import Request


class ErrorAnalysis(BaseModel):
    """Validated schema for LLM error analysis response."""

    advice: str
    recommendation: str | None = None


class ProxyError(Exception):
    """Base proxy exception carrying HTTP status, message, and error type."""

    __slots__ = ("status_code", "message", "error_type", "advice", "call_id")

    def __init__(
        self,
        status_code: int,
        message: str,
        error_type: str,
        advice: str | None = None,
        call_id: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.error_type = error_type
        self.advice = advice or _get_advice_for_error(error_type, message)
        self.call_id = call_id
        super().__init__(message)

    def to_dict(self) -> dict:
        """Return OpenAI-format error dict with actionable advice."""
        result = {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "code": self.status_code,
            }
        }
        # Include advice for agents — they won't remember to ask for help
        if self.advice:
            result["error"]["advice"] = self.advice
            result["error"]["skill"] = "/best-practices-scillm"
        if self.call_id:
            result["error"]["debug_url"] = f"http://localhost:4001/v1/scillm/debug/{self.call_id}"
        return result


# ---------------------------------------------------------------------------
# Advice mapping — actionable guidance for common errors
# ---------------------------------------------------------------------------

_ERROR_ADVICE: dict[str, str] = {
    "rate_limit_error": (
        "Rate limited. For batches, use CHUNK_SIZE=4 instead of asyncio.gather(*all_tasks). "
        "The proxy queues excess requests but times out after 60s."
    ),
    "timeout_error": (
        "Request timed out. Use timeout=60-120s for LLM calls. "
        "If batch processing, reduce concurrency with CHUNK_SIZE=4."
    ),
    "authentication_error": (
        "Auth failed. Use 'Authorization: Bearer sk-dev-proxy-123' header. "
        "Check that the scillm proxy is running on :4001."
    ),
    "connection_error": (
        "Connection failed. Verify scillm proxy is running: "
        "docker compose -p scillm -f deploy/docker/compose.scillm.core.yml up -d"
    ),
    "invalid_request_error": (
        "Bad request. Common issues: missing 'messages' field, using 'max_tokens' (don't), "
        "or wrong model name. Check /v1/scillm/models for valid models."
    ),
    "server_error": (
        "Provider error (5xx). The proxy retries automatically. If persistent, "
        "try a different model or check /v1/scillm/health for provider status."
    ),
    "router_error": (
        "All providers failed after exhausting fallbacks. Check /v1/scillm/health for provider status. "
        "If batch processing, use CHUNK_SIZE=4 to reduce concurrent load on providers."
    ),
}

_MESSAGE_ADVICE: list[tuple[str, str]] = [
    ("queue timeout", "Batch too large. Use CHUNK_SIZE=4 loop instead of firing all requests at once."),
    ("no instances available", "Model cold on Chutes. Proxy will cascade to fallback. Wait 60s for warmup."),
    ("context length", "Prompt too long. Reduce input size or use text-gemini (1M context)."),
    ("content policy", "Content filtered. Rephrase the prompt to avoid policy triggers."),
    ("json", "JSON parsing failed. Add response_format: {type: 'json_object'} for structured output."),
]


def _get_advice_for_error(error_type: str, message: str) -> str | None:
    """Return actionable advice based on error type and message."""
    # Check message patterns first (more specific)
    msg_lower = message.lower()
    for pattern, advice in _MESSAGE_ADVICE:
        if pattern in msg_lower:
            return advice

    # Fall back to error type advice
    return _ERROR_ADVICE.get(error_type)


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
# FastAPI exception handler with LLM-powered analysis
# ---------------------------------------------------------------------------

_ANALYSIS_CALLER = "scillm.error-analyzer"


# ---------------------------------------------------------------------------
# Deterministic error diagnosis (no LLM needed for obvious cases)
# ---------------------------------------------------------------------------

_BATCH_CHUNK_SNIPPET = """CHUNK_SIZE = 4
results = []
for i in range(0, len(prompts), CHUNK_SIZE):
    chunk = prompts[i:i + CHUNK_SIZE]
    chunk_results = await asyncio.gather(*(call_proxy(p) for p in chunk))
    results.extend(chunk_results)"""

_TIMEOUT_SNIPPET = """response = httpx.post(
    url,
    headers=headers,
    json=payload,
    timeout=60.0,
)"""


def _deterministic_diagnosis(
    error_type: str,
    error_message: str,
    is_batch_error: bool,
    *,
    has_x_caller_skill: bool | None = None,
    used_proxy_base_url: bool | None = None,
    client_timeout_seconds: float | None = None,
    included_max_tokens: bool | None = None,
) -> ErrorAnalysis | None:
    """Handle obvious error cases deterministically. Returns None if ambiguous.

    Optional observability fields allow diagnosing anti-patterns when known:
    - has_x_caller_skill: whether X-Caller-Skill header was present
    - used_proxy_base_url: whether client used proxy URL vs direct provider
    - client_timeout_seconds: the timeout the client used
    - included_max_tokens: whether max_tokens was in the request
    """
    msg = error_message.lower()

    # Priority 1: Connection refused / proxy not running
    if "connection refused" in msg or "econnrefused" in msg or ":4001" in msg or "failed to connect" in msg:
        return ErrorAnalysis(
            advice="Start the scillm proxy or verify that port 4001 is reachable.",
            recommendation=None,
        )

    # Priority 2: Auth errors
    if "401" in msg or "unauthorized" in msg or "invalid token" in msg or "missing bearer" in msg or "forbidden" in msg:
        return ErrorAnalysis(
            advice="Send the expected bearer token to the proxy.",
            recommendation=None,
        )

    # Priority 3: Observable anti-patterns (when facts are known)
    if used_proxy_base_url is False:
        return ErrorAnalysis(
            advice="Point the client at the scillm proxy base URL instead of calling the provider directly.",
            recommendation='client = OpenAI(base_url="http://127.0.0.1:4001/v1", api_key="sk-dev-proxy-123")',
        )

    if has_x_caller_skill is False:
        return ErrorAnalysis(
            advice="Add the X-Caller-Skill header so proxy failures can be traced back to the calling skill.",
            recommendation='headers = {"Authorization": "Bearer sk-dev-proxy-123", "X-Caller-Skill": "your-skill-name"}',
        )

    if included_max_tokens is True:
        return ErrorAnalysis(
            advice="Omit max_tokens from the proxy request to avoid truncation-related failures.",
            recommendation='payload = {"model": model_name, "messages": messages}  # no max_tokens',
        )

    # Priority 4: Batch pressure / rate limits / queue timeout
    if is_batch_error or any(p in msg for p in ("queue timeout", "429", "rate limit", "too many", "concurrent", "overloaded")):
        return ErrorAnalysis(
            advice="Reduce concurrency and process requests in chunks of 4.",
            recommendation=_BATCH_CHUNK_SNIPPET,
        )

    # Priority 5: Client timeout (observable or inferred from message)
    if client_timeout_seconds is not None and client_timeout_seconds < 60:
        return ErrorAnalysis(
            advice="Increase the client timeout to 60 seconds, or 120 seconds for large batches.",
            recommendation=_TIMEOUT_SNIPPET,
        )
    if "timeout" in msg and "queue" not in msg:
        return ErrorAnalysis(
            advice="Increase the client timeout to 60 seconds, or 120 seconds for large batches.",
            recommendation=_TIMEOUT_SNIPPET,
        )

    # Priority 6: Empty response
    if "empty response" in msg or "no content" in msg or "blank response" in msg or "null output" in msg:
        return ErrorAnalysis(
            advice="Retry once, then simplify the prompt or try a different model.",
            recommendation=None,
        )

    # Ambiguous — let LLM handle
    return None


def _should_use_llm_analyzer(error_type: str, error_message: str) -> bool:
    """Return True if the error is ambiguous enough to warrant LLM analysis."""
    text = f"{error_type} {error_message}".lower()

    deterministic_patterns = [
        "401",
        "unauthorized",
        "econnrefused",
        "connection refused",
        ":4001",
        "429",
        "rate limit",
        "queue timeout",
    ]
    return not any(p in text for p in deterministic_patterns)


# System prompt for LLM analyzer (used only for ambiguous cases)
_LLM_SYSTEM_PROMPT = """You are the scillm proxy error analyzer.

Your job is to diagnose one failed scillm call and return the single most useful fix.

You must use only the information explicitly provided in the user message.
Do not invent missing facts.
Do not assume implementation details that are not stated.
Do not list multiple competing diagnoses.
Pick the single best diagnosis and the single best fix.

Your response must be valid JSON matching exactly this schema:
{
  "advice": "string",
  "recommendation": "string or null"
}

Rules:
- "advice" must be exactly one sentence.
- "recommendation" must be either a short Python code snippet as a plain string, or null.
- Do not use markdown fences.
- Do not include any keys other than "advice" and "recommendation".
- If there is not enough evidence for a code-level fix, set "recommendation" to null.

Diagnosis policy:
1. Prefer explicit error-message evidence over inference.
2. If the message mentions connection refused, ECONNREFUSED, failed to connect, or port 4001, diagnose that the proxy is not running or not reachable.
3. If the message mentions 401, unauthorized, invalid token, missing bearer, or forbidden auth, diagnose bad or missing proxy authentication.
4. If the message mentions queue timeout, 429, rate limit, too many requests, concurrency, overloaded, or if batch-related is true, diagnose excessive concurrency / batch pressure.
5. If the message mentions timeout but does not indicate queueing, 429, or concurrency, diagnose client timeout too short.
6. If the message mentions empty response, blank response, no content, or null output, diagnose that the model returned no usable output.
7. Otherwise, diagnose insufficient evidence for a precise root cause and give conservative next-step advice.

Known fixes:
- Excessive concurrency / batch pressure:
  advice: Reduce concurrency and process requests in chunks of 4.
  recommendation: return a short Python snippet that batches async calls with CHUNK_SIZE = 4.

- Client timeout too short:
  advice: Increase the client timeout to 60 seconds, or 120 seconds for large batches.
  recommendation: return a short Python snippet showing timeout=60.0.

- Proxy not running or not reachable:
  advice: Start the scillm proxy or verify that port 4001 is reachable.
  recommendation: null

- Bad or missing proxy authentication:
  advice: Send the expected bearer token to the proxy.
  recommendation: null

- No usable model output:
  advice: Retry once, then simplify the prompt or try a different model.
  recommendation: null

- Insufficient evidence:
  advice: The failure is not specific enough to diagnose precisely; inspect the full proxy error and request metadata.
  recommendation: null

Code snippet requirements:
- Return plain Python only, as a JSON string value.
- Keep it short and directly copy-pasteable.
- Do not mention facts or numbers that were not provided, except CHUNK_SIZE = 4 and timeout=60.0 which are approved defaults."""


def _build_llm_user_prompt(
    error_type: str,
    error_message: str,
    caller: str,
    model_requested: str | None,
    is_batch_error: bool,
    *,
    has_x_caller_skill: bool | None = None,
    used_proxy_base_url: bool | None = None,
    client_timeout_seconds: float | None = None,
    included_max_tokens: bool | None = None,
) -> str:
    """Build the user prompt for LLM error analysis.

    Optional observability fields are included when known, allowing the LLM
    to diagnose anti-patterns that are otherwise invisible from error text alone.
    """
    lines = [
        "A scillm call just failed. Diagnose the failure and provide the single best fix.",
        "",
        "Error details:",
        f"- Type: {error_type}",
        f"- Message: {error_message}",
        f"- Caller: {caller}",
        f"- Model: {model_requested or 'unknown'}",
        f"- Batch-related: {is_batch_error}",
    ]

    # Include observable facts when known
    if has_x_caller_skill is not None:
        lines.append(f"- X-Caller-Skill header present: {has_x_caller_skill}")
    if used_proxy_base_url is not None:
        lines.append(f"- Used proxy base URL: {used_proxy_base_url}")
    if client_timeout_seconds is not None:
        lines.append(f"- Client timeout: {client_timeout_seconds}s")
    if included_max_tokens is not None:
        lines.append(f"- Included max_tokens: {included_max_tokens}")

    lines.extend([
        "",
        "Return valid JSON only using exactly this schema:",
        "{",
        '  "advice": "string",',
        '  "recommendation": "string or null"',
        "}",
    ])

    return "\n".join(lines)


async def _analyze_error_with_llm(
    error_type: str,
    error_message: str,
    caller: str,
    model_requested: str | None,
    *,
    has_x_caller_skill: bool | None = None,
    used_proxy_base_url: bool | None = None,
    client_timeout_seconds: float | None = None,
    included_max_tokens: bool | None = None,
) -> ErrorAnalysis | None:
    """Analyze error and provide fix. Tries deterministic rules first, LLM for ambiguous cases.

    Returns ErrorAnalysis with 'advice' (str) and 'recommendation' (str or None).
    Returns None if analysis fails (falls back to static advice).

    Optional observability fields enable anti-pattern diagnosis when facts are known.
    """
    import json
    import httpx

    is_batch_error = any(
        pattern in error_message.lower()
        for pattern in ("queue timeout", "rate limit", "429", "too many", "concurrent", "overloaded")
    )

    # Try deterministic diagnosis first — no LLM needed for obvious cases
    deterministic = _deterministic_diagnosis(
        error_type,
        error_message,
        is_batch_error,
        has_x_caller_skill=has_x_caller_skill,
        used_proxy_base_url=used_proxy_base_url,
        client_timeout_seconds=client_timeout_seconds,
        included_max_tokens=included_max_tokens,
    )
    if deterministic:
        return deterministic

    # Check if LLM analysis would add value
    if not _should_use_llm_analyzer(error_type, error_message):
        return None

    # Ambiguous case — use LLM for analysis
    user_prompt = _build_llm_user_prompt(
        error_type,
        error_message,
        caller,
        model_requested,
        is_batch_error,
        has_x_caller_skill=has_x_caller_skill,
        used_proxy_base_url=used_proxy_base_url,
        client_timeout_seconds=client_timeout_seconds,
        included_max_tokens=included_max_tokens,
    )

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "http://127.0.0.1:4001/v1/chat/completions",
                headers={
                    "Authorization": "Bearer sk-dev-proxy-123",
                    "X-Caller-Skill": _ANALYSIS_CALLER,
                },
                json={
                    "model": "text-gemini",
                    "messages": [
                        {"role": "system", "content": _LLM_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.0,  # Deterministic for classification
                    "response_format": {"type": "json_object"},
                },
                timeout=10.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                try:
                    result = json.loads(content)
                    return ErrorAnalysis.model_validate(result)
                except (json.JSONDecodeError, ValidationError) as e:
                    logger.debug("LLM response validation failed: {}", e)
                    # Try to salvage advice from raw content
                    return ErrorAnalysis(advice=content[:200], recommendation=None)
    except Exception as e:
        logger.debug("Error analysis failed (using static advice): {}", e)

    return None


async def proxy_error_handler(request: Request, exc: ProxyError) -> JSONResponse:
    """FastAPI/Starlette exception handler for ``ProxyError``.

    Register via ``app.add_exception_handler(ProxyError, proxy_error_handler)``.

    For non-analysis requests, makes a quick LLM call to provide specific
    guidance — prevents agents from repeating the same mistake 900 times.
    """
    caller_skill = request.headers.get("x-caller-skill", "unknown")
    logger.warning(
        "ProxyError {status} on {method} {path} (caller={caller}): {msg}",
        status=exc.status_code,
        method=request.method,
        path=request.url.path,
        caller=caller_skill,
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

    # Get error response dict
    error_dict = exc.to_dict()

    # LLM-powered analysis for non-analysis requests (prevent infinite loops)
    if caller_skill != _ANALYSIS_CALLER and exc.status_code >= 400:
        try:
            # Extract observable facts from request for anti-pattern diagnosis
            model_requested = None
            included_max_tokens = None
            try:
                body = await request.json()
                model_requested = body.get("model")
                included_max_tokens = "max_tokens" in body
            except Exception:
                pass

            # Observable facts we can derive from the request
            has_x_caller_skill = caller_skill != "unknown"

            # Check if deterministic path would handle this (for analysis type label)
            is_batch_error = any(
                pattern in exc.message.lower()
                for pattern in ("queue timeout", "rate limit", "429", "too many", "concurrent", "overloaded")
            )
            deterministic_result = _deterministic_diagnosis(
                exc.error_type,
                exc.message,
                is_batch_error,
                has_x_caller_skill=has_x_caller_skill,
                included_max_tokens=included_max_tokens,
            )

            analysis = await _analyze_error_with_llm(
                exc.error_type,
                exc.message,
                caller_skill or "unknown",
                model_requested,
                has_x_caller_skill=has_x_caller_skill,
                included_max_tokens=included_max_tokens,
            )
            if analysis:
                error_dict["error"]["advice"] = analysis.advice
                error_dict["error"]["recommendation"] = analysis.recommendation
                error_dict["error"]["analysis"] = "deterministic" if deterministic_result else "llm"
        except Exception as e:
            logger.debug("LLM analysis skipped: {}", e)

    return JSONResponse(status_code=exc.status_code, content=error_dict)
