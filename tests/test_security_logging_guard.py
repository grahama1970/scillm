import re


def test_no_raw_headers_logged_in_openai_like_handler():
    """Ensure we don't pass raw headers into logging additional_args in the HTTPX OpenAI-compatible path."""
    p = "litellm/llms/openai_like/chat/handler.py"
    with open(p, "r", encoding="utf-8") as f:
        src = f.read()
    # Ensure we no longer include '"headers": headers' in additional_args
    assert '"headers": headers' not in src, "handler logs raw headers; must not log secrets"

