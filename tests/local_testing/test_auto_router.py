import types

import pytest

from scillm.extras import auto_router as auto_router_mod


def test_auto_router_requires_explicit_flag(monkeypatch):
    monkeypatch.delenv("SCILLM_AUTO_ROUTER", raising=False)
    monkeypatch.setenv("CHUTES_API_BASE_1", "https://llm.chutes.ai/v1")
    monkeypatch.setenv("CHUTES_API_KEY_1", "sk-test")
    with pytest.raises(RuntimeError) as excinfo:
        auto_router_mod.auto_router_from_env(kind="text", require_json=True)
    assert "SCILLM_AUTO_ROUTER is disabled" in str(excinfo.value)


def test_auto_router_enabled_returns_router(monkeypatch):
    monkeypatch.setenv("SCILLM_AUTO_ROUTER", "1")
    monkeypatch.setenv("CHUTES_API_BASE_1", "https://llm.chutes.ai/v1")
    monkeypatch.setenv("CHUTES_API_KEY_1", "sk-test")

    captured_model_list = []

    def fake_auto_model_list_from_env(**kwargs):
        captured_model_list.append(kwargs)
        return [
            {
                "model_name": "chutes/text",
                "litellm_params": {"model": "foo/Bar-235B", "api_base": "https://llm.chutes.ai/v1"},
            }
        ]

    fake_router_calls = []

    class _FakeRouter:
        def __init__(self, model_list=None):
            fake_router_calls.append(model_list)
            self.model_list = model_list or []

    monkeypatch.setattr(auto_router_mod, "auto_model_list_from_env", fake_auto_model_list_from_env)
    monkeypatch.setattr(auto_router_mod, "Router", _FakeRouter)

    router = auto_router_mod.auto_router_from_env(kind="text", require_json=True)

    assert isinstance(router, _FakeRouter)
    assert fake_router_calls and fake_router_calls[0][0]["model_name"] == "chutes/text"
    assert captured_model_list and captured_model_list[0]["require_json"] is True
