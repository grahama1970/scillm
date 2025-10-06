import asyncio
import time
import httpx
import os
import pytest

import litellm
from litellm.router import Router
from litellm.utils import ModelResponse


def _mk_rate_limit_error():
    resp = httpx.Response(429, headers={"Retry-After": "0"})
    return litellm.RateLimitError(
        message="rate limit",
        llm_provider="stub",
        model="x",
        response=resp,
    )


@pytest.mark.asyncio
async def test_router_retry_429_succeeds_then_returns():
    attempts = {"n": 0}

    async def _ok_call(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise _mk_rate_limit_error()
        mr = ModelResponse()
        mr.choices[0].message.content = "ok"  # type: ignore[attr-defined]
        return mr

    r = Router(model_list=[])
    async def _healthy(model: str, parent_otel_span=None):
        return ([{"ok": True}], [{"ok": True}])
    r._async_get_healthy_deployments = _healthy  # type: ignore[attr-defined]
    out = await r.async_function_with_retries(
        original_function=_ok_call,
        model="x",
        num_retries=1,
        retry_enabled=True,
        retry_max_attempts=3,
        retry_time_budget_s=5,
        metadata={},
    )
    assert out.choices[0].message.content == "ok"  # type: ignore[attr-defined]
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_router_retry_429_budget_giveup():
    attempts = {"n": 0}
    called = {"giveup": False}

    async def _always_429(*args, **kwargs):
        attempts["n"] += 1
        raise _mk_rate_limit_error()

    def on_giveup(meta):
        called["giveup"] = True

    r = Router(model_list=[])
    async def _healthy2(model: str, parent_otel_span=None):
        return ([{"ok": True}], [{"ok": True}])
    r._async_get_healthy_deployments = _healthy2  # type: ignore[attr-defined]
    with pytest.raises(litellm.RateLimitError):
        await r.async_function_with_retries(
            original_function=_always_429,
            model="x",
            num_retries=0,
            retry_enabled=True,
            retry_max_attempts=2,
            retry_time_budget_s=0.01,
            on_giveup=on_giveup,
            metadata={},
        )
    assert called["giveup"] is True
    assert attempts["n"] >= 1
