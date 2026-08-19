"""Model group router with fallback cascade, per-exception retry, and circuit breaker.

Routes completion requests through model groups with priority-ordered
fallbacks. Each deployment gets a lazily-created, cached AsyncOpenAI client.
Retries are governed by exception type using the config's RetryPolicy.

Circuit breaker: tracks consecutive failures per group. After `allowed_fails`
consecutive failures, the group enters cooldown for `cooldown_time` seconds.
During cooldown, the group is skipped and the next fallback is tried directly.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import base64
import io
import mimetypes
import zipfile
from urllib.parse import urlparse

import os

import httpx
import openai
from loguru import logger

from scillm.proxy.config import Deployment, ModelGroup, ProxyConfig, RetryPolicy
from scillm.proxy.errors import ProviderOAuthError
# Bifrost removed — direct provider routing via openai SDK
from scillm.proxy.providers.auth import is_anthropic_available, is_codex_available
from scillm.proxy.providers.claude import claude_completion, claude_completion_stream
from scillm.proxy.providers.codex import codex_completion, codex_completion_stream
from scillm.proxy.providers.codex_models import discover_codex_models, resolve_codex_model
from scillm.proxy.providers.opencode_go import (
    ENDPOINT_CHAT_COMPLETIONS,
    ENDPOINT_MESSAGES,
    OPENCODE_GO_CHAT_TIMEOUT_SEC,
    OPENCODE_GO_DEFAULT_API_BASE,
    OPENCODE_GO_MESSAGES_TIMEOUT_SEC,
    is_opencode_go_model,
    opencode_go_endpoint_type,
    opencode_go_messages_completion,
    opencode_go_messages_stream,
    opencode_go_model_id,
)

# Gemini OAuth: uses Gemini CLI for headless calls (bypasses 20 RPD API limit)
from chutes.middleware.gemini_oauth import complete_messages as gemini_oauth_complete

# Chutes warmup: called when a Chutes model returns 5xx (cold start)
_CHUTES_API_BASE = os.environ.get("CHUTES_API_BASE", "https://llm.chutes.ai")
_CHUTES_MGMT_API = "https://api.chutes.ai"
_chutes_warmup_pending: set[str] = set()  # models already being warmed (avoid spam)


def _trigger_chutes_warmup(model: str) -> None:
    """Fire-and-forget warmup call to Chutes management API.

    Called when a Chutes model returns 5xx (likely cold). The warmup API
    notifies miners to spin up instances. Non-blocking — runs in background.
    """
    if model in _chutes_warmup_pending:
        return
    _chutes_warmup_pending.add(model)

    async def _do_warmup() -> None:
        try:
            key = os.environ.get("CHUTES_API_KEY") or os.environ.get("CHUTES_API_TOKEN")
            if not key:
                return
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{_CHUTES_MGMT_API}/chutes/warmup/{model}",
                    params={"quick": "true"},
                    headers={"Authorization": f"Bearer {key}"},
                )
                if resp.status_code == 200:
                    logger.info("Chutes warmup triggered for cold model {} — miners notified", model)
                else:
                    logger.warning("Chutes warmup {} returned {}", model, resp.status_code)
        except Exception as exc:
            logger.debug("Chutes warmup {} failed: {}", model, exc)
        finally:
            # Allow re-warmup after 60s
            await asyncio.sleep(60)
            _chutes_warmup_pending.discard(model)

    asyncio.create_task(_do_warmup())


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProxyError(Exception):
    """Terminal error raised when all retries and fallbacks are exhausted."""

    def __init__(self, status_code: int, message: str, details: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self.message = message
        self.details = details or {}
        super().__init__(f"[{status_code}] {message}")


# ---------------------------------------------------------------------------
# Circuit breaker state
# ---------------------------------------------------------------------------


class _CircuitState:
    """Per-group circuit breaker state."""

    __slots__ = ("consecutive_failures", "cooldown_until", "total_failures", "total_successes")

    def __init__(self) -> None:
        self.consecutive_failures: int = 0
        self.cooldown_until: float = 0.0
        self.total_failures: int = 0
        self.total_successes: int = 0

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.cooldown_until = 0.0
        self.total_successes += 1

    def record_failure(self, allowed_fails: int, cooldown_time: int) -> None:
        self.consecutive_failures += 1
        self.total_failures += 1
        if self.consecutive_failures >= allowed_fails:
            self.cooldown_until = time.monotonic() + cooldown_time
            logger.warning(
                "circuit_breaker: opened after {} consecutive failures, cooldown {}s",
                self.consecutive_failures,
                cooldown_time,
            )

    def is_open(self) -> bool:
        """True if circuit is open (in cooldown)."""
        if self.cooldown_until <= 0:
            return False
        if time.monotonic() >= self.cooldown_until:
            # Half-open: allow a probe
            self.cooldown_until = 0.0
            self.consecutive_failures = 0
            logger.info("circuit_breaker: half-open, allowing probe")
            return False
        return True

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        if self.cooldown_until > now:
            state = "open"
            remaining = round(self.cooldown_until - now, 1)
        elif self.consecutive_failures > 0:
            state = "half-open"
            remaining = 0
        else:
            state = "closed"
            remaining = 0
        return {
            "state": state,
            "consecutive_failures": self.consecutive_failures,
            "cooldown_remaining_s": remaining,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
        }


# ---------------------------------------------------------------------------
# Exception → retry-count mapping
# ---------------------------------------------------------------------------

_STATUS_MAP: dict[type[Exception], str] = {
    openai.InternalServerError: "internal_server_error",
    openai.RateLimitError: "rate_limit_error",
    openai.APITimeoutError: "timeout_error",
    openai.AuthenticationError: "authentication_error",
    openai.BadRequestError: "bad_request_error",
}

_STATUS_CODE_MAP: dict[type[Exception], int] = {
    openai.InternalServerError: 500,
    openai.RateLimitError: 429,
    openai.APITimeoutError: 504,
    openai.AuthenticationError: 401,
    openai.BadRequestError: 400,
}


def _max_retries_for(exc: Exception, policy: RetryPolicy) -> int:
    """Return the allowed retry count for an exception type."""
    for exc_type, attr in _STATUS_MAP.items():
        if isinstance(exc, exc_type):
            return getattr(policy, attr)
    # 404 = model not found — never retry
    if isinstance(exc, openai.NotFoundError):
        return 0
    # Unknown openai API errors get the internal_server_error budget
    if isinstance(exc, openai.APIStatusError):
        return policy.internal_server_error
    return 0


def _status_code_for(exc: Exception) -> int:
    """Extract an HTTP status code from an exception."""
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    for exc_type, code in _STATUS_CODE_MAP.items():
        if isinstance(exc, exc_type):
            return code
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code
    return 502


def _details_for_exception(exc: Exception | None) -> dict[str, Any]:
    """Preserve structured provider details across group/cascade wrapping."""
    if exc is None:
        return {}
    details = getattr(exc, "details", None)
    if isinstance(details, dict):
        result = dict(details)
    else:
        result = {}
    error_type = getattr(exc, "error_type", None)
    if error_type and "error_type" not in result:
        result["error_type"] = error_type
    if isinstance(exc, ProviderOAuthError):
        result.setdefault("oauth_provider_error", True)
    return result


def _is_opencode_go_deployment(dep: Deployment) -> bool:
    api_base = (dep.api_base or "").rstrip("/")
    return api_base.startswith(OPENCODE_GO_DEFAULT_API_BASE.rstrip("/"))


def _served_model_matches_exact_request(dep: Deployment, actual_model: str) -> bool:
    if actual_model == dep.model:
        return True
    if not _is_opencode_go_deployment(dep):
        return False

    requested = opencode_go_model_id(dep.model).casefold()
    actual = actual_model.casefold()
    if requested and requested in actual:
        return True

    requested_compact = requested.replace(".", "").replace("-", "")
    actual_compact = actual.replace(".", "").replace("-", "")
    return bool(requested_compact and requested_compact in actual_compact)


def _deadline_remaining_s(deadline_at: float | None) -> float | None:
    if deadline_at is None:
        return None
    return deadline_at - time.monotonic()


def _deadline_timeout_details(
    kwargs: dict[str, Any],
    *,
    provider: str = "",
    model: str = "",
    final_provider_error: Exception | str | None = None,
) -> dict[str, Any]:
    metadata = kwargs.get("_scillm_metadata") if isinstance(kwargs.get("_scillm_metadata"), dict) else {}
    started_at = kwargs.get("_deadline_started_at")
    elapsed_ms = None
    if started_at is not None:
        elapsed_ms = int((time.monotonic() - float(started_at)) * 1000)
    details = {
        "caller": kwargs.get("_caller_skill") or "unknown",
        "item_id": metadata.get("item_id"),
        "batch_id": metadata.get("batch_id"),
        "provider": provider,
        "model": model,
        "elapsed_ms": elapsed_ms,
        "timeout_s": kwargs.get("_deadline_timeout_s"),
        "cascade_attempts": kwargs.get("_cascade_attempts", 0),
        "final_provider_error": str(final_provider_error) if final_provider_error else None,
    }
    return {key: value for key, value in details.items() if value is not None}


def _safe_api_base_host(api_base: str | None) -> str | None:
    if not api_base:
        return None
    parsed = urlparse(api_base)
    return parsed.netloc or None


def _record_provider_bound_diagnostics(
    diagnostics: dict[str, Any] | None,
    *,
    dep: Deployment,
    provider: str,
    body_keys: list[str] | set[str],
    stream: bool,
) -> None:
    """Record prompt-free provider-bound request shape for reliability debugging."""
    if diagnostics is None:
        return
    keys = sorted(str(key) for key in body_keys)
    diagnostics.update({
        "provider": provider,
        "model": dep.model,
        "api_base_host": _safe_api_base_host(dep.api_base),
        "body_keys": keys,
        "token_cap_fields_present": [
            field for field in ("max_tokens", "max_completion_tokens") if field in keys
        ],
        "stream": bool(stream),
    })


def _deadline_exceeded(
    kwargs: dict[str, Any],
    *,
    provider: str = "",
    model: str = "",
    final_provider_error: Exception | str | None = None,
) -> ProxyError:
    details = _deadline_timeout_details(
        kwargs,
        provider=provider,
        model=model,
        final_provider_error=final_provider_error,
    )
    timeout_s = details.get("timeout_s", "unknown")
    attempts = details.get("cascade_attempts", 0)
    return ProxyError(
        504,
        f"Request deadline exceeded after {timeout_s}s "
        f"(provider={provider or 'unknown'}, model={model or 'unknown'}, "
        f"cascade_attempts={attempts})",
        "timeout_error",
        details=details,
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------




def _is_moonshot_deployment(dep: Deployment) -> bool:
    base = (dep.api_base or "").lower()
    return "moonshot.ai" in base or "moonshot.cn" in base


def _has_image_url_messages(messages: list[dict[str, Any]]) -> bool:
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
        if isinstance(content, dict) and "image_url" in content:
            return True
    return False


def _apply_moonshot_multimodal_kwargs(
    dep: Deployment,
    messages: list[dict[str, Any]],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Moonshot kimi-k2.6 accepts image_url; disable thinking for deterministic text."""
    if not _is_moonshot_deployment(dep) or not _has_image_url_messages(messages):
        return kwargs
    updated = dict(kwargs)
    extra_body = dict(updated.get("extra_body") or {})
    thinking = extra_body.get("thinking")
    if not isinstance(thinking, dict) or thinking.get("type") != "enabled":
        extra_body["thinking"] = {"type": "disabled"}
        updated["extra_body"] = extra_body
    return updated

class Router:
    """Route completion requests across model groups with fallback cascade.

    Features:
        - Per-exception retry with exponential backoff.
        - Fallback cascade (try next group on exhaustion).
        - Circuit breaker per group (skip groups in cooldown).

    Args:
        config: A fully-parsed ProxyConfig.
    """

    def __init__(self, config: ProxyConfig) -> None:
        self._config = config
        self._clients: dict[int, openai.AsyncOpenAI] = {}
        self._circuits: dict[str, _CircuitState] = {}
        logger.info(
            "Router initialised — {} group(s), {} alias(es), {} fallback chain(s), "
            "circuit_breaker(allowed_fails={}, cooldown={}s)",
            len(config.model_groups),
            len(config.aliases),
            len(config.fallbacks),
            config.allowed_fails,
            config.cooldown_time,
        )

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------

    def _circuit(self, group_name: str) -> _CircuitState:
        if group_name not in self._circuits:
            self._circuits[group_name] = _CircuitState()
        return self._circuits[group_name]

    # ------------------------------------------------------------------
    # Client management (lazy + cached)
    # ------------------------------------------------------------------

    def _client_for(self, dep: Deployment) -> openai.AsyncOpenAI:
        """Return a cached AsyncOpenAI client for *dep*, creating if needed."""
        key = id(dep)
        client = self._clients.get(key)
        if client is not None:
            return client
        client = openai.AsyncOpenAI(
            api_key=dep.api_key or "no-key",
            base_url=dep.api_base,
            timeout=float(dep.timeout),
        )
        self._clients[key] = client
        logger.debug("Created client for {} (base={})", dep.model, dep.api_base)
        return client

    # ------------------------------------------------------------------
    # Gemini native API (for file/inlineData content)
    # ------------------------------------------------------------------

    @staticmethod
    def _has_inline_data(messages: list[dict[str, Any]]) -> bool:
        """Check if any message contains inlineData parts (ZIP, PDF, etc.)."""
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and "inlineData" in part:
                        return True
        return False

    @staticmethod
    def _is_gemini(dep: Deployment) -> bool:
        """Check if a deployment targets Gemini."""
        return bool(
            dep.api_base
            and "generativelanguage.googleapis.com" in dep.api_base
        )

    @staticmethod
    def _to_gemini_contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Translate OpenAI messages to Gemini generateContent format."""
        contents: list[dict[str, Any]] = []
        for msg in messages:
            role = "model" if msg.get("role") == "assistant" else "user"
            content = msg.get("content", "")
            parts: list[dict[str, Any]] = []

            if isinstance(content, str):
                parts.append({"text": content})
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        parts.append({"text": part["text"]})
                    elif "inlineData" in part:
                        inline = part["inlineData"]
                        mime = inline.get("mimeType", "")
                        if mime == "application/zip":
                            # Explode ZIP: unpack and send each file as its own part
                            zip_bytes = base64.b64decode(inline["data"])
                            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                                for name in zf.namelist():
                                    if name.endswith("/"):
                                        continue  # skip directories
                                    file_bytes = zf.read(name)
                                    file_mime = mimetypes.guess_type(name)[0] or "text/plain"
                                    # Text files: send as text parts with filename header
                                    if file_mime.startswith("text/") or file_mime in (
                                        "application/json", "application/javascript",
                                        "application/xml", "application/typescript",
                                    ):
                                        try:
                                            text_content = file_bytes.decode("utf-8")
                                        except UnicodeDecodeError:
                                            text_content = file_bytes.decode("latin-1")
                                        parts.append({"text": f"=== {name} ===\n{text_content}"})
                                    else:
                                        # Binary files (images, PDFs): send as inlineData
                                        file_b64 = base64.b64encode(file_bytes).decode()
                                        parts.append({"inlineData": {"mimeType": file_mime, "data": file_b64}})
                                logger.info("ZIP exploded: {} files from archive", len(zf.namelist()))
                        else:
                            parts.append({"inlineData": inline})
                    elif part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            # data:image/png;base64,xxxxx
                            header, data = url.split(",", 1)
                            mime = header.split(":")[1].split(";")[0]
                            parts.append({"inlineData": {"mimeType": mime, "data": data}})
                        else:
                            parts.append({"text": f"[image: {url}]"})

            if parts:
                contents.append({"role": role, "parts": parts})
        return contents

    async def _gemini_native_call(
        self,
        dep: Deployment,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        """Call Gemini's native generateContent API for file/inlineData content.

        Translates OpenAI messages to Gemini format, calls the REST API directly,
        and wraps the response back into OpenAI ChatCompletion format.
        """
        contents = self._to_gemini_contents(messages)

        # Build Gemini API URL: extract base and construct generateContent endpoint
        # Config api_base is like: https://generativelanguage.googleapis.com/v1beta/openai
        # Native API needs: https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
        base = dep.api_base or ""
        base = base.replace("/openai", "").rstrip("/")
        url = f"{base}/models/{dep.model}:generateContent"

        body: dict[str, Any] = {"contents": contents}

        # Map OpenAI params to Gemini generationConfig
        gen_config: dict[str, Any] = {}
        if "max_tokens" in kwargs:
            gen_config["maxOutputTokens"] = kwargs["max_tokens"]
        if "temperature" in kwargs:
            gen_config["temperature"] = kwargs["temperature"]
        if "top_p" in kwargs:
            gen_config["topP"] = kwargs["top_p"]
        if "stop" in kwargs:
            gen_config["stopSequences"] = kwargs["stop"] if isinstance(kwargs["stop"], list) else [kwargs["stop"]]
        if gen_config:
            body["generationConfig"] = gen_config

        logger.info("Gemini native call: model={}, {} content parts, url={}",
                     dep.model, sum(len(c.get("parts", [])) for c in contents), url)

        async with httpx.AsyncClient(timeout=float(dep.timeout)) as client:
            resp = await client.post(
                url,
                json=body,
                headers={"Content-Type": "application/json"},
                params={"key": dep.api_key},
            )

        if resp.status_code != 200:
            error_body = resp.text
            logger.warning("Gemini native API error {}: {}", resp.status_code, error_body[:500])
            raise openai.APIStatusError(
                message=f"Gemini native API error: {error_body[:500]}",
                response=resp,  # type: ignore[arg-type]
                body=error_body,
            )

        data = resp.json()

        # Extract text from Gemini response
        candidates = data.get("candidates", [])
        text = ""
        finish_reason = "stop"
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts)
            fr = candidates[0].get("finishReason", "STOP")
            finish_reason = "length" if fr == "MAX_TOKENS" else "stop"

        # Extract usage
        usage_meta = data.get("usageMetadata", {})

        # Wrap in OpenAI ChatCompletion format
        import uuid as _uuid
        return openai.types.chat.ChatCompletion(
            id=f"chatcmpl-gemini-{_uuid.uuid4().hex[:8]}",
            choices=[
                openai.types.chat.chat_completion.Choice(
                    finish_reason=finish_reason,
                    index=0,
                    message=openai.types.chat.ChatCompletionMessage(
                        content=text,
                        role="assistant",
                    ),
                )
            ],
            created=int(time.time()),
            model=dep.model,
            object="chat.completion",
            usage=openai.types.CompletionUsage(
                completion_tokens=usage_meta.get("candidatesTokenCount", 0),
                prompt_tokens=usage_meta.get("promptTokenCount", 0),
                total_tokens=usage_meta.get("totalTokenCount", 0),
            ),
        )

    # ------------------------------------------------------------------
    # Alias / group resolution
    # ------------------------------------------------------------------

    def _resolve_group(self, model: str) -> str:
        """Resolve aliases and return the canonical group name."""
        return self._config.aliases.get(model, model)

    def _get_group(self, name: str) -> ModelGroup | None:
        group = self._config.model_groups.get(name)
        if group is not None:
            return group

        # Auto-create Claude group for model names starting with "claude"
        # (e.g. "claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5").
        # Uses OAuth token from ~/.pi/agent/auth.json.
        if name.lower().startswith("claude") and is_anthropic_available():
            dep = Deployment(
                model=name,
                api_base="https://api.anthropic.com",
                api_key="oauth",
                timeout=90,
                custom_llm_provider="anthropic-oauth",
            )
            group = ModelGroup(name=name, deployments=[dep])
            self._config.model_groups[name] = group
            logger.info("Auto-created Claude OAuth group for model {!r}", name)
            return group

        # Auto-create Codex group for model names starting with "codex" or "gpt"
        # or "o3"/"o4" (OpenAI reasoning models).
        # Uses OAuth token from ~/.pi/agent/auth.json.
        _codex_prefixes = ("codex", "gpt", "o1", "o3", "o4")
        if any(name.lower().startswith(p) for p in _codex_prefixes) and is_codex_available():
            discovered = discover_codex_models()
            resolved = resolve_codex_model(name, discovered)
            if discovered and resolved is None:
                logger.warning("Codex model {!r} is absent from the discovered catalog", name)
                return None
            routed_model = resolved.slug if resolved is not None else name
            dep = Deployment(
                model=routed_model,
                api_base="https://api.openai.com",
                api_key="oauth",
                timeout=120,
                custom_llm_provider="codex-oauth",
            )
            group = ModelGroup(name=name, deployments=[dep])
            self._config.model_groups[name] = group
            logger.info("Auto-created Codex OAuth group {!r} routed to {!r}", name, routed_model)
            return group

        # Auto-create Gemini group for model names starting with "gemini"
        # (e.g. "gemini-2.5-flash", "gemini-3-flash-preview", "gemini-3.1-pro-preview").
        gemini_base = self._config.gemini_api_base
        gemini_key = self._config.gemini_api_key
        if gemini_base and gemini_key and name.lower().startswith("gemini"):
            dep = Deployment(
                model=name,
                api_base=gemini_base,
                api_key=gemini_key,
                timeout=90,
            )
            group = ModelGroup(name=name, deployments=[dep])
            self._config.model_groups[name] = group
            logger.info("Auto-created Gemini group for model {!r}", name)
            return group

        # Auto-create OpenCode Go group for provider/model ids.
        # Important: this must run before Chutes slash detection because the
        # public model names intentionally use "opencode-go/<model-id>".
        if is_opencode_go_model(name):
            model_id = opencode_go_model_id(name)
            endpoint_type = opencode_go_endpoint_type(model_id)
            if endpoint_type == ENDPOINT_MESSAGES:
                custom_provider = "opencode-go-messages"
            else:
                custom_provider = None
            if endpoint_type not in {ENDPOINT_CHAT_COMPLETIONS, ENDPOINT_MESSAGES}:
                logger.warning("Unknown OpenCode Go model {!r}; defaulting to chat/completions", name)
            timeout = (
                OPENCODE_GO_MESSAGES_TIMEOUT_SEC
                if endpoint_type == ENDPOINT_MESSAGES
                else OPENCODE_GO_CHAT_TIMEOUT_SEC
            )
            dep = Deployment(
                model=model_id,
                api_base=self._config.opencode_go_api_base or OPENCODE_GO_DEFAULT_API_BASE,
                api_key=self._config.opencode_go_api_key or "",
                timeout=timeout,
                custom_llm_provider=custom_provider,
            )
            group = ModelGroup(name=name, deployments=[dep])
            self._config.model_groups[name] = group
            logger.info("Auto-created OpenCode Go group for model {!r} via {}", name, endpoint_type)
            return group

        # Auto-create Chutes group for model names containing "/"
        # (e.g. "Qwen/Qwen3-30B-A3B", "deepseek-ai/DeepSeek-V3").
        chutes_base = self._config.chutes_api_base
        chutes_key = self._config.chutes_api_key
        if chutes_base and chutes_key and "/" in name:
            dep = Deployment(
                model=name,
                api_base=chutes_base,
                api_key=chutes_key,
                timeout=60,
            )
            group = ModelGroup(name=name, deployments=[dep])
            self._config.model_groups[name] = group
            logger.info("Auto-created Chutes group for model {!r}", name)
            return group

        # Auto-create Ollama group for unknown model names that look like
        # Ollama tags (e.g. "qwen2.5:7b", "llama3:8b").  This lets callers
        # use any locally-pulled Ollama model without an explicit config entry.
        ollama_base = self._config.ollama_api_base
        if ollama_base and (":" in name or "/" not in name):
            dep = Deployment(
                model=name,
                api_base=ollama_base,
                api_key="ollama",
                timeout=120,
            )
            group = ModelGroup(name=name, deployments=[dep])
            self._config.model_groups[name] = group
            logger.info("Auto-created Ollama group for model {!r}", name)
            return group

        return None

    def _fallback_chain(self, group_name: str) -> list[str]:
        """Return the ordered list of groups to try (primary + fallbacks)."""
        chain = [group_name]
        chain.extend(self._config.fallbacks.get(group_name, []))
        return chain

    # ------------------------------------------------------------------
    # Single-deployment attempt with per-exception retry
    # ------------------------------------------------------------------

    async def _try_deployment(
        self,
        dep: Deployment,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        """Attempt a completion against a single deployment with retries.

        Returns the OpenAI response/stream on success.
        Raises the last exception on exhaustion.
        """
        request_context = dict(kwargs)
        kwargs = dict(kwargs)
        override = kwargs.pop("_max_retries_override", None)
        deadline_at_raw = kwargs.pop("_deadline_at", None)
        deadline_at = float(deadline_at_raw) if deadline_at_raw is not None else None
        provider_bound_diagnostics = kwargs.pop("_provider_bound_diagnostics", None)
        kwargs.pop("_deadline_timeout_s", None)
        kwargs.pop("_deadline_started_at", None)
        kwargs.pop("_caller_skill", None)
        kwargs.pop("_scillm_metadata", None)
        kwargs.pop("_cascade_attempts", None)
        requested_exact_model = bool(kwargs.pop("require_exact_model", False))
        allow_model_remap = kwargs.pop("allow_model_remap", None)
        require_exact_model = requested_exact_model or allow_model_remap is False

        # Dynamic timeout from TimeoutEstimatorMiddleware (ms -> seconds)
        # Use MAX of dynamic estimate and config timeout — config is a floor
        dynamic_timeout_ms = kwargs.pop("_dynamic_timeout_ms", None)
        policy_max_timeout_s = kwargs.pop("_policy_max_timeout_s", None)
        config_timeout = float(dep.timeout or 120)
        if dynamic_timeout_ms is not None:
            dynamic_timeout = float(dynamic_timeout_ms) / 1000.0
            timeout_sec = max(dynamic_timeout, config_timeout)
        else:
            timeout_sec = config_timeout
        if policy_max_timeout_s is not None:
            timeout_sec = min(timeout_sec, float(policy_max_timeout_s))

        remaining = _deadline_remaining_s(deadline_at)
        if remaining is not None:
            if remaining <= 0:
                raise _deadline_exceeded(request_context, provider=dep.custom_llm_provider or "", model=dep.model)
            timeout_sec = min(timeout_sec, remaining)

        client = self._client_for(dep)
        policy = self._config.retry_policy
        base_delay = self._config.retry_after
        last_exc: Exception | None = None

        # Ollama doesn't support OpenAI's response_format parameter —
        # strip it to avoid silent failures or 400 errors.
        is_ollama = dep.api_base and ("11434" in dep.api_base or "ollama" in dep.api_base.lower())
        if is_ollama and "response_format" in kwargs:
            logger.debug("Stripping response_format for Ollama deployment {}", dep.model)
            kwargs = {k: v for k, v in kwargs.items() if k != "response_format"}

        is_opencode_go_chat = (
            dep.custom_llm_provider is None
            and bool(dep.api_base)
            and "opencode.ai/zen" in dep.api_base
        )
        if is_opencode_go_chat and "response_format" in kwargs:
            # OpenCode Go chat/completions models such as GLM can hang when
            # OpenAI JSON mode is forwarded. Keep scillm's JSON guard active at
            # the middleware layer, but ask the model for JSON in the prompt.
            logger.debug("Stripping response_format for OpenCode Go chat deployment {}", dep.model)
            kwargs = {k: v for k, v in kwargs.items() if k != "response_format"}

        # Gemini native API path: when messages contain inlineData parts
        # (ZIP, PDF, etc.), bypass the openai SDK and call Gemini directly.
        if self._is_gemini(dep) and self._has_inline_data(messages):
            logger.info("Using Gemini native API for inlineData content → {}", dep.model)
            return await self._gemini_native_call(dep, messages, **kwargs)

        # Claude OAuth path: uses Anthropic Messages API with format translation.
        if dep.custom_llm_provider == "anthropic-oauth":
            if kwargs.get("stream", False):
                stream_kwargs = {k: v for k, v in kwargs.items() if k != "stream"}
                return claude_completion_stream(dep.model, messages, timeout=timeout_sec, **stream_kwargs)
            return await claude_completion(dep.model, messages, timeout=timeout_sec, **kwargs)

        # Codex OAuth path: uses OpenAI Responses API with SSE streaming.
        if dep.custom_llm_provider == "codex-oauth":
            if kwargs.get("stream", False):
                stream_kwargs = {k: v for k, v in kwargs.items() if k != "stream"}
                return codex_completion_stream(dep.model, messages, timeout=timeout_sec, **stream_kwargs)
            return await codex_completion(dep.model, messages, timeout=timeout_sec, **kwargs)

        # Gemini OAuth path: uses Gemini CLI for headless calls (bypasses 20 RPD API limit).
        # Best for long-context tasks (1M tokens). No streaming support yet.
        if dep.custom_llm_provider == "gemini-oauth":
            if kwargs.get("stream", False):
                logger.warning("gemini-oauth: streaming not supported, falling back to non-streaming")
            return await gemini_oauth_complete(messages, model=dep.model, timeout=timeout_sec)

        # OpenCode Go messages path: DeepSeek and MiniMax use an
        # Anthropic-compatible /messages endpoint, not /chat/completions.
        if dep.custom_llm_provider == "opencode-go-messages":
            if kwargs.get("stream", False):
                stream_kwargs = {k: v for k, v in kwargs.items() if k != "stream"}
                stream_kwargs["_provider_bound_diagnostics"] = provider_bound_diagnostics
                return opencode_go_messages_stream(
                    dep.model,
                    dep.api_base or OPENCODE_GO_DEFAULT_API_BASE,
                    dep.api_key or "",
                    messages,
                    timeout=timeout_sec,
                    **stream_kwargs,
                )
            return await opencode_go_messages_completion(
                dep.model,
                dep.api_base or OPENCODE_GO_DEFAULT_API_BASE,
                dep.api_key or "",
                messages,
                timeout=timeout_sec,
                _provider_bound_diagnostics=provider_bound_diagnostics,
                **kwargs,
            )

        # We do one initial attempt + up to max_retries retries.
        # _max_retries_override caps retries when multiple models share a group
        # (e.g., non-TEE + TEE) so we fail fast to the next deployment.
        max_possible = max(
            policy.internal_server_error,
            policy.rate_limit_error,
            policy.timeout_error,
            policy.authentication_error,
            policy.bad_request_error,
        )
        if override is not None:
            max_possible = min(max_possible, override)

        for attempt in range(max_possible + 1):
            try:
                remaining = _deadline_remaining_s(deadline_at)
                if remaining is not None:
                    if remaining <= 0:
                        raise _deadline_exceeded(
                            request_context,
                            provider=dep.custom_llm_provider or "",
                            model=dep.model,
                            final_provider_error=last_exc,
                        )
                    timeout_sec = min(timeout_sec, remaining)
                provider_name = (
                    "opencode-go"
                    if is_opencode_go_chat
                    else dep.custom_llm_provider or "openai-compatible"
                )
                provider_body_keys = {"model", "messages", *kwargs.keys()}
                _record_provider_bound_diagnostics(
                    provider_bound_diagnostics,
                    dep=dep,
                    provider=provider_name,
                    body_keys=provider_body_keys,
                    stream=bool(kwargs.get("stream", False)),
                )
                call_kwargs = _apply_moonshot_multimodal_kwargs(dep, messages, kwargs)
                resp = await client.chat.completions.create(
                    model=dep.model,
                    messages=messages,
                    timeout=timeout_sec,
                    **call_kwargs,
                )
                if require_exact_model and not kwargs.get("stream", False):
                    actual_model = getattr(resp, "model", None)
                    if (
                        isinstance(actual_model, str)
                        and actual_model
                        and not _served_model_matches_exact_request(dep, actual_model)
                    ):
                        raise ProxyError(
                            502,
                            f"Provider returned model={actual_model!r}; required exact model={dep.model!r}",
                            details={
                                "requested_model": dep.model,
                                "served_model": actual_model,
                                "provider": dep.custom_llm_provider or "openai-compatible",
                            },
                        )
                return resp
            except (
                openai.InternalServerError,
                openai.RateLimitError,
                openai.APITimeoutError,
                openai.AuthenticationError,
                openai.BadRequestError,
                openai.APIStatusError,
            ) as exc:
                last_exc = exc

                # Chutes cold-start detection: on first 5xx, trigger warmup API
                if (
                    attempt == 0
                    and isinstance(exc, (openai.InternalServerError, openai.APITimeoutError))
                    and dep.api_base
                    and _CHUTES_API_BASE in dep.api_base
                ):
                    _trigger_chutes_warmup(dep.model)

                # Chutes cold model: "No instances available" — skip retries, cascade to fallback
                exc_msg = str(exc).lower()
                if "no instances available" in exc_msg or "no workers available" in exc_msg:
                    logger.warning(
                        "Cold model {} — skipping retries, cascading to fallback: {}",
                        dep.model,
                        str(exc)[:100],
                    )
                    raise

                allowed = _max_retries_for(exc, policy)
                if attempt >= allowed:
                    logger.warning(
                        "Deployment {} exhausted {} retries for {}: {}",
                        dep.model,
                        allowed,
                        type(exc).__name__,
                        exc,
                    )
                    raise
                delay = min(base_delay * (2 ** attempt), 60)
                remaining = _deadline_remaining_s(deadline_at)
                if remaining is not None and remaining <= delay:
                    raise _deadline_exceeded(
                        request_context,
                        provider=dep.custom_llm_provider or "",
                        model=dep.model,
                        final_provider_error=exc,
                    )
                logger.info(
                    "Deployment {} attempt {}/{} got {} — retrying in {:.1f}s",
                    dep.model,
                    attempt + 1,
                    allowed,
                    type(exc).__name__,
                    delay,
                )
                await asyncio.sleep(delay)

        # Should not reach here, but satisfy the type checker
        assert last_exc is not None  # noqa: S101
        raise last_exc

    # ------------------------------------------------------------------
    # Group-level attempt (shuffle deployments, try each)
    # ------------------------------------------------------------------

    async def _try_group(
        self,
        group: ModelGroup,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        """Try all deployments in a group in config order. Returns first success.

        Config order matters: non-TEE before TEE, free key before paid key.
        Only shuffle when all deployments use the same model (load balancing).
        When multiple distinct models exist, fail fast (1 retry) per deployment
        so we don't burn minutes on a cold model when a hot one is next.
        """
        deps = list(group.deployments)
        models = {d.model for d in deps}
        multi_model = len(models) > 1
        if not multi_model:
            random.shuffle(deps)  # Same model, different keys → load balance

        last_exc: Exception | None = None
        for dep in deps:
            try:
                # Multi-model groups: fail fast (1 retry) so we don't burn time
                # on a cold model when a hot alternative is next in line.
                kw = dict(**kwargs)
                if multi_model:
                    kw["_max_retries_override"] = 1
                return await self._try_deployment(dep, messages, **kw)
            except Exception as exc:
                last_exc = exc
                if isinstance(exc, ProxyError) and exc.status_code == 504:
                    raise
                logger.debug(
                    "Group '{}' deployment {} failed: {}",
                    group.name,
                    dep.model,
                    exc,
                )
                continue

        assert last_exc is not None  # noqa: S101
        raise last_exc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        """Route a chat completion through the fallback cascade.

        Args:
            model: Requested model name (may be an alias or group name).
            messages: OpenAI-format message list.
            **kwargs: Forwarded to ``client.chat.completions.create``
                      (e.g. ``stream=True``, ``temperature``, etc.).

        Returns:
            The OpenAI ChatCompletion response, or an async stream when
            ``stream=True``.

        Raises:
            ProxyError: When every deployment in every fallback group has
                        been exhausted.
        """
        group_name = self._resolve_group(model)

        # Dynamic fallback chain from middleware (e.g., chutes_router utilization-aware)
        dynamic_chain = kwargs.pop("_dynamic_fallback_chain", None)
        if dynamic_chain:
            chain = dynamic_chain
            logger.info(
                "complete model={!r} → group={!r}, chain={} (dynamic)",
                model,
                group_name,
                chain,
            )
        else:
            chain = self._fallback_chain(group_name)
            logger.info(
                "complete model={!r} → group={!r}, chain={}",
                model,
                group_name,
                chain,
            )

        last_exc: Exception | None = None
        last_status = 502
        skipped: list[str] = []
        cascade_attempts = 0

        for gname in chain:
            remaining = _deadline_remaining_s(kwargs.get("_deadline_at"))
            if remaining is not None and remaining <= 0:
                raise _deadline_exceeded(kwargs, final_provider_error=last_exc)

            # Circuit breaker: skip groups in cooldown
            circuit = self._circuit(gname)
            if circuit.is_open():
                logger.info("circuit_breaker: skipping {} (in cooldown)", gname)
                skipped.append(gname)
                continue

            group = self._get_group(gname)
            if group is None:
                logger.warning("Group {!r} not found, skipping", gname)
                continue
            if not group.deployments:
                logger.warning("Group {!r} has no deployments, skipping", gname)
                continue
            try:
                cascade_attempts += 1
                group_kwargs = dict(kwargs)
                group_kwargs["_cascade_attempts"] = cascade_attempts
                result = await self._try_group(group, messages, **group_kwargs)
                circuit.record_success()
                return result
            except Exception as exc:
                last_exc = exc
                last_status = _status_code_for(exc)
                if isinstance(exc, ProxyError) and exc.status_code == 504:
                    raise
                circuit.record_failure(
                    self._config.allowed_fails,
                    self._config.cooldown_time,
                )
                logger.warning(
                    "Fallback group {!r} exhausted: {}",
                    gname,
                    exc,
                )
                continue

        # Every group in the chain failed
        skipped_msg = f" (skipped in cooldown: {skipped})" if skipped else ""
        details = _details_for_exception(last_exc)
        if skipped:
            details["skipped_groups"] = skipped
        details.setdefault("fallback_chain", chain)
        msg = (
            f"All groups exhausted for model={model!r} "
            f"(chain={chain}){skipped_msg}: {last_exc}"
        )
        logger.error(msg)
        raise ProxyError(last_status, msg, details=details)

    # ------------------------------------------------------------------
    # Health / introspection
    # ------------------------------------------------------------------

    def circuit_status(self) -> dict[str, Any]:
        """Return circuit breaker state for all groups."""
        return {name: state.status() for name, state in self._circuits.items()}

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close all cached HTTP clients."""
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
        logger.info("Router closed — all clients released")
