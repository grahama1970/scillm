from __future__ import annotations

import json

import httpx
import pytest

from scillm.proxy import app as proxy_app
from scillm.proxy.app import (
    _DEFAULT_MODEL_POOLS,
    _batch_sse_event,
    _collect_chat_sse_lines,
    _item_id,
    _lane_for_index,
    _messages_for_batch_item,
    _model_pool,
    _model_pool_status,
    _weighted_lane_sequence,
)
from scillm.proxy.config import GeneralSettings, ProxyConfig
from scillm.proxy.errors import ProxyError
from scillm.proxy.middleware import MiddlewareChain


def test_default_qra_pool_uses_chutes_and_opencode_lanes():
    pool = _model_pool("qra-deepseek-pool")

    assert pool is not None
    assert pool["description"].startswith("QRA extraction pool using independent")
    assert pool["lanes"] == [
        {
            "name": "qra-chutes-deepseek",
            "provider": "chutes",
            "model": "",
            "weight": 3,
            "max_concurrency": 5,
            "timeout": 420.0,
            "require_hot_chutes": True,
        },
        {
            "name": "qra-opencode-go-deepseek-v4-flash",
            "provider": "opencode-go",
            "model": "opencode-go/deepseek-v4-flash",
            "weight": 2,
            "max_concurrency": 4,
            "timeout": 620.0,
            "require_hot_chutes": False,
        },
    ]


def test_weighted_lane_sequence_expands_weights():
    lanes = _DEFAULT_MODEL_POOLS["qra-deepseek-pool"]["lanes"]
    sequence = _weighted_lane_sequence(lanes)

    assert [lane["name"] for lane in sequence] == [
        "qra-chutes-deepseek",
        "qra-chutes-deepseek",
        "qra-chutes-deepseek",
        "qra-opencode-go-deepseek-v4-flash",
        "qra-opencode-go-deepseek-v4-flash",
    ]


def test_lane_for_index_uses_weighted_round_robin():
    lanes = _DEFAULT_MODEL_POOLS["qra-deepseek-pool"]["lanes"]

    assigned = [_lane_for_index(lanes, index)["name"] for index in range(7)]

    assert assigned == [
        "qra-chutes-deepseek",
        "qra-chutes-deepseek",
        "qra-chutes-deepseek",
        "qra-opencode-go-deepseek-v4-flash",
        "qra-opencode-go-deepseek-v4-flash",
        "qra-chutes-deepseek",
        "qra-chutes-deepseek",
    ]


def test_batch_item_accepts_messages_or_prompt():
    assert _messages_for_batch_item({"messages": [{"role": "user", "content": "hi"}]}) == [
        {"role": "user", "content": "hi"}
    ]
    assert _messages_for_batch_item({"prompt": "hi"}) == [{"role": "user", "content": "hi"}]


def test_batch_item_requires_content():
    with pytest.raises(ProxyError):
        _messages_for_batch_item({"id": "empty"})


def test_item_id_prefers_explicit_fields():
    assert _item_id({"item_id": "a", "id": "b"}, 0) == "a"
    assert _item_id({"id": "b"}, 0) == "b"
    assert _item_id({}, 2) == "item-3"


def test_batch_sse_event_formats_named_json_event():
    assert _batch_sse_event("item_completed", {"item_id": "a", "ok": True}) == (
        'event: item_completed\ndata: {"item_id": "a", "ok": true}\n\n'
    )


@pytest.mark.asyncio
async def test_collect_chat_sse_lines_reassembles_content_and_usage():
    async def lines():
        yield "event: started"
        yield 'data: {"model": "oc-kimi"}'
        yield 'data: {"choices": [{"delta": {"content": "hel"}}]}'
        yield 'data: {"choices": [{"delta": {"content": "lo"}}], "usage": {"total_tokens": 7}}'
        yield "data: [DONE]"

    data = await _collect_chat_sse_lines(lines(), requested_model="oc-kimi")

    assert data["model"] == "oc-kimi"
    assert data["choices"][0]["message"]["content"] == "hello"
    assert data["usage"]["total_tokens"] == 7


@pytest.mark.asyncio
async def test_collect_chat_sse_lines_requires_done():
    async def lines():
        yield 'data: {"choices": [{"delta": {"content": "partial"}}]}'

    with pytest.raises(ProxyError, match="stream ended without"):
        await _collect_chat_sse_lines(lines(), requested_model="oc-kimi")


@pytest.mark.asyncio
async def test_batch_stream_endpoint_uses_inner_streaming(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict] = []

    class StreamingRouter:
        async def complete(self, model, messages, **kwargs):
            calls.append({"model": model, "stream": kwargs.get("stream")})

            async def chunks():
                yield b'data: {"model": "served-model", "choices": [{"delta": {"content": "{\\"ok\\": "}}]}\n\n'
                yield b'data: {"choices": [{"delta": {"content": "true}"}}], "usage": {"total_tokens": 3}}\n\n'
                yield b"data: [DONE]\n\n"

            return chunks()

    monkeypatch.setattr(proxy_app, "_config", ProxyConfig(general=GeneralSettings(master_key="")))
    monkeypatch.setattr(proxy_app, "_router", StreamingRouter())
    monkeypatch.setattr(proxy_app, "_middleware_chain", MiddlewareChain([]))
    monkeypatch.setitem(
        _DEFAULT_MODEL_POOLS["qra-deepseek-pool"]["lanes"][0],
        "model",
        "Qwen/Qwen3.6-27B-TEE",
    )

    transport = httpx.ASGITransport(app=proxy_app.app)
    events: list[tuple[str, dict]] = []
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/v1/scillm/batch/completions/stream",
            headers={"x-caller-skill": "create-qras"},
            json={
                "model_pool": "qra-deepseek-pool",
                "batch_id": "stream-test",
                "items": [{"id": "item-a", "messages": [{"role": "user", "content": "hi"}]}],
            },
        ) as response:
            assert response.status_code == 200
            event_name = "message"
            async for line in response.aiter_lines():
                if not line:
                    continue
                if line.startswith("event:"):
                    event_name = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    events.append((event_name, json.loads(line[len("data:"):].strip())))

    assert calls and calls[0]["stream"] is True
    completed = [data for name, data in events if name == "item_completed"]
    assert completed
    assert completed[0]["item_id"] == "item-a"
    assert completed[0]["content"] == '{"ok": true}'
    assert any(name == "batch_done" for name, _ in events)


@pytest.mark.asyncio
async def test_batch_stream_lane_offset_distributes_single_item_requests(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict] = []

    class StreamingRouter:
        async def complete(self, model, messages, **kwargs):
            calls.append({"model": model, "stream": kwargs.get("stream")})

            async def chunks():
                yield b'data: {"choices": [{"delta": {"content": "{\\"ok\\": true}"}}]}\n\n'
                yield b"data: [DONE]\n\n"

            return chunks()

    monkeypatch.setattr(proxy_app, "_config", ProxyConfig(general=GeneralSettings(master_key="")))
    monkeypatch.setattr(proxy_app, "_router", StreamingRouter())
    monkeypatch.setattr(proxy_app, "_middleware_chain", MiddlewareChain([]))

    transport = httpx.ASGITransport(app=proxy_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/v1/scillm/batch/completions/stream",
            headers={"x-caller-skill": "create-qras"},
            json={
                "model_pool": "qra-deepseek-pool",
                "batch_id": "stream-offset-test",
                "lane_offset": 3,
                "items": [{"id": "item-a", "messages": [{"role": "user", "content": "hi"}]}],
            },
        ) as response:
            assert response.status_code == 200
            async for _line in response.aiter_lines():
                pass

    assert calls
    assert calls[0]["model"] == "opencode-go/deepseek-v4-flash"


def test_model_pool_status_reports_aggregate_and_lanes():
    pool = _model_pool("qra-deepseek-pool")
    assert pool is not None

    status = _model_pool_status(
        "qra-deepseek-pool",
        pool,
        {
            "chutes": {
                "effective_limit": 4,
                "configured_limit": 4,
                "in_flight": 3,
                "queued": 1,
                "registry_in_flight": 3,
                "semaphore_in_flight": 3,
                "drift": 0,
            },
        },
    )

    assert status["in_flight"] == 3
    assert status["limit"] == 8
    assert status["queued"] == 1
    assert [lane["provider"] for lane in status["lanes"]] == ["chutes", "opencode-go"]


def test_model_pool_status_uses_live_semaphore_not_stale_registry_rows():
    pool = _model_pool("qra-deepseek-pool")
    assert pool is not None

    status = _model_pool_status(
        "qra-deepseek-pool",
        pool,
        {
            "chutes": {
                "effective_limit": 4,
                "configured_limit": 4,
                "in_flight": 1,
                "live_in_flight": 1,
                "semaphore_in_flight": 1,
                "registry_in_flight": 9,
                "stale_active_calls": 8,
                "registry_drift": 8,
            },
        },
    )

    chutes_lane = next(lane for lane in status["lanes"] if lane["provider"] == "chutes")
    assert chutes_lane["live_in_flight"] == 1
    assert chutes_lane["stale_active_calls"] == 8
    assert chutes_lane["registry_drift"] == 8
    assert status["live_in_flight"] == 1


class TestOpsChutesPreflightAvailability:
    """Issue #14: checker-unavailable must not block healthy models."""

    @staticmethod
    def _run(coro):
        import asyncio

        return asyncio.get_event_loop().run_until_complete(coro)

    def test_checker_unavailable_proceeds_unverified(self, monkeypatch):
        import asyncio

        from scillm.proxy import chutes_direct as cd

        async def fake_run(*args, timeout=60.0):
            return {"ok": False, "error": "ops_chutes_run_not_found", "returncode": 127}

        monkeypatch.setattr(cd, "_run_ops_chutes", fake_run)
        plan = asyncio.run(cd._ops_chutes_model_plan("deepseek-ai/DeepSeek-V3.2-TEE"))
        assert plan["action"] == "proceed_unverified"
        assert plan["preflight_unverified_reason"] == "ops_chutes_run_not_found"
        assert plan.get("error") is None

    def test_real_health_failure_still_blocks(self, monkeypatch):
        import asyncio

        from scillm.proxy import chutes_direct as cd

        async def fake_run(*args, timeout=60.0):
            return {"ok": False, "error": "model reported DOWN", "stdout": "DOWN", "returncode": 1}

        monkeypatch.setattr(cd, "_run_ops_chutes", fake_run)
        plan = asyncio.run(cd._ops_chutes_model_plan("deepseek-ai/DeepSeek-V3.2-TEE"))
        assert plan["action"] == "health_check_failed"
        assert plan["error"] == "ops_chutes_model_health_failed"
