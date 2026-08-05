from __future__ import annotations

import time
import asyncio

import httpx
import pytest

from chutes.middleware import active_calls
from chutes.middleware.active_calls import (
    ActiveCall,
    ActiveCallsMiddleware,
    clear_active_calls,
    evict_stale_active_calls,
    get_stale_counts_by_provider,
)
from chutes.middleware import concurrency_guard
from scillm.proxy import app as proxy_app
from scillm.proxy.config import GeneralSettings, ProxyConfig
from scillm.proxy.errors import ProxyError, proxy_error_handler
from scillm.proxy.middleware import BaseMiddleware, MiddlewareChain


def test_stale_active_calls_are_evicted(monkeypatch):
    active_calls._active_calls.clear()
    active_calls._stale_calls.clear()
    active_calls._active_calls["stale"] = ActiveCall(
        call_id="stale",
        model="opencode-go/deepseek-v4-flash",
        caller="create-qras",
        started_at=time.monotonic() - 16,
        started_ts="2026-04-25T00:00:00+00:00",
        provider="opencode-go",
        timeout_s=10,
    )

    evicted = evict_stale_active_calls()

    assert len(evicted) == 1
    assert evicted[0]["call_id"] == "stale"
    assert evicted[0]["live_status"] == "stale"
    assert active_calls._active_calls == {}
    assert get_stale_counts_by_provider()["opencode-go"] == 1


def test_clear_active_calls_filters_old_create_qras_rows():
    active_calls._active_calls.clear()
    active_calls._stale_calls.clear()
    active_calls._active_calls["old"] = ActiveCall(
        call_id="old",
        model="opencode-go/deepseek-v4-flash",
        caller="create-qras",
        started_at=time.monotonic() - 700,
        started_ts="2026-04-25T00:00:00+00:00",
        provider="opencode-go",
    )
    active_calls._active_calls["new"] = ActiveCall(
        call_id="new",
        model="opencode-go/deepseek-v4-flash",
        caller="create-qras",
        started_at=time.monotonic(),
        started_ts="2026-04-25T00:00:00+00:00",
        provider="opencode-go",
    )

    purged = clear_active_calls(older_than_s=600, caller="create-qras")

    assert [call["call_id"] for call in purged] == ["old"]
    assert set(active_calls._active_calls) == {"new"}


def test_clear_active_calls_removes_matching_stale_diagnostics():
    active_calls._active_calls.clear()
    active_calls._stale_calls.clear()
    active_calls._stale_calls.append(
        {
            "call_id": "stale-old",
            "model": "opencode-go/deepseek-v4-flash",
            "caller": "create-qras",
            "provider": "opencode-go",
            "elapsed_ms": 700_000,
            "live_status": "stale",
        }
    )
    active_calls._stale_calls.append(
        {
            "call_id": "stale-new",
            "model": "opencode-go/deepseek-v4-flash",
            "caller": "create-qras",
            "provider": "opencode-go",
            "elapsed_ms": 10_000,
            "live_status": "stale",
        }
    )

    purged = clear_active_calls(older_than_s=600, caller="create-qras")

    assert [call["call_id"] for call in purged] == ["stale-old"]
    assert [call["call_id"] for call in active_calls._stale_calls] == ["stale-new"]


def test_concurrency_release_is_idempotent():
    provider = "opencode-go"
    request_id = "req-test"
    concurrency_guard._semaphores[provider] = concurrency_guard.asyncio.Semaphore(1)
    concurrency_guard._in_flight[provider] = 1
    concurrency_guard._slot_acquired_at[provider] = {request_id: time.monotonic()}

    concurrency_guard._release(provider, request_id)
    concurrency_guard._release(provider, request_id)

    assert concurrency_guard._in_flight[provider] == 0
    assert request_id not in concurrency_guard._slot_acquired_at[provider]


@pytest.mark.asyncio
async def test_active_call_uses_concurrency_provider():
    active_calls._active_calls.clear()
    middleware = ActiveCallsMiddleware()

    request = await middleware.pre_call(
        {
            "model": "opencode-go/deepseek-v4-flash",
            "_concurrency_provider": "opencode-go",
            "_headers": {"x-caller-skill": "create-qras"},
        }
    )

    call = next(iter(active_calls._active_calls.values()))
    assert call.provider == "opencode-go"
    await middleware.on_error(request, RuntimeError("cleanup"))


@pytest.mark.asyncio
async def test_streaming_active_call_lifecycle_waits_for_terminal_chunk():
    active_calls._active_calls.clear()
    active_calls._completed_calls.clear()
    chain = MiddlewareChain([ActiveCallsMiddleware()])

    request = await chain.run_pre_call(
        {
            "model": "oc-kimi",
            "_concurrency_provider": "opencode-go",
            "_headers": {"x-caller-skill": "review-prompt"},
            "stream": True,
            "timeout": 30,
            "stream_heartbeat_s": 0.01,
            "stream_progress_events": True,
        }
    )

    async def provider_stream():
        yield 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        await asyncio.sleep(0)
        yield "data: [DONE]\n\n"

    wrapped = proxy_app._stream_with_middleware_lifecycle(provider_stream(), request, chain)
    first = await anext(wrapped)

    assert '"content":"ok"' in first
    assert len(active_calls._active_calls) == 1
    call = next(iter(active_calls._active_calls.values())).to_dict()
    assert call["provider"] == "opencode-go"
    assert call["stream_heartbeat_s"] == 0.01
    assert call["stream_progress_events"] is True

    rest = [chunk async for chunk in wrapped]

    assert rest == ["data: [DONE]\n\n"]
    assert active_calls._active_calls == {}
    assert active_calls._completed_calls[-1].success is True


@pytest.mark.asyncio
async def test_streaming_active_call_snapshot_exposes_operator_diagnostics():
    active_calls._active_calls.clear()
    active_calls._completed_calls.clear()
    middleware = ActiveCallsMiddleware()

    request = await middleware.pre_call(
        {
            "model": "oc-kimi",
            "_concurrency_provider": "opencode-go",
            "_headers": {"x-caller-skill": "review-prompt"},
            "stream": True,
            "timeout": 180,
            "_dynamic_timeout_ms": 45000,
            "_timeout_source": "p95",
            "stream_heartbeat_s": 5,
            "stream_progress_events": True,
        }
    )

    snapshot = next(iter(active_calls._active_calls.values())).to_dict()

    assert snapshot["model"] == "oc-kimi"
    assert snapshot["model_requested"] == "oc-kimi"
    assert snapshot["model_served"] == "oc-kimi"
    assert snapshot["caller"] == "review-prompt"
    assert snapshot["provider"] == "opencode-go"
    assert snapshot["stream"] is True
    assert snapshot["stream_progress_events"] is True
    assert snapshot["stream_heartbeat_s"] == 5.0
    assert snapshot["timeout_s"] == 180.0
    assert snapshot["deadline_timeout_s"] == 180.0
    assert snapshot["dynamic_timeout_s"] == 45.0
    assert snapshot["timeout_source"] == "p95"
    assert snapshot["started_ts"]
    assert snapshot["created_at"] == snapshot["started_ts"]
    assert isinstance(snapshot["elapsed_ms"], int)
    assert isinstance(snapshot["duration_s"], float)
    assert snapshot["duration_s"] >= 0.0

    await middleware.on_error(request, RuntimeError("cleanup"))


@pytest.mark.asyncio
async def test_streaming_error_payload_marks_active_call_failed():
    active_calls._active_calls.clear()
    active_calls._completed_calls.clear()
    chain = MiddlewareChain([ActiveCallsMiddleware()])

    request = await chain.run_pre_call(
        {
            "model": "oc-kimi",
            "_concurrency_provider": "opencode-go",
            "_headers": {"x-caller-skill": "review-prompt"},
            "stream": True,
            "timeout": 30,
        }
    )

    async def provider_stream():
        yield 'data: {"error":{"message":"provider timeout","type":"timeout_error"}}\n\n'
        yield "data: [DONE]\n\n"

    chunks = [
        chunk
        async for chunk in proxy_app._stream_with_middleware_lifecycle(provider_stream(), request, chain)
    ]

    assert chunks[-1] == "data: [DONE]\n\n"
    assert active_calls._active_calls == {}
    assert active_calls._completed_calls[-1].success is False


@pytest.mark.asyncio
async def test_empty_stream_marks_active_call_failed():
    active_calls._active_calls.clear()
    active_calls._completed_calls.clear()
    chain = MiddlewareChain([ActiveCallsMiddleware()])

    request = await chain.run_pre_call(
        {
            "model": "oc-kimi",
            "_concurrency_provider": "opencode-go",
            "_headers": {"x-caller-skill": "review-prompt"},
            "stream": True,
            "timeout": 30,
        }
    )

    async def provider_stream():
        yield "event: started\n"
        yield 'data: {"model":"oc-kimi","elapsed_ms":1}\n\n'
        yield "event: done\n"
        yield 'data: {"model":"oc-kimi","elapsed_ms":2}\n\n'
        yield "data: [DONE]\n\n"

    chunks = [
        chunk
        async for chunk in proxy_app._stream_with_middleware_lifecycle(provider_stream(), request, chain)
    ]

    assert chunks[-1] == "data: [DONE]\n\n"
    assert active_calls._active_calls == {}
    assert active_calls._completed_calls[-1].success is False


def test_new_semaphore_with_reserved_slots_uses_initial_availability():
    sem = concurrency_guard._new_semaphore_with_reserved_slots(4, 3)

    assert sem._value == 1


@pytest.mark.asyncio
async def test_chat_requires_x_caller_skill_before_provider_routing(monkeypatch):
    class UnusedRouter:
        async def complete(self, model, messages, **kwargs):
            raise AssertionError("router should not run without caller skill")

    monkeypatch.setattr(proxy_app, "_config", ProxyConfig(general=GeneralSettings(master_key="")))
    monkeypatch.setattr(proxy_app, "_router", UnusedRouter())
    monkeypatch.setattr(proxy_app, "_middleware_chain", MiddlewareChain([]))

    transport = httpx.ASGITransport(app=proxy_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "local-text",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "caller_skill_required"


@pytest.mark.asyncio
async def test_chat_timeout_unregisters_active_call_and_returns_details(monkeypatch):
    class SlowRouter:
        async def complete(self, model, messages, **kwargs):
            await asyncio.sleep(1)

    active_calls._active_calls.clear()
    active_calls._stale_calls.clear()
    monkeypatch.setattr(proxy_app, "_config", ProxyConfig(general=GeneralSettings(master_key="")))
    monkeypatch.setattr(proxy_app, "_router", SlowRouter())
    monkeypatch.setattr(proxy_app, "_middleware_chain", MiddlewareChain([ActiveCallsMiddleware()]))

    transport = httpx.ASGITransport(app=proxy_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"x-caller-skill": "create-evidence-case"},
            json={
                "model": "chutes-deepseek",
                "messages": [{"role": "user", "content": "slow"}],
                "timeout": 0.01,
                "scillm_metadata": {"batch_id": "b1", "item_id": "i1"},
            },
        )

    assert response.status_code == 504
    error = response.json()["error"]
    assert error["type"] == "timeout_error"
    assert error["details"]["caller"] == "create-evidence-case"
    assert error["details"]["item_id"] == "i1"
    assert error["details"]["timeout_s"] == 0.01
    assert active_calls._active_calls == {}


@pytest.mark.asyncio
async def test_chat_timeout_covers_pre_call_queue_wait(monkeypatch):
    class SlowPreCall(BaseMiddleware):
        async def pre_call(self, request: dict) -> dict:
            await asyncio.sleep(1)
            return request

    class UnusedRouter:
        async def complete(self, model, messages, **kwargs):
            raise AssertionError("router should not run after pre-call deadline")

    active_calls._active_calls.clear()
    active_calls._stale_calls.clear()
    monkeypatch.setattr(proxy_app, "_config", ProxyConfig(general=GeneralSettings(master_key="")))
    monkeypatch.setattr(proxy_app, "_router", UnusedRouter())
    monkeypatch.setattr(proxy_app, "_middleware_chain", MiddlewareChain([SlowPreCall()]))

    transport = httpx.ASGITransport(app=proxy_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={"x-caller-skill": "deadline-smoke-test"},
            json={
                "model": "chutes-deepseek",
                "messages": [{"role": "user", "content": "slow pre-call"}],
                "timeout": 0.01,
                "scillm_metadata": {"batch_id": "b1", "item_id": "i2"},
            },
        )

    assert response.status_code == 504
    error = response.json()["error"]
    assert error["details"]["caller"] == "deadline-smoke-test"
    assert error["details"]["item_id"] == "i2"


@pytest.mark.asyncio
async def test_timeout_error_handler_skips_llm_analysis(monkeypatch):
    async def fail_analysis(*args, **kwargs):
        raise AssertionError("timeout errors must not invoke LLM analysis")

    class FakeUrl:
        path = "/v1/chat/completions"

    class FakeRequest:
        headers = {"x-caller-skill": "deadline-smoke-test"}
        method = "POST"
        url = FakeUrl()
        client = None

        async def json(self):
            return {"model": "chutes-deepseek"}

    monkeypatch.setattr("scillm.proxy.errors._analyze_error_with_llm", fail_analysis)

    response = await proxy_error_handler(
        FakeRequest(),
        ProxyError(504, "Request timed out", "timeout_error", details={"item_id": "i3"}),
    )

    assert response.status_code == 504
