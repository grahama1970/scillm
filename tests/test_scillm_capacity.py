import pytest

from scillm import _normalize_quota_exception, CapacityExceededError


def test_capacity_wrapped_from_litellm_rate_limit():
    try:
        from litellm.exceptions import RateLimitError
    except Exception:  # pragma: no cover - litellm not available
        pytest.skip("litellm not importable")

    import httpx

    resp = httpx.Response(429, request=httpx.Request("POST", "https://example.com/v1/chat/completions"))
    err = RateLimitError(
        message="Infrastructure is at maximum capacity",
        llm_provider="chutes",
        model="test-model",
        response=resp,
    )

    out = _normalize_quota_exception(err)
    assert isinstance(out, CapacityExceededError)
    assert out.reason == "capacity_exhausted"
    assert out.provider == "chutes"
    assert out.model == "test-model"
    assert getattr(out, "status", None) == 429
