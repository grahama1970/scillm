from __future__ import annotations

from scillm.proxy.config import ProxyConfig
import pytest

from scillm.proxy.app import (
    _is_direct_chutes_model,
    _is_empty_length_response,
    _is_empty_zero_usage_response,
    _models_dev_extract,
    _models_dev_provider_key,
    _opencode_go_catalog_payload_for_validation,
    _stream_chunk_has_visible_output,
    _stream_chunk_reasoning_chars,
    _stream_terminal_diagnostics_from_chunk,
    _validate_model_request,
)
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
from chutes.middleware.concurrency_guard import _resolve_provider, _slot_max_age_s
from chutes.middleware.active_calls import _infer_provider, _request_timeout_s


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
    assert model["input"] == {"text": True, "image": False, "pdf": False}
    assert model["capabilities"]["image_input"] is False


def test_describe_opencode_go_kimi_reports_image_input():
    model = describe_opencode_go_model("opencode-go/kimi-k2.6", key_configured=True)

    assert model["endpoint_type"] == ENDPOINT_CHAT_COMPLETIONS
    assert model["input"] == {"text": True, "image": True, "pdf": False}
    assert model["capabilities"]["image_input"] is True


def test_opencode_go_catalog_rows_report_reasoning_not_advertised():
    catalog = _opencode_go_catalog_payload_for_validation()

    assert catalog["provider"] == "opencode-go"
    assert catalog["models"]
    assert catalog["models"][0]["reasoning_efforts"] == []
    assert catalog["models"][0]["reasoning_source"] == "not_advertised"


def test_models_dev_provider_key_maps_opencode_go():
    assert _models_dev_provider_key("opencode-go") == "opencode"
    assert _models_dev_provider_key("opencode_go") == "opencode"


def test_models_dev_extract_opencode_model_is_advisory():
    catalog = {
        "opencode": {
            "models": {
                "kimi-k2.6": {
                    "id": "kimi-k2.6",
                    "modalities": {"input": ["text", "image"], "output": ["text"]},
                }
            }
        }
    }

    result = _models_dev_extract(catalog, provider="opencode-go", model="opencode-go/kimi-k2.6")

    assert result["found"] is True
    assert result["advisory_only"] is True
    assert result["provider_key"] == "opencode"
    assert result["model_key"] == "kimi-k2.6"
    assert result["record"]["modalities"]["input"] == ["text", "image"]


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
    assert _resolve_provider("oc-glm") == "opencode-go"
    assert _resolve_provider("oc-kimi") == "opencode-go"
    assert _resolve_provider("oc-deepseek") == "opencode-go"
    assert _resolve_provider("oc-qwen") == "opencode-go"
    assert _resolve_provider("deepseek-ai/DeepSeek-V3.2-TEE") == "chutes"


def test_concurrency_guard_uses_longer_stale_window_for_opencode_go():
    assert _slot_max_age_s("opencode-go") == 600.0
    assert _slot_max_age_s("chutes") == 90.0


def test_active_calls_routes_opencode_go_aliases_to_provider():
    assert _infer_provider("oc-glm") == "opencode-go"
    assert _infer_provider("oc-kimi") == "opencode-go"


def test_active_calls_uses_opencode_go_timeout_floor_for_aliases():
    assert _request_timeout_s({"model": "oc-glm", "_dynamic_timeout_ms": 10_000}) == 600.0
    assert _request_timeout_s({"model": "oc-glm", "timeout": 30, "_dynamic_timeout_ms": 10_000}) == 30.0


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
    assert "opencode-go/kimi-k2.6" in exc.value.message


def test_app_validation_allows_openai_style_image_url_for_kimi():
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

    _validate_model_request("opencode-go/kimi-k2.6", body, _Request())


def test_app_validation_allows_text_opencode_go_requests():
    body = {"messages": [{"role": "user", "content": "Return JSON."}]}

    _validate_model_request("opencode-go/deepseek-v4-flash", body, _Request())


def test_app_validation_rejects_opencode_go_reasoning_with_help():
    body = {
        "messages": [{"role": "user", "content": "Return JSON."}],
        "reasoning_effort": "high",
    }

    with pytest.raises(ProxyError) as exc:
        _validate_model_request("opencode-go/kimi-k2.6", body, _Request())

    assert exc.value.error_type == "unsupported_reasoning_effort"
    assert exc.value.details["provider"] == "opencode-go"
    assert exc.value.details["available_reasoning_efforts"] == []
    assert exc.value.details["accepted_reasoning_values"] == ["none"]
    assert "Omit reasoning" in exc.value.details["project_agent_message"]


def test_app_validation_rejects_unknown_opencode_go_with_catalog():
    body = {"messages": [{"role": "user", "content": "Return JSON."}]}

    with pytest.raises(ProxyError) as exc:
        _validate_model_request("opencode-go/no-such-model", body, _Request())

    assert exc.value.error_type == "model_not_available"
    assert exc.value.details["provider"] == "opencode-go"
    assert any(row["id"] == "opencode-go/kimi-k2.6" for row in exc.value.details["available_models"])
    assert "refresh_provider_models=true" in exc.value.details["refresh_hint"]


@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
def test_app_validation_strips_output_token_caps(field):
    body = {
        "messages": [{"role": "user", "content": "Return JSON."}],
        field: 128,
    }

    _validate_model_request("oc-kimi", body, _Request())

    assert field not in body


def test_stream_terminal_diagnostics_extract_finish_reason_and_usage():
    chunk = (
        'data: {"choices":[{"delta":{},"finish_reason":"length"}],'
        '"usage":{"prompt_tokens":10,"completion_tokens":4096,"total_tokens":4106}}\n\n'
        "data: [DONE]\n\n"
    )

    diagnostics = _stream_terminal_diagnostics_from_chunk(chunk)

    assert diagnostics["finish_reason"] == "length"
    assert diagnostics["usage"]["completion_tokens"] == 4096
    assert diagnostics["saw_done"] is True


def test_stream_chunk_visible_output_accepts_reasoning_content():
    chunk = (
        'data: {"choices":[{"delta":{"reasoning_content":"visible kimi output"},'
        '"finish_reason":null}]}\n\n'
    )

    assert _stream_chunk_has_visible_output(chunk) is True


def test_stream_chunk_reasoning_chars_counts_reasoning_content():
    chunk = (
        'data: {"choices":[{"delta":{"reasoning_content":"visible "},'
        '"finish_reason":null}]}\n\n'
        'data: {"choices":[{"message":{"reasoning_content":"kimi output"},'
        '"finish_reason":"stop"}]}\n\n'
    )

    assert _stream_chunk_reasoning_chars(chunk) == len("visible kimi output")


def test_empty_length_response_detects_visible_token_exhaustion():
    response = {
        "choices": [{"finish_reason": "length", "message": {"content": ""}}],
        "usage": {"completion_tokens": 4096},
    }

    assert _is_empty_length_response(response) is True


def test_empty_length_response_allows_reasoning_content():
    response = {
        "choices": [
            {
                "finish_reason": "length",
                "message": {"content": "", "reasoning_content": "visible kimi output"},
            }
        ],
        "usage": {"completion_tokens": 4096},
    }

    assert _is_empty_length_response(response) is False


def test_empty_length_response_allows_non_empty_length_response():
    response = {
        "choices": [{"finish_reason": "length", "message": {"content": "partial answer"}}],
        "usage": {"completion_tokens": 4096},
    }

    assert _is_empty_length_response(response) is False


def test_empty_zero_usage_response_detects_router_false_green():
    response = {
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": ""}}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }

    assert _is_empty_zero_usage_response(response) is True


def test_empty_zero_usage_response_allows_tool_calls():
    response = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "lookup"}}],
                },
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }

    assert _is_empty_zero_usage_response(response) is False


def test_empty_zero_usage_response_allows_visible_content():
    response = {
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "answer"}}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }

    assert _is_empty_zero_usage_response(response) is False


def test_opencode_messages_body_omits_default_max_tokens():
    body = _build_messages_body(
        "deepseek-v4-pro",
        [{"role": "user", "content": "Return JSON."}],
        {},
    )

    assert "max_tokens" not in body


def test_opencode_messages_body_records_prompt_free_provider_diagnostics():
    diagnostics = {}
    body = _build_messages_body(
        "deepseek-v4-pro",
        [{"role": "user", "content": "Return JSON."}],
        {},
    )
    from scillm.proxy.providers.opencode_go import _record_provider_bound_diagnostics

    _record_provider_bound_diagnostics(
        diagnostics,
        model="deepseek-v4-pro",
        api_base="https://opencode.ai/zen/go/v1",
        body=body,
    )

    assert diagnostics["provider"] == "opencode-go"
    assert diagnostics["model"] == "deepseek-v4-pro"
    assert diagnostics["body_keys"] == ["messages", "model"]
    assert diagnostics["token_cap_fields_present"] == []


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
