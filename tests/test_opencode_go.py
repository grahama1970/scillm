from __future__ import annotations

from scillm.proxy.config import ProxyConfig
import pytest

from scillm.proxy.app import _is_direct_chutes_model, _is_empty_length_response, _validate_model_request
from scillm.proxy.errors import ProxyError
from scillm.proxy.providers.opencode_go import (
    ENDPOINT_CHAT_COMPLETIONS,
    ENDPOINT_MESSAGES,
    OPENCODE_GO_CHAT_TIMEOUT_SEC,
    OPENCODE_GO_MESSAGES_TIMEOUT_SEC,
    _build_messages_body,
    _collect_system_prompt,
    describe_opencode_go_model,
    opencode_go_endpoint_type,
    parse_opencode_models_output,
)
from scillm.proxy.router import Router
from chutes.middleware.chutes_router import _is_chutes_model
from chutes.middleware.concurrency_guard import _resolve_provider


class _Request:
    headers: dict[str, str] = {"x-caller-skill": "test"}


def test_parse_opencode_models_output_strips_refresh_and_ansi():
    output = "\x1b[92m\x1b[1mModels cache refreshed\x1b[0m\nopencode-go/kimi-k2.6\nopencode-go/minimax-m2.7\n"

    assert parse_opencode_models_output(output) == [
        "opencode-go/kimi-k2.6",
        "opencode-go/minimax-m2.7",
    ]


def test_endpoint_registry_covers_messages_and_chat_models():
    assert opencode_go_endpoint_type("kimi-k2.6") == ENDPOINT_CHAT_COMPLETIONS
    assert opencode_go_endpoint_type("deepseek-v4-pro") == ENDPOINT_MESSAGES
    assert opencode_go_endpoint_type("minimax-m2.7") == ENDPOINT_MESSAGES


def test_describe_opencode_go_model_marks_supported():
    model = describe_opencode_go_model("opencode-go/deepseek-v4-flash", key_configured=True)

    assert model["id"] == "opencode-go/deepseek-v4-flash"
    assert model["endpoint_type"] == ENDPOINT_MESSAGES
    assert model["supported"] is True
    assert model["key_configured"] is True


def test_router_autocreates_opencode_go_chat_group_before_chutes_slash_route():
    config = ProxyConfig(
        opencode_go_api_base="https://opencode.ai/zen/go/v1",
        opencode_go_api_key="test-key",
        chutes_api_base="https://llm.chutes.ai/v1",
        chutes_api_key="chutes-key",
    )
    router = Router(config)

    group = router._get_group("opencode-go/kimi-k2.6")

    assert group is not None
    assert group.deployments[0].model == "kimi-k2.6"
    assert group.deployments[0].api_key == "test-key"
    assert group.deployments[0].custom_llm_provider is None
    assert group.deployments[0].timeout == OPENCODE_GO_CHAT_TIMEOUT_SEC


def test_router_autocreates_opencode_go_messages_group():
    config = ProxyConfig(
        opencode_go_api_base="https://opencode.ai/zen/go/v1",
        opencode_go_api_key="test-key",
    )
    router = Router(config)

    group = router._get_group("opencode-go/minimax-m2.7")

    assert group is not None
    assert group.deployments[0].model == "minimax-m2.7"
    assert group.deployments[0].custom_llm_provider == "opencode-go-messages"
    assert group.deployments[0].timeout == OPENCODE_GO_MESSAGES_TIMEOUT_SEC


def test_chutes_router_does_not_claim_opencode_go_models():
    assert _is_chutes_model("opencode-go/deepseek-v4-pro") is False
    assert _is_chutes_model("opencode-go/minimax-m2.7") is False
    assert _is_chutes_model("deepseek-ai/DeepSeek-V3.2-TEE") is True


def test_concurrency_guard_routes_opencode_go_to_its_own_provider():
    assert _resolve_provider("opencode-go/deepseek-v4-flash") == "opencode-go"
    assert _resolve_provider("opencode-go/minimax-m2.7") == "opencode-go"
    assert _resolve_provider("deepseek-ai/DeepSeek-V3.2-TEE") == "chutes"


def test_app_validation_allows_direct_chutes_model_ids():
    assert _is_direct_chutes_model("deepseek-ai/DeepSeek-V3-0324-TEE") is True
    assert _is_direct_chutes_model("Qwen/Qwen3-30B-A3B") is True
    assert _is_direct_chutes_model("opencode-go/deepseek-v4-pro") is False


def test_app_validation_rejects_multimodal_opencode_go_requests():
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image."},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ],
            }
        ]
    }

    with pytest.raises(ProxyError) as exc:
        _validate_model_request("opencode-go/deepseek-v4-flash", body, _Request())

    assert "text-only" in exc.value.message
    assert "attachment=false" in exc.value.message


def test_app_validation_allows_text_opencode_go_requests():
    body = {"messages": [{"role": "user", "content": "Return JSON."}]}

    _validate_model_request("opencode-go/deepseek-v4-flash", body, _Request())


def test_empty_length_response_detects_visible_token_exhaustion():
    response = {
        "choices": [{"finish_reason": "length", "message": {"content": ""}}],
        "usage": {"completion_tokens": 4096},
    }

    assert _is_empty_length_response(response) is True


def test_empty_length_response_allows_non_empty_length_response():
    response = {
        "choices": [{"finish_reason": "length", "message": {"content": "partial answer"}}],
        "usage": {"completion_tokens": 4096},
    }

    assert _is_empty_length_response(response) is False


def test_opencode_messages_body_omits_default_max_tokens():
    body = _build_messages_body(
        "deepseek-v4-pro",
        [{"role": "user", "content": "Return JSON."}],
        {},
    )

    assert "max_tokens" not in body


def test_opencode_messages_body_preserves_explicit_max_tokens():
    body = _build_messages_body(
        "deepseek-v4-pro",
        [{"role": "user", "content": "Return JSON."}],
        {"max_tokens": 2048},
    )

    assert body["max_tokens"] == 2048


def test_opencode_messages_body_preserves_all_system_messages():
    body = _build_messages_body(
        "deepseek-v4-pro",
        [
            {"role": "system", "content": "Contract A."},
            {"role": "system", "content": [{"type": "text", "text": "Contract B."}]},
            {"role": "user", "content": "Return JSON."},
        ],
        {},
    )

    assert body["system"] == "Contract A.\n\nContract B."


def test_opencode_messages_body_translates_json_object_response_format():
    body = _build_messages_body(
        "deepseek-v4-pro",
        [
            {"role": "system", "content": "Return QRA pairs."},
            {"role": "user", "content": "CM0018 -> CM0019"},
        ],
        {"response_format": {"type": "json_object"}},
    )

    assert "Return QRA pairs." in body["system"]
    assert "exactly one valid JSON object" in body["system"]
    assert "markdown fences" in body["system"]
    assert "Output contract reminder" in body["messages"][-1]["content"][-1]["text"]
    assert "exactly one valid JSON object" in body["messages"][-1]["content"][-1]["text"]
    assert "response_format" not in body


def test_opencode_messages_body_translates_json_schema_response_format():
    body = _build_messages_body(
        "deepseek-v4-pro",
        [{"role": "user", "content": "CM0018 -> CM0019"}],
        {
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "qra_response",
                    "schema": {
                        "type": "object",
                        "required": ["pairs"],
                        "properties": {"pairs": {"type": "array"}},
                    },
                },
            }
        },
    )

    assert "qra_response" in body["system"]
    assert '"pairs"' in body["system"]
    assert "markdown fences" in body["system"]
    assert "qra_response" in body["messages"][-1]["content"][-1]["text"]


def test_collect_system_prompt_handles_list_content():
    system = _collect_system_prompt([
        {"role": "system", "content": [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]},
        {"role": "user", "content": "hi"},
    ])

    assert system == "A\nB"
