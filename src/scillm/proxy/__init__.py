"""scillm.proxy — thin OpenAI-compatible proxy server.

~2K lines of focused code with custom provider adapters.
Supports OpenAI-compatible, Anthropic OAuth, Codex OAuth, and Gemini native APIs.
"""

__all__ = ["config", "router", "middleware", "streaming", "errors"]
