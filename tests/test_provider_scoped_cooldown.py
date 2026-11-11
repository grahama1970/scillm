import asyncio
import contextlib
import json
import time

import pytest
import pytest_asyncio

from chutes.middleware import concurrency_guard as guard
from scillm.proxy.config import Deployment, ModelGroup, ProxyConfig


def _deployment(model: str, api_base: str, provider: str) -> Deployment:
    return Deployment(
        model=model,
        api_base=api_base,
        api_key=None,
        custom_llm_provider=provider,
    )


CONFIG = ProxyConfig(
    model_groups={
        "claude-sonnet": ModelGroup(
            name="claude-sonnet",
            deployments=[_deployment("claude-sonnet", "https://api.anthropic.com", "anthropic-oauth")],
        ),
        "claude-opus": ModelGroup(
            name="claude-opus",
            deployments=[_deployment("claude-opus", "https://api.anthropic.com", "anthropic-oauth")],
        ),
        "claude-haiku": ModelGroup(
            name="claude-haiku",
            deployments=[_deployment("claude-haiku", "https://api.anthropic.com", "anthropic-oauth")],
        ),
        "claude-fable": ModelGroup(
            name="claude-fable",
            deployments=[_deployment("claude-fable", "https://api.anthropic.com", "anthropic-oauth")],
        ),
        "zai-glm": ModelGroup(
            name="zai-glm",
            deployments=[_deployment("glm-5.1", "https://api.z.ai/api/coding/paas/v4", "")],
        ),
        "zai-glm-flash": ModelGroup(
            name="zai-glm-flash",
            deployments=[_deployment("glm-5.3-flash", "https://api.z.ai/api/paas/v4", "")],
        ),
        "local-text": ModelGroup(
            name="local-text",
            deployments=[_deployment("local-text", "http://localhost:11434/v1", "")],
        ),
        "gpt-5.5": ModelGroup(
            name="gpt-5.5",
            deployments=[_deployment("gpt-5.5", "https://api.openai.com", "codex-oauth")],
        ),
    },
    aliases={
        "sonnet": "claude-sonnet",
        "opus": "claude-opus",
        "haiku": "claude-haiku",
        "fable": "claude-fable",
    },
)


@pytest_asyncio.fixture(autouse=True)
async def reset_guard_state(monkeypatch):
    snapshots = {
        "paused": dict(guard._paused_until),
        "semaphores": dict(guard._semaphores),
        "in_flight": dict(guard._in_flight),
        "queue_depth": dict(guard._queue_depth),
        "slots": {key: dict(value) for key, value in guard._slot_acquired_at.items()},
        "effective": dict(guard._effective_limits),
        "hits": {key: list(value) for key, value in guard._rate_limit_hits.items()},
        "recovery": dict(guard._last_recovery),
        "limits": dict(guard.PROVIDER_LIMITS),
        "scope_map": dict(guard._routing_scope_map),
        "routing_config": guard._routing_config,
        "last_stale": guard._last_stale_check,
        "request_counter": guard._request_counter,
        "state_loaded": guard._state_loaded,
        "cleanup_restarts": guard._cleanup_restart_count,
        "stale_task": guard._stale_cleanup_task,
    }

    async def no_persist():
        return None

    monkeypatch.setattr(guard, "_persist_state", no_persist)
    guard._paused_until.clear()
    guard._semaphores.clear()
    guard._in_flight.clear()
    guard._queue_depth.clear()
    guard._slot_acquired_at.clear()
    guard._effective_limits.clear()
    guard._rate_limit_hits.clear()
    guard._last_recovery.clear()
    guard._last_stale_check = 0.0
    guard._request_counter = 0
    guard._state_loaded = False
    guard._cleanup_restart_count = 0
    guard._stale_cleanup_task = None
    guard.configure_routing_scopes(CONFIG)

    yield

    task = guard._stale_cleanup_task
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    guard._stale_cleanup_task = None
    guard._paused_until.clear()
    guard._paused_until.update(snapshots["paused"])
    guard._semaphores.clear()
    guard._semaphores.update(snapshots["semaphores"])
    guard._in_flight.clear()
    guard._in_flight.update(snapshots["in_flight"])
    guard._queue_depth.clear()
    guard._queue_depth.update(snapshots["queue_depth"])
    guard._slot_acquired_at.clear()
    guard._slot_acquired_at.update(snapshots["slots"])
    guard._effective_limits.clear()
    guard._effective_limits.update(snapshots["effective"])
    guard._rate_limit_hits.clear()
    guard._rate_limit_hits.update(snapshots["hits"])
    guard._last_recovery.clear()
    guard._last_recovery.update(snapshots["recovery"])
    guard.PROVIDER_LIMITS.clear()
    guard.PROVIDER_LIMITS.update(snapshots["limits"])
    guard._routing_scope_map.clear()
    guard._routing_scope_map.update(snapshots["scope_map"])
    guard._routing_config = snapshots["routing_config"]
    guard._last_stale_check = snapshots["last_stale"]
    guard._request_counter = snapshots["request_counter"]
    guard._state_loaded = snapshots["state_loaded"]
    guard._cleanup_restart_count = snapshots["cleanup_restarts"]
    guard._stale_cleanup_task = snapshots["stale_task"]


def test_loaded_routing_config_resolves_canonical_quota_domains():
    assert guard._resolve_provider("sonnet", CONFIG) == "claude"
    assert guard._resolve_provider("opus", CONFIG) == "claude"
    assert guard._resolve_provider("haiku", CONFIG) == "claude"
    assert guard._resolve_provider("fable", CONFIG) == "claude"
    assert guard._resolve_provider("zai-glm", CONFIG) == "zai"
    assert guard._resolve_provider("zai-glm-flash", CONFIG) == "zai"
    assert guard._resolve_provider("local-text", CONFIG) == "ollama"
    assert guard._resolve_provider("gpt-5.5", CONFIG) == "codex"
    assert guard._resolve_provider("sonnet", CONFIG) != guard._resolve_provider("zai-glm", CONFIG)


@pytest.mark.asyncio
async def test_glm_pause_does_not_delay_sonnet_dispatch():
    guard._record_429("zai", retry_after=60)
    middleware = guard.ConcurrencyMiddleware(CONFIG)

    started = time.monotonic()
    request = await middleware.pre_call({"model": "sonnet"})
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert request["_concurrency_provider"] == "claude"
    await middleware.on_error(request, RuntimeError("cancelled"))
    assert guard._paused_until["zai"] > guard.time.monotonic()


class _Synthetic429Error(RuntimeError):
    status_code = 429


@pytest.mark.asyncio
async def test_zai_429_pauses_shared_scope_but_not_claude():
    middleware = guard.ConcurrencyMiddleware(CONFIG)
    zai_request = await middleware.pre_call({"model": "zai-glm"})

    await middleware.on_error(zai_request, _Synthetic429Error("synthetic 429"))
    await asyncio.sleep(0)

    claude_request = await asyncio.wait_for(
        middleware.pre_call({"model": "sonnet"}),
        timeout=0.2,
    )
    assert claude_request["_concurrency_provider"] == "claude"
    assert guard._paused_until["zai"] > guard.time.monotonic()

    blocked_flash = asyncio.create_task(middleware.pre_call({"model": "zai-glm-flash"}))
    await asyncio.sleep(0)
    assert not blocked_flash.done()
    blocked_flash.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked_flash

    await middleware.on_error(claude_request, RuntimeError("cancelled"))
    assert guard._in_flight["zai"] == 0
    assert guard._in_flight["claude"] == 0
    assert guard._queue_depth["zai"] == 0
    assert guard._queue_depth["claude"] == 0


@pytest.mark.asyncio
async def test_anthropic_aliases_preserve_configured_two_slot_limit():
    middleware = guard.ConcurrencyMiddleware(CONFIG)
    first = await middleware.pre_call({"model": "sonnet"})
    second = await middleware.pre_call({"model": "opus"})
    third_task = asyncio.create_task(middleware.pre_call({"model": "haiku"}))
    await asyncio.sleep(0.01)

    assert guard._resolve_provider("sonnet", CONFIG) == "claude"
    assert guard._resolve_provider("opus", CONFIG) == "claude"
    assert guard._resolve_provider("haiku", CONFIG) == "claude"
    assert guard._in_flight["claude"] == 2
    assert guard._queue_depth["claude"] == 1
    assert not third_task.done()

    await middleware.on_error(first, RuntimeError("cancelled"))
    third = await asyncio.wait_for(third_task, timeout=0.5)
    await middleware.on_error(second, RuntimeError("cancelled"))
    await middleware.on_error(third, RuntimeError("cancelled"))
    assert guard._in_flight["claude"] == 0


@pytest.mark.asyncio
async def test_zai_aliases_share_cooldown_and_cancellation_preserves_pause():
    guard._record_429("zai", retry_after=60)
    middleware = guard.ConcurrencyMiddleware(CONFIG)
    coding_task = asyncio.create_task(middleware.pre_call({"model": "zai-glm"}))
    flash_task = asyncio.create_task(middleware.pre_call({"model": "zai-glm-flash"}))
    await asyncio.sleep(0)

    assert not coding_task.done()
    assert not flash_task.done()
    assert guard._resolve_provider("zai-glm", CONFIG) == guard._resolve_provider("zai-glm-flash", CONFIG) == "zai"
    coding_task.cancel()
    flash_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await coding_task
    with pytest.raises(asyncio.CancelledError):
        await flash_task
    assert guard._paused_until["zai"] > guard.time.monotonic()


@pytest.mark.asyncio
async def test_deadline_cancellation_does_not_clear_provider_pause():
    guard._record_429("zai", retry_after=60)
    middleware = guard.ConcurrencyMiddleware(CONFIG)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(middleware.pre_call({"model": "zai-glm"}), timeout=0.02)

    assert guard._paused_until["zai"] > guard.time.monotonic()


@pytest.mark.asyncio
async def test_effect_readback_shows_independent_pause_and_alias_slot_cap():
    guard._record_429("zai", retry_after=60)
    middleware = guard.ConcurrencyMiddleware(CONFIG)

    sonnet_request = await middleware.pre_call({"model": "sonnet"})
    first_anthropic = await middleware.pre_call({"model": "opus"})
    blocked_zai = asyncio.create_task(middleware.pre_call({"model": "zai-glm-flash"}))
    blocked_anthropic = asyncio.create_task(middleware.pre_call({"model": "haiku"}))
    await asyncio.sleep(0.01)

    status = guard.get_concurrency_status()
    effect = {
        "provider_a": {"scope": "zai", "paused": status["zai"]["paused"]},
        "provider_b": {
            "scope": "claude",
            "dispatchable": sonnet_request["_concurrency_provider"] == "claude",
        },
        "provider_a_still_paused": guard.get_concurrency_status()["zai"]["paused"],
        "anthropic_alias_slot_cap": {
            "configured": status["claude"]["configured_limit"],
            "in_flight": status["claude"]["in_flight"],
            "third_alias_queued": guard._queue_depth["claude"] == 1,
        },
    }
    print("EFFECT_READBACK " + json.dumps(effect, sort_keys=True))

    assert effect["provider_a"]["paused"] is True
    assert effect["provider_b"]["dispatchable"] is True
    assert effect["provider_a_still_paused"] is True
    assert effect["anthropic_alias_slot_cap"] == {
        "configured": 2,
        "in_flight": 2,
        "third_alias_queued": True,
    }

    blocked_zai.cancel()
    blocked_anthropic.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked_zai
    with pytest.raises(asyncio.CancelledError):
        await blocked_anthropic
    await middleware.on_error(sonnet_request, RuntimeError("cancelled"))
    await middleware.on_error(first_anthropic, RuntimeError("cancelled"))
