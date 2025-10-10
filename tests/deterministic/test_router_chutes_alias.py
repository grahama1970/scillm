import os
import asyncio
import pytest

from litellm.router import Router


class _FakeMsg:
    def __init__(self, content):
        self.content = content


class _FakeResp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": _FakeMsg(content)})()]
        self.additional_kwargs = {}


@pytest.mark.asyncio
async def test_alias_normalization_env_injection(monkeypatch):
    # Set CHUTES env; no explicit api_base/api_key passed
    monkeypatch.setenv("CHUTES_PROVIDER", "openai")
    monkeypatch.setenv("CHUTES_API_BASE", "https://llm.chutes.ai/v1")
    monkeypatch.setenv("CHUTES_API_KEY", "sk-test-xyz")

    router = Router(deterministic=True)

    captured = {}

    async def fake_async_function_with_fallbacks(*args, **kwargs):
        # Capture kwargs to assert env injection and alias normalization occurred upstream
        captured.update({
            "model": kwargs.get("model"),
            "custom_llm_provider": kwargs.get("custom_llm_provider"),
            "api_base": kwargs.get("api_base"),
            "api_key": "set" if kwargs.get("api_key") else None,
        })
        return _FakeResp("hello")

    monkeypatch.setattr(Router, "async_function_with_fallbacks", fake_async_function_with_fallbacks)

    resp = await router.acompletion(
        model="openai/Qwen/Qwen2.5-VL-72B-Instruct",
        messages=[{"role": "user", "content": "hi"}],
        response_format={"type": "json_object"},
    )
    meta = resp.additional_kwargs.get("router", {})
    # Assertions
    assert captured["custom_llm_provider"] == "openai"
    assert captured["model"] == "Qwen/Qwen2.5-VL-72B-Instruct"  # alias normalized
    assert captured["api_base"].startswith("https://")
    assert captured["api_key"] == "set"
    assert meta.get("error_type", "ok") in (None, "ok")


@pytest.mark.asyncio
async def test_multimodal_preservation(monkeypatch):
    router = Router(deterministic=True)

    observed = {"messages": None}

    async def fake_async_function_with_fallbacks(*args, **kwargs):
        observed["messages"] = kwargs.get("messages")
        return _FakeResp('{"ok":true}')

    monkeypatch.setattr(Router, "async_function_with_fallbacks", fake_async_function_with_fallbacks)

    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            ],
        }
    ]
    resp = await router.acompletion(
        model="openai/org/vision-model",
        messages=msgs,
        response_format={"type": "json_object"},
    )
    # Ensure messages stayed as list with both parts
    sent = observed["messages"][0]["content"]
    assert isinstance(sent, list) and len(sent) == 2
    assert sent[1]["type"] == "image_url" and sent[1]["image_url"]["url"].startswith("data:image/")
    assert resp.additional_kwargs.get("router", {}).get("deterministic") is True


@pytest.mark.asyncio
async def test_meta_empty_content(monkeypatch):
    router = Router(deterministic=True)

    async def fake_async_function_with_fallbacks(*args, **kwargs):
        # Return empty content to trigger error_type classification
        return _FakeResp("")

    monkeypatch.setattr(Router, "async_function_with_fallbacks", fake_async_function_with_fallbacks)

    resp = await router.acompletion(
        model="openai/org/model",
        messages=[{"role": "user", "content": "Ping"}],
    )
    assert resp.additional_kwargs.get("router", {}).get("error_type") == "empty_content"

