"""scillm — thin OpenAI-compatible proxy with multi-provider fallback.

Keep package import side effects minimal so paved helpers like
``scillm.batch_wrappers`` remain importable without pulling in the full proxy
stack and provider-specific optional dependencies.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


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


def __getattr__(name: str) -> Any:
    if name in {"ProxyConfig", "load_config"}:
        module = import_module("scillm.proxy.config")
    elif name == "Router":
        module = import_module("scillm.proxy.router")
    elif name in {"BaseMiddleware", "MiddlewareChain", "MiddlewareReject"}:
        module = import_module("scillm.proxy.middleware")
    elif name in {"stream_response", "collect_response"}:
        module = import_module("scillm.proxy.streaming")
    elif name in {"ProxyError", "classify_openai_error"}:
        module = import_module("scillm.proxy.errors")
    else:
        raise AttributeError(name)
    return getattr(module, name)
