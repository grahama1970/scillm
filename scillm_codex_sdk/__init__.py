# SPDX-License-Identifier: MIT
"""
SciLLM Codex Cloud SDK (experimental)

Two client surfaces:
- CloudTasksClient (async task-oriented client used by advanced flows)
- CodexCloudClient (simple HTTP client with resilient retries and familiar chat/variants helpers)
"""

from .client import CloudTasksClient, CloudTasksClientSync
from .errors import CodexError, CodexAuthError, CodexHttpError

# Optional simpler surface (may be defined in this module at runtime)
try:
    # Will be injected below via a small wrapper if not present
    from .client import CodexCloudClient  # type: ignore
except Exception:  # pragma: no cover
    CodexCloudClient = None  # type: ignore

__all__ = [
    "CloudTasksClient",
    "CloudTasksClientSync",
    "CodexCloudClient",
    "CodexError",
    "CodexAuthError",
    "CodexHttpError",
]

__version__ = "0.1.0"

def version() -> str:
    return __version__

def _env_flag(name: str, default: bool = False) -> bool:
    import os
    v = os.getenv(name)
    if v is None:
        return default
    return v not in ("0", "false", "False", "")

def _default_client():
    """Return a best-effort default client for quick scripts."""
    try:
        if CodexCloudClient is not None:  # type: ignore
            return CodexCloudClient()  # type: ignore
    except Exception:
        pass
    return CloudTasksClientSync()

def chat(messages, model=None, **kwargs):
    """
    Thin pass-through to a default client chat-like call. Returns OpenAI-shaped dict.
    Prefer scillm.extras.codex_cloud.chat_cloud for higher-level UX.
    """
    c = _default_client()
    if hasattr(c, "chat"):
        return c.chat(messages=messages, model=model, **kwargs)  # type: ignore
    # Fallback: approximate via task helper
    return CloudTasksClientSync().chat_to_task(messages, env_label=kwargs.get("env_label"), env_id=kwargs.get("env_id"))

def generate_variants(prompt: str, n: int = 3, model=None, **kwargs):
    """
    Thin pass-through to a default client generate_variants()/create_task.
    Returns a dict with a 'variants' list when possible.
    """
    c = _default_client()
    if hasattr(c, "generate_variants"):
        return c.generate_variants(prompt=prompt, n=n, model=model, **kwargs)  # type: ignore
    # Fallback: create a single task and wrap as one variant
    res = CloudTasksClientSync().chat_to_task([
        {"role": "user", "content": prompt}
    ], env_label=kwargs.get("env_label"), env_id=kwargs.get("env_id"))
    return {"variants": [{"id": res.get("id"), "content": res.get("content", "")}]}
