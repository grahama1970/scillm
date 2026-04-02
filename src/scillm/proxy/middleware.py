"""Middleware hook interface for scillm proxy.

Clean async chain of pre_call /
post_call / on_error hooks.  Each middleware is a BaseMiddleware subclass
that overrides only the hooks it needs.

Usage::

    chain = MiddlewareChain([JsonGuard(), BudgetEnforcer()])
    request = await chain.run_pre_call(request)
    response = await chain.run_post_call(request, response)
"""

from __future__ import annotations

from abc import ABC
from typing import Any

from loguru import logger


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MiddlewareReject(Exception):
    """Raised when a middleware rejects a request.

    Attributes:
        status_code: HTTP status code to return (default 400).
        message: Human-readable rejection reason.
    """

    def __init__(self, message: str = "Request rejected by middleware", status_code: int = 400) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class BaseMiddleware(ABC):
    """Abstract base for proxy middleware hooks.

    Subclasses override only the hooks they care about; defaults are no-ops.
    """

    async def pre_call(self, request: dict) -> dict | None:
        """Run before routing.  Return modified request, or None to reject."""
        return request

    async def post_call(self, request: dict, response: Any) -> Any:
        """Run after response.  Return (possibly modified) response."""
        return response

    async def on_error(self, request: dict, error: Exception) -> None:
        """Observe a failure (logging, metrics, alerting)."""


# ---------------------------------------------------------------------------
# Chain
# ---------------------------------------------------------------------------


class MiddlewareChain:
    """Ordered chain of middleware hooks.

    Hooks run in list order for pre_call / post_call, and in list order
    (best-effort, never raises) for on_error.
    """

    def __init__(self, middlewares: list[BaseMiddleware]) -> None:
        self._middlewares: list[BaseMiddleware] = list(middlewares)

    # -- introspection -----------------------------------------------------

    def __len__(self) -> int:
        return len(self._middlewares)

    def __repr__(self) -> str:
        names = [type(m).__name__ for m in self._middlewares]
        return f"MiddlewareChain({names})"

    # -- hooks -------------------------------------------------------------

    async def run_pre_call(self, request: dict) -> dict:
        """Execute pre_call hooks in order.

        Returns the (possibly mutated) request dict.
        Raises MiddlewareReject if any hook returns None.
        """
        current = request
        for mw in self._middlewares:
            name = type(mw).__name__
            logger.debug("pre_call: {}", name)
            result = await mw.pre_call(current)
            if result is None:
                logger.warning("pre_call rejected by {}", name)
                raise MiddlewareReject(f"Rejected by {name}")
            current = result
        return current

    async def run_post_call(self, request: dict, response: Any) -> Any:
        """Execute post_call hooks in order.

        Returns the (possibly mutated) response.
        """
        current = response
        for mw in self._middlewares:
            name = type(mw).__name__
            logger.debug("post_call: {}", name)
            current = await mw.post_call(request, current)
        return current

    async def run_on_error(self, request: dict, error: Exception) -> None:
        """Execute on_error hooks in order, suppressing any exceptions."""
        for mw in self._middlewares:
            name = type(mw).__name__
            try:
                await mw.on_error(request, error)
            except Exception:
                logger.opt(exception=True).warning("on_error hook failed in {}", name)
