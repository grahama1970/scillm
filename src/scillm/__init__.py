"""scillm — thin OpenAI-compatible proxy with multi-provider fallback.

Public API exports the proxy's own modules. Zero litellm imports.
All provider calls go through openai.AsyncOpenAI in the router.
"""

from scillm.proxy.config import ProxyConfig, load_config
from scillm.proxy.router import Router
from scillm.proxy.middleware import BaseMiddleware, MiddlewareChain, MiddlewareReject
from scillm.proxy.streaming import stream_response, collect_response
from scillm.proxy.errors import ProxyError, classify_openai_error

__all__ = [
    "ProxyConfig",
    "load_config",
    "Router",
    "BaseMiddleware",
    "MiddlewareChain",
    "MiddlewareReject",
    "stream_response",
    "collect_response",
    "ProxyError",
    "classify_openai_error",
]
