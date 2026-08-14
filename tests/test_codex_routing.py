from __future__ import annotations

import pytest

from scillm.proxy import app as proxy_app
from scillm.proxy.app import _is_codex_oauth_model, _is_public_selectable_model, _validate_model_request
from scillm.proxy.config import load_config
from scillm.proxy.errors import ProxyError
from scillm.proxy.providers.codex import _openai_messages_to_codex_input, _parse_codex_response


class _Request:
    headers: dict[str, str] = {}


def test_gpt_55_is_codex_oauth_model():
    assert _is_codex_oauth_model("gpt-5.5") is True
    assert _is_codex_oauth_model("gpt-5.3-codex") is False
    assert _is_codex_oauth_model("codex-latest") is True
    assert _is_codex_oauth_model("deepseek-ai/DeepSeek-V3.2-TEE") is False


def test_chutes_provider_ids_are_not_public_selector_models():
    assert _is_public_selectable_model("Qwen/Qwen3.6-27B-TEE") is False
    assert _is_public_selectable_model("deepseek-ai/DeepSeek-V3.2-TEE") is False
    assert _is_public_selectable_model("gpt-5.5") is True


def test_validation_allows_gpt_55_when_codex_available(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(proxy_app, "_VALID_MODEL_ALIASES", {"chutes-deepseek"})
    monkeypatch.setattr(proxy_app, "_codex_oauth_available", lambda: True)

    body = {"messages": [{"role": "user", "content": "hi"}]}
    _validate_model_request("gpt-5.5", body, _Request())


def test_validation_allows_dynamic_claude_provider_model(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(proxy_app, "_VALID_MODEL_ALIASES", {"chutes-deepseek"})
    monkeypatch.setattr(proxy_app, "_claude_oauth_available", lambda: True)

    body = {"messages": [{"role": "user", "content": "hi"}]}
    _validate_model_request("claude-opus-5", body, _Request())


def test_validation_rejects_unknown_claude_with_provider_catalog(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(proxy_app, "_VALID_MODEL_ALIASES", {"chutes-deepseek"})
    monkeypatch.setattr(proxy_app, "_claude_oauth_available", lambda: True)

    body = {"messages": [{"role": "user", "content": "hi"}]}
    with pytest.raises(ProxyError) as exc:
        _validate_model_request("claude-opus-4-9", body, _Request())

    assert exc.value.error_type == "model_not_available"
    assert exc.value.details["provider"] == "claude"
    assert any(row["id"] == "claude-opus-5" for row in exc.value.details["available_models"])
    assert any(row["reasoning_efforts"] for row in exc.value.details["available_models"])


def test_validation_allows_missing_reasoning_for_gpt_55(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(proxy_app, "_VALID_MODEL_ALIASES", {"chutes-deepseek"})
    monkeypatch.setattr(proxy_app, "_codex_oauth_available", lambda: True)

    body = {"messages": [{"role": "user", "content": "hi"}]}
    _validate_model_request("gpt-5.5", body, _Request())
    assert "reasoning_effort" not in body


def test_validation_allows_codex_reasoning_dict(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(proxy_app, "_VALID_MODEL_ALIASES", {"chutes-deepseek"})
    monkeypatch.setattr(proxy_app, "_codex_oauth_available", lambda: True)

    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "reasoning": {"effort": "low"},
    }
    _validate_model_request("gpt-5.5", body, _Request())


def test_validation_normalizes_codex_reasoning_effort(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(proxy_app, "_VALID_MODEL_ALIASES", {"chutes-deepseek"})
    monkeypatch.setattr(proxy_app, "_codex_oauth_available", lambda: True)

    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "reasoning_effort": "HIGH",
    }
    _validate_model_request("gpt-5.5", body, _Request())

    assert body["reasoning_effort"] == "high"


def test_validation_allows_dynamic_codex_xhigh_reasoning(monkeypatch: pytest.MonkeyPatch, tmp_path):
    path = tmp_path / "models_cache.json"
    path.write_text(
        """
        {
          "models": [
            {
              "slug": "gpt-5.5",
              "display_name": "GPT-5.5",
              "supported_reasoning_levels": [{"effort": "high"}, {"effort": "xhigh"}]
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("SCILLM_CODEX_MODELS_CACHE", str(path))
    monkeypatch.setattr(proxy_app, "_VALID_MODEL_ALIASES", {"chutes-deepseek"})
    monkeypatch.setattr(proxy_app, "_codex_oauth_available", lambda: True)

    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "reasoning_effort": "XHIGH",
    }
    _validate_model_request("gpt-5.5", body, _Request())

    assert body["reasoning_effort"] == "xhigh"


def test_validation_rejects_invalid_codex_reasoning_effort(monkeypatch: pytest.MonkeyPatch, tmp_path):
    path = tmp_path / "models_cache.json"
    path.write_text(
        """
        {
          "models": [
            {
              "slug": "gpt-5.5",
              "display_name": "GPT-5.5",
              "supported_reasoning_levels": [{"effort": "high"}, {"effort": "xhigh"}]
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("SCILLM_CODEX_MODELS_CACHE", str(path))
    monkeypatch.setattr(proxy_app, "_VALID_MODEL_ALIASES", {"chutes-deepseek"})
    monkeypatch.setattr(proxy_app, "_codex_oauth_available", lambda: True)

    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "reasoning": "mustard",
    }
    with pytest.raises(ProxyError, match="Unsupported reasoning effort") as exc:
        _validate_model_request("gpt-5.5", body, _Request())

    assert exc.value.error_type == "unsupported_reasoning_effort"
    assert exc.value.details["provider"] == "codex"
    assert exc.value.details["available_reasoning_efforts"] == ["high", "xhigh"]
    assert exc.value.details["accepted_reasoning_values"] == ["none", "high", "xhigh"]


def test_validation_rejects_stale_codex_reasoning_effort_with_dynamic_help(monkeypatch: pytest.MonkeyPatch, tmp_path):
    path = tmp_path / "models_cache.json"
    path.write_text(
        """
        {
          "models": [
            {
              "slug": "gpt-5.5",
              "display_name": "GPT-5.5",
              "supported_reasoning_levels": [{"effort": "medium"}, {"effort": "high"}]
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("SCILLM_CODEX_MODELS_CACHE", str(path))
    monkeypatch.setattr(proxy_app, "_VALID_MODEL_ALIASES", {"chutes-deepseek"})
    monkeypatch.setattr(proxy_app, "_codex_oauth_available", lambda: True)

    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "reasoning_effort": "xhigh",
    }
    with pytest.raises(ProxyError) as exc:
        _validate_model_request("gpt-5.5", body, _Request())

    assert exc.value.error_type == "unsupported_reasoning_effort"
    assert exc.value.details["requested_reasoning_effort"] == "xhigh"
    assert exc.value.details["available_reasoning_efforts"] == ["medium", "high"]
    assert "refresh_provider_models=true" in exc.value.details["refresh_hint"]


def test_validation_rejects_unknown_codex_with_reasoning_catalog(monkeypatch: pytest.MonkeyPatch, tmp_path):
    path = tmp_path / "models_cache.json"
    path.write_text(
        """
        {
          "models": [
            {
              "slug": "gpt-5.6-sol",
              "display_name": "GPT-5.6 Sol",
              "supported_reasoning_levels": [{"effort": "high"}, {"effort": "xhigh"}]
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("SCILLM_CODEX_MODELS_CACHE", str(path))
    monkeypatch.setattr(proxy_app, "_VALID_MODEL_ALIASES", {"chutes-deepseek"})
    monkeypatch.setattr(proxy_app, "_codex_oauth_available", lambda: True)

    body = {"messages": [{"role": "user", "content": "hi"}]}
    with pytest.raises(ProxyError) as exc:
        _validate_model_request("gpt-9.9", body, _Request())

    assert exc.value.error_type == "model_not_available"
    assert exc.value.details["provider"] == "codex"
    assert exc.value.details["available_models"][0]["id"] == "gpt-5.6-sol"
    assert exc.value.details["available_models"][0]["reasoning_efforts"] == ["high", "xhigh"]


def test_validation_rejects_gpt_55_when_codex_unavailable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(proxy_app, "_VALID_MODEL_ALIASES", {"chutes-deepseek"})
    monkeypatch.setattr(proxy_app, "_codex_oauth_available", lambda: False)

    body = {"messages": [{"role": "user", "content": "hi"}]}
    with pytest.raises(ProxyError):
        _validate_model_request("gpt-5.5", body, _Request())


def test_validation_rejects_unsupported_chatgpt_codex_models(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(proxy_app, "_VALID_MODEL_ALIASES", {"chutes-deepseek"})
    monkeypatch.setattr(proxy_app, "_codex_oauth_available", lambda: True)

    body = {"messages": [{"role": "user", "content": "hi"}]}
    with pytest.raises(ProxyError, match="not supported for one-shot Codex OAuth"):
        _validate_model_request("gpt-5.3-codex", body, _Request())


def test_config_registers_gpt_55_model_group():
    config = load_config("local/proxy_server_config.yaml")
    group = config.model_groups["gpt-5.5"]

    assert group.deployments[0].model == "gpt-5.5"
    assert group.deployments[0].custom_llm_provider == "codex-oauth"


def test_codex_input_preserves_image_url_parts():
    instructions, input_items = _openai_messages_to_codex_input([
        {"role": "system", "content": "Answer briefly."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What color is this image?"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,iVBORw0KGgo=",
                    },
                },
            ],
        },
    ])

    assert instructions == "Answer briefly."
    assert input_items == [
        {
            "role": "user",
            "type": "message",
            "content": [
                {"type": "input_text", "text": "What color is this image?"},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,iVBORw0KGgo=",
                },
            ],
        }
    ]


def test_codex_parser_rejects_empty_zero_usage_completed_response():
    events = [
        {
            "type": "response.completed",
            "response": {
                "model": "gpt-5.5",
                "output": [],
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                },
            },
        }
    ]

    with pytest.raises(ProxyError) as exc:
        _parse_codex_response(events, "gpt-5.5")

    assert exc.value.status_code == 502
    assert exc.value.error_type == "provider_empty_zero_usage_response"
    assert exc.value.details["prompt_tokens"] == 0
    assert exc.value.details["completion_tokens"] == 0


def test_codex_parser_allows_visible_completed_response():
    events = [
        {"type": "response.output_text.delta", "delta": "answer"},
        {
            "type": "response.completed",
            "response": {
                "model": "gpt-5.5",
                "output": [],
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 3,
                },
            },
        },
    ]

    response = _parse_codex_response(events, "gpt-5.5")

    assert response.choices[0].message.content == "answer"
    assert response.usage is not None
    assert response.usage.prompt_tokens == 12
    assert response.usage.completion_tokens == 3
