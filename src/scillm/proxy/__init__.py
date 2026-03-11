"""scillm.proxy — thin OpenAI-compatible proxy server.

Replaces litellm's 31K-line monolith with <2K lines of focused code.
All providers speak native OpenAI format; no translation layer needed.
"""

__all__ = ["config", "router", "middleware", "streaming", "errors"]
