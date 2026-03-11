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
    # Alias / group resolution
    # ------------------------------------------------------------------

    def _resolve_group(self, model: str) -> str:
        """Resolve aliases and return the canonical group name."""
        return self._config.aliases.get(model, model)

    def _get_group(self, name: str) -> ModelGroup | None:
        return self._config.model_groups.get(name)

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
