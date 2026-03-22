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

import httpx
import openai
from loguru import logger

from scillm.proxy.config import Deployment, ModelGroup, ProxyConfig, RetryPolicy


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProxyError(Exception):
    """Terminal error raised when all retries and fallbacks are exhausted."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
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
    for exc_type, code in _STATUS_CODE_MAP.items():
        if isinstance(exc, exc_type):
            return code
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code
    return 502


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


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
            # Cache it so subsequent requests skip this path
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

        # Gemini native API path: when messages contain inlineData parts
        # (ZIP, PDF, etc.), bypass the openai SDK and call Gemini directly.
        if self._is_gemini(dep) and self._has_inline_data(messages):
            logger.info("Using Gemini native API for inlineData content → {}", dep.model)
            return await self._gemini_native_call(dep, messages, **kwargs)

        # We do one initial attempt + up to max_retries retries.
        # max_retries is only known after the first failure, so we loop
        # with a generous upper bound and break when budget exhausted.
        max_possible = max(
            policy.internal_server_error,
            policy.rate_limit_error,
            policy.timeout_error,
            policy.authentication_error,
            policy.bad_request_error,
        )

        for attempt in range(max_possible + 1):
            try:
                resp = await client.chat.completions.create(
                    model=dep.model,
                    messages=messages,
                    **kwargs,
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
        """Try all deployments in a group (shuffled). Returns first success."""
        deps = list(group.deployments)
        random.shuffle(deps)

        last_exc: Exception | None = None
        for dep in deps:
            try:
                return await self._try_deployment(dep, messages, **kwargs)
            except Exception as exc:
                last_exc = exc
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

        for gname in chain:
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
                result = await self._try_group(group, messages, **kwargs)
                circuit.record_success()
                return result
            except Exception as exc:
                last_exc = exc
                last_status = _status_code_for(exc)
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
        msg = (
            f"All groups exhausted for model={model!r} "
            f"(chain={chain}){skipped_msg}: {last_exc}"
        )
        logger.error(msg)
        raise ProxyError(last_status, msg)

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
