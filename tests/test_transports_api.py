"""Deterministic tests for the normalized transport API (issue #28).

The carrier is stubbed so no provider is called; live proofs are separate
(scripts/prove_transports_live.py).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse

import scillm.proxy.transport_profiles as tp
import scillm.proxy.transports_api as ta
from scillm.proxy.errors import ProxyError
from scillm.proxy.transport_profiles import ProfileRegistry, TransportProfile
from scillm.proxy.transports_api import create_transports_router


def profile(**overrides):
    base = dict(
        id="turn",
        label="native turn",
        provider="ollama",
        model="local-text",
        auth_source="none",
        mode="model_turn",
        capabilities=["streaming", "tool_calling", "cancellation", "structured_events"],
    )
    base.update(overrides)
    return TransportProfile(**base)


def make_client(carrier, registry_profiles=None, aliases=None):
    profiles = registry_profiles or [
        profile(),
        profile(
            id="compat",
            mode="opaque_agent_compat",
            provider="opencode-serve",
            capabilities=["streaming", "session_resume", "structured_events"],
        ),
    ]
    reg = ProfileRegistry(profiles, aliases or {})
    tp._registry = reg
    app = FastAPI()
    app.include_router(create_transports_router(lambda r: None, carrier=carrier), prefix="/v1/scillm")

    @app.exception_handler(ProxyError)
    async def _handler(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"error": {"message": exc.message, "type": exc.error_type}})

    return TestClient(app)


def chat_response(content=None, tool_calls=None, reasoning=None, usage=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if reasoning:
        message["reasoning_content"] = reasoning
    return {
        "id": "chatcmpl-upstream-1",
        "model": "resolved-model",
        "choices": [{"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": usage or {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
    }


REQ = {
    "schema": "scillm.transport_request.v1",
    "profile": "turn",
    "correlation": {"tau_run_id": "run-1", "node_id": "node-a", "attempt": 1, "goal_hash": "abc123"},
    "messages": [{"role": "user", "content": "hi"}],
}


def wait_result(client, tid):
    return client.get(f"/v1/scillm/transports/{tid}/result", params={"wait_sec": 5}).json()


class TestSingleTurn:
    def test_one_turn_completes_with_upstream_correlation(self):
        async def carrier(record):
            return chat_response(content="hello there")

        client = make_client(carrier)
        r = client.post("/v1/scillm/transports", json=REQ)
        assert r.status_code == 201
        handle = r.json()
        assert handle["schema"] == "scillm.transport_handle.v1"
        assert handle["correlation"]["goal_hash"] == "abc123"
        result = wait_result(client, handle["transport_id"])
        assert result["ok"] is True
        assert result["state"] == "turn_completed"
        assert result["upstream"]["id"] == "chatcmpl-upstream-1"
        assert result["upstream"]["model"] == "resolved-model"
        assert result["tau_completion"] is None  # transport never claims Tau completion
        events = client.get(f"/v1/scillm/transports/{handle['transport_id']}/events").json()["events"]
        types = [e["type"] for e in events]
        assert types == ["turn_started", "usage", "assistant_message"]
        assert all(e["schema"] == "scillm.transport_event.v1" for e in events)

    def test_unknown_profile_fails_closed(self):
        async def carrier(record):  # pragma: no cover
            raise AssertionError("must not be called")

        client = make_client(carrier)
        r = client.post("/v1/scillm/transports", json={**REQ, "profile": "ghost"})
        assert r.status_code == 404

    def test_missing_required_capability_fails_closed(self):
        async def carrier(record):  # pragma: no cover
            raise AssertionError("must not be called")

        weak = profile(id="turn", capabilities=["streaming"])
        client = make_client(carrier, registry_profiles=[weak])
        r = client.post("/v1/scillm/transports", json={**REQ, "required_capabilities": ["tool_calling"]})
        assert r.status_code == 422
        assert r.json()["error"]["type"] == "transport_capability_unsatisfied"

    def test_tools_against_non_tool_profile_fails_closed(self):
        async def carrier(record):  # pragma: no cover
            raise AssertionError("must not be called")

        weak = profile(id="turn", capabilities=["streaming"])
        client = make_client(carrier, registry_profiles=[weak])
        r = client.post("/v1/scillm/transports", json={**REQ, "tools": [{"type": "function", "function": {"name": "f"}}]})
        assert r.status_code == 422

    def test_wrong_schema_version_fails_closed(self):
        async def carrier(record):  # pragma: no cover
            raise AssertionError("must not be called")

        client = make_client(carrier)
        r = client.post("/v1/scillm/transports", json={**REQ, "schema": "scillm.transport_request.v0"})
        assert r.status_code == 422


class TestFalseGreenGuards:
    @pytest.mark.parametrize(
        "payload,reason",
        [
            (chat_response(reasoning="thinking..."), "reasoning_only_output"),
            (chat_response(content="   "), "empty_terminal_text"),
        ],
    )
    def test_reasoning_only_or_empty_text_never_normalizes_to_success(self, payload, reason):
        async def carrier(record):
            return payload

        client = make_client(carrier)
        tid = client.post("/v1/scillm/transports", json=REQ).json()["transport_id"]
        result = wait_result(client, tid)
        assert result["ok"] is False
        assert result["state"] == "failed"
        assert result["state_reason"] == reason

    def test_provider_error_is_typed_not_success(self):
        async def carrier(record):
            raise ProxyError(502, "upstream exploded", "transport_provider_error")

        client = make_client(carrier)
        tid = client.post("/v1/scillm/transports", json=REQ).json()["transport_id"]
        result = wait_result(client, tid)
        assert result["ok"] is False
        assert result["state"] == "failed"


class TestToolLoop:
    def test_tool_call_turn_awaits_tau_and_next_turn_completes(self):
        calls = []

        async def carrier(record):
            calls.append([dict(m) for m in record.messages])
            if len(calls) == 1:
                return chat_response(tool_calls=[{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{\"path\": \"x\"}"},
                }])
            return chat_response(content="file says 42")

        client = make_client(carrier)
        tid = client.post(
            "/v1/scillm/transports",
            json={**REQ, "tools": [{"type": "function", "function": {"name": "read_file"}}]},
        ).json()["transport_id"]
        result = wait_result(client, tid)
        # pending tool state is not a final answer
        assert result["state"] == "awaiting_tool_result"
        assert result["ok"] is True

        # SciLLM did not execute the tool: Tau supplies the result
        r2 = client.post(
            f"/v1/scillm/transports/{tid}/turns",
            json={"tool_results": [{"tool_call_id": "call_1", "content": "42"}]},
        )
        assert r2.status_code == 202
        result2 = wait_result(client, tid)
        assert result2["state"] == "turn_completed"
        assert result2["turn"] == 1
        # second provider call saw the tool message appended by Tau's input
        assert any(m.get("role") == "tool" and m.get("tool_call_id") == "call_1" for m in calls[1])
        # exactly two provider calls — no autonomous agent loop in between
        assert len(calls) == 2

    def test_next_turn_without_tool_results_when_awaiting_fails(self):
        async def carrier(record):
            return chat_response(tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}])

        client = make_client(carrier)
        tid = client.post("/v1/scillm/transports", json=REQ).json()["transport_id"]
        wait_result(client, tid)
        r = client.post(f"/v1/scillm/transports/{tid}/turns", json={"messages": [{"role": "user", "content": "go on"}]})
        assert r.status_code == 422


class TestCancelAndUnsupported:
    @pytest.mark.asyncio
    async def test_cancel_in_flight_turn(self):
        # A persistent loop (httpx ASGI transport) so the in-flight provider
        # turn survives across requests, as it does under uvicorn.
        import httpx

        started = asyncio.Event()

        async def carrier(record):
            started.set()
            await asyncio.sleep(30)
            return chat_response(content="too late")  # pragma: no cover

        profiles = [profile()]
        tp._registry = ProfileRegistry(profiles, {})
        app = FastAPI()
        app.include_router(create_transports_router(lambda r: None, carrier=carrier), prefix="/v1/scillm")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            tid = (await client.post("/v1/scillm/transports", json=REQ)).json()["transport_id"]
            await asyncio.wait_for(started.wait(), timeout=5)
            r = await client.post(f"/v1/scillm/transports/{tid}/cancel")
            assert r.status_code == 200
            assert r.json()["state"] == "cancelled"
            result = (await client.get(f"/v1/scillm/transports/{tid}/result")).json()
            assert result["ok"] is False
            assert result["state"] == "cancelled"

    def test_cancel_with_nothing_in_flight_is_typed_unsupported(self):
        async def carrier(record):
            return chat_response(content="done")

        client = make_client(carrier)
        tid = client.post("/v1/scillm/transports", json=REQ).json()["transport_id"]
        wait_result(client, tid)
        r = client.post(f"/v1/scillm/transports/{tid}/cancel")
        assert r.status_code == 409
        assert r.json()["error"]["type"] == "unsupported"

    def test_next_turn_on_failed_transport_is_typed_unsupported(self):
        async def carrier(record):
            return chat_response(content="  ")

        client = make_client(carrier)
        tid = client.post("/v1/scillm/transports", json=REQ).json()["transport_id"]
        wait_result(client, tid)
        r = client.post(f"/v1/scillm/transports/{tid}/turns", json={"messages": [{"role": "user", "content": "again"}]})
        assert r.status_code == 409
        assert r.json()["error"]["type"] == "unsupported"


class TestOpaqueCompat:
    def test_compat_profile_cannot_be_driven_as_native_turn(self):
        async def carrier(record):  # pragma: no cover
            raise AssertionError("must not be called")

        client = make_client(carrier)
        r = client.post("/v1/scillm/transports", json={**REQ, "profile": "compat"})
        assert r.status_code == 409
        body = r.json()
        assert body["state"] == "fork_required"
        assert body["error"]["type"] == "fork_required"
        assert body["error"]["native_surface"] == "/v1/scillm/opencode"
        assert "tool_calling" not in body["error"]["reduced_capabilities"]


class TestAuthTyped:
    def test_upstream_401_is_typed_auth_failure_not_empty_output(self):
        """Issue #29: a 401 from the chat surface must fail the turn with
        transport_auth_invalid in events and result — never empty output."""

        async def carrier(record):
            raise ProxyError(
                401,
                "provider turn rejected with HTTP 401: invalid or stale proxy credentials",
                "transport_auth_invalid",
            )

        client = make_client(carrier)
        tid = client.post("/v1/scillm/transports", json=REQ).json()["transport_id"]
        result = wait_result(client, tid)
        assert result["ok"] is False
        assert result["state"] == "failed"
        assert result["error_type"] == "transport_auth_invalid"
        events = client.get(f"/v1/scillm/transports/{tid}/events").json()["events"]
        errs = [e for e in events if e["type"] == "provider_error"]
        assert errs and errs[-1]["data"]["type"] == "transport_auth_invalid"

    def test_bad_bearer_on_create_is_401_not_silent(self):
        async def carrier(record):  # pragma: no cover
            raise AssertionError("must not be called")

        profiles = [profile()]
        tp._registry = ProfileRegistry(profiles, {})
        app = FastAPI()
        app.include_router(
            create_transports_router(lambda r: "Invalid API key", carrier=carrier), prefix="/v1/scillm"
        )

        @app.exception_handler(ProxyError)
        async def _handler(request, exc):
            return JSONResponse(
                status_code=exc.status_code,
                content={"error": {"message": exc.message, "type": exc.error_type}},
            )

        client = TestClient(app)
        r = client.post("/v1/scillm/transports", json=REQ)
        assert r.status_code == 401
        assert r.json()["error"]["type"] == "authentication_error"

    @pytest.mark.asyncio
    async def test_default_carrier_maps_401_to_transport_auth_invalid(self):
        from unittest.mock import patch as _patch

        import httpx as _httpx

        from scillm.proxy import transports_api as ta_mod

        record = ta_mod.TransportRecord(
            ta_mod.TransportCreateRequest(**{**REQ, "profile": "turn"}), profile()
        )

        class FakeResp:
            status_code = 401
            text = '{"error":{"message":"Invalid API key","type":"authentication_error"}}'

        class FakeClient:
            def __init__(self, *a, **k): ...
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def post(self, *a, **k): return FakeResp()

        with _patch.object(_httpx, "AsyncClient", FakeClient):
            with pytest.raises(ProxyError) as ei:
                await ta_mod._default_carrier(record)
        assert ei.value.error_type == "transport_auth_invalid"
        assert ei.value.status_code == 401
