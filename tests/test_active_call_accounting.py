from __future__ import annotations

import time

import pytest

from chutes.middleware import active_calls
from chutes.middleware.active_calls import (
    ActiveCall,
    ActiveCallsMiddleware,
    clear_active_calls,
    evict_stale_active_calls,
)
from chutes.middleware import concurrency_guard


def test_stale_active_calls_are_evicted(monkeypatch):
    active_calls._active_calls.clear()
    active_calls._active_calls["stale"] = ActiveCall(
        call_id="stale",
        model="opencode-go/deepseek-v4-flash",
        caller="create-qras",
        started_at=time.monotonic() - 700,
        started_ts="2026-04-25T00:00:00+00:00",
        provider="opencode-go",
        timeout_s=10,
    )

    evicted = evict_stale_active_calls()

    assert len(evicted) == 1
    assert evicted[0]["call_id"] == "stale"
    assert active_calls._active_calls == {}


def test_clear_active_calls_filters_old_create_qras_rows():
    active_calls._active_calls.clear()
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
