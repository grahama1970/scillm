from __future__ import annotations

import httpx
import pytest

from scillm.proxy import app as proxy_app
from scillm.proxy.errors import ProviderOAuthError
from scillm.proxy.providers import claude, codex


@pytest.mark.asyncio
async def test_codex_missing_oauth_token_is_structured(monkeypatch):
    monkeypatch.setattr(codex, "get_codex_credentials", lambda *a, **k: None)

    with pytest.raises(ProviderOAuthError) as exc_info:
        await codex.codex_completion("gpt-5.5", [{"role": "user", "content": "hi"}])

    err = exc_info.value.to_dict()["error"]
    assert err["type"] == "provider_auth_error"
    assert err["details"]["provider"] == "codex-oauth"
    assert err["details"]["provider_error_code"] == "PROVIDER_AUTH_FAILED"
    assert err["details"]["model_requested"] == "gpt-5.5"
    assert "codex login" in err["details"]["project_agent_message"]


@pytest.mark.asyncio
async def test_codex_forced_refresh_failure_raises_structured_oauth_error(monkeypatch):
    calls: list[bool] = []

    def fake_credentials(*, force_refresh: bool = False):
        calls.append(force_refresh)
        if force_refresh:
            return None
        return ("stale-access-token", "acct")

    class FakeStream:
        status_code = 401

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def aread(self):
            return b'{"error":{"code":"token_revoked","message":"Encountered invalidated oauth token"}}'

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, *args, **kwargs):
            return FakeStream()

    monkeypatch.setattr(codex, "get_codex_credentials", fake_credentials)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    with pytest.raises(ProviderOAuthError) as exc_info:
        await codex.codex_completion("gpt-5.5", [{"role": "user", "content": "hi"}])

    err = exc_info.value.to_dict()["error"]
    assert calls == [False, True, True]
    assert err["type"] == "provider_auth_error"
    assert err["details"]["provider"] == "codex-oauth"
    assert err["details"]["provider_error_code"] == "PROVIDER_AUTH_FAILED"
    assert err["details"]["provider_auth_status"] == "not_configured_or_expired"
    assert "codex login" in err["details"]["project_agent_message"]


@pytest.mark.asyncio
async def test_claude_missing_oauth_token_is_structured(monkeypatch):
    monkeypatch.setattr(claude, "get_anthropic_token", lambda: None)

    with pytest.raises(ProviderOAuthError) as exc_info:
        await claude.claude_completion("claude-sonnet-4-20250514", [{"role": "user", "content": "hi"}])

    err = exc_info.value.to_dict()["error"]
    assert err["type"] == "provider_auth_error"
    assert err["details"]["provider"] == "anthropic-oauth"
    assert err["details"]["provider_error_code"] == "PROVIDER_AUTH_FAILED"
    assert err["details"]["model_requested"] == "claude-sonnet-4-20250514"
    assert "claude auth login" in err["details"]["project_agent_message"]


@pytest.mark.asyncio
async def test_claude_forced_refresh_retries_revoked_access_token(monkeypatch):
    token_calls: list[bool] = []
    auth_headers: list[str] = []

    def fake_token(*, force_refresh: bool = False):
        token_calls.append(force_refresh)
        return "fresh-access-token" if force_refresh else "stale-access-token"

    class FakeResponse:
        def __init__(self, status_code: int, text: str, payload: dict | None = None):
            self.status_code = status_code
            self.text = text
            self._payload = payload or {}

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, _url, *, json, headers):
            auth_headers.append(headers["Authorization"])
            if len(auth_headers) == 1:
                return FakeResponse(
                    401,
                    '{"error":{"type":"authentication_error","message":"OAuth access token has been revoked"}}',
                )
            return FakeResponse(
                200,
                "{}",
                {
                    "model": "claude-sonnet-4-6",
                    "content": [{"type": "text", "text": "OK"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )

    monkeypatch.setattr(claude, "get_anthropic_token", fake_token)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await claude.claude_completion(
        "claude-sonnet-4-6",
        [{"role": "user", "content": "Return exactly OK."}],
    )

    assert token_calls == [False, True]
    assert auth_headers == ["Bearer stale-access-token", "Bearer fresh-access-token"]
    assert result.choices[0].message.content == "OK"


@pytest.mark.asyncio
async def test_claude_retries_without_deprecated_temperature(monkeypatch):
    request_bodies: list[dict] = []

    class FakeResponse:
        def __init__(self, status_code: int, text: str, payload: dict | None = None):
            self.status_code = status_code
            self.text = text
            self._payload = payload or {}

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, _url, *, json, headers):
            request_bodies.append(dict(json))
            if len(request_bodies) == 1:
                return FakeResponse(
                    400,
                    '{"error":{"type":"invalid_request_error","message":"`temperature` is deprecated for this model."}}',
                )
            return FakeResponse(
                200,
                "{}",
                {
                    "model": "claude-opus-4-8",
                    "content": [{"type": "text", "text": "OK"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )

    monkeypatch.setattr(claude, "get_anthropic_token", lambda **_kwargs: "access-token")
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    result = await claude.claude_completion(
        "claude-opus-4-8",
        [{"role": "user", "content": "Return exactly OK."}],
        temperature=0,
    )

    assert request_bodies[0]["temperature"] == 0
    assert "temperature" not in request_bodies[1]
    assert result.choices[0].message.content == "OK"


def test_scillm_reasoning_and_multimodal_proof_for_gpt55():
    body = {
        "model": "gpt-5.5",
        "reasoning_effort": "high",
    }
    response = {
        "model": "gpt-5.5",
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
    }
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "inspect"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        ],
    }]

    proxy_app._attach_proof_fields(body, response, messages)

    assert response["scillm_reasoning"] == {
        "requested_effort": "high",
        "applied_effort": "high",
        "forwarded": True,
        "provider_field": "reasoning.effort",
        "ignored_reason": None,
    }
    assert response["scillm_multimodal"]["input_multimodal"] is True
    assert response["scillm_multimodal"]["image_count"] == 1
    assert response["scillm_multimodal"]["image_seen_by"] == "codex-oauth"


def test_provider_oauth_error_exposes_model_mismatch_details():
    exc = ProviderOAuthError(
        provider="codex-oauth",
        status_code=502,
        message="Codex served model 'gpt-5.4' for requested model 'gpt-5.3-codex'.",
        provider_error_code="PROVIDER_MODEL_MISMATCH",
        model_requested="gpt-5.3-codex",
        model_served="gpt-5.4",
        provider_auth_status="configured",
        project_agent_message="Requested and served model differ.",
    )

    err = exc.to_dict()["error"]
    assert err["type"] == "provider_error"
    assert err["details"]["provider_error_code"] == "PROVIDER_MODEL_MISMATCH"
    assert err["details"]["model_requested"] == "gpt-5.3-codex"
    assert err["details"]["model_served"] == "gpt-5.4"
