from __future__ import annotations

import pytest

from scillm.proxy import app as proxy_app
from scillm.proxy.app import _is_codex_oauth_model, _validate_model_request
from scillm.proxy.config import load_config
from scillm.proxy.errors import ProxyError


class _Request:
    headers: dict[str, str] = {}


def test_gpt_55_is_codex_oauth_model():
    assert _is_codex_oauth_model("gpt-5.5") is True
    assert _is_codex_oauth_model("gpt-5.3-codex") is True
    assert _is_codex_oauth_model("codex-latest") is True
    assert _is_codex_oauth_model("deepseek-ai/DeepSeek-V3.2-TEE") is False


def test_validation_allows_gpt_55_when_codex_available(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(proxy_app, "_VALID_MODEL_ALIASES", {"text"})
    monkeypatch.setattr(proxy_app, "_codex_oauth_available", lambda: True)

    body = {"messages": [{"role": "user", "content": "hi"}]}
    _validate_model_request("gpt-5.5", body, _Request())


def test_validation_rejects_gpt_55_when_codex_unavailable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(proxy_app, "_VALID_MODEL_ALIASES", {"text"})
    monkeypatch.setattr(proxy_app, "_codex_oauth_available", lambda: False)

    body = {"messages": [{"role": "user", "content": "hi"}]}
    with pytest.raises(ProxyError):
        _validate_model_request("gpt-5.5", body, _Request())


def test_config_registers_gpt_55_model_group():
    config = load_config("local/proxy_server_config.yaml")
    group = config.model_groups["gpt-5.5"]

    assert group.deployments[0].model == "gpt-5.5"
    assert group.deployments[0].custom_llm_provider == "codex-oauth"
