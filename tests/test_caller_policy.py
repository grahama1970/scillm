from __future__ import annotations

import httpx
import pytest

from chutes.middleware.caller_policy import CallerPolicyMiddleware, policy_target_model, request_capabilities


def test_example_profile_routes_evidence_case_to_gemini_only() -> None:
    from pathlib import Path

    import yaml

    profile_path = Path(__file__).parents[1] / "registry" / "caller_profiles.example.yaml"
    profile = yaml.safe_load(profile_path.read_text())["caller_profiles"]["create-evidence-case"]

    assert profile["allowed_models"] == ["gemini-flash"]
    assert "qra-*" in profile["deny_model_patterns"]
    assert profile["allow_streaming"] is False
from scillm.proxy.config import CallerProfile, Deployment, GeneralSettings, ModelGroup, ProxyConfig, load_config
from scillm.proxy.middleware import MiddlewareReject
from scillm.proxy import app as proxy_app


def _request(
    *,
    caller: str = "create-qras",
    model: str = "qra-deepseek-pool",
    messages: object | None = None,
    metadata: dict | None = None,
    **extra,
) -> dict:
    request = {
        "model": model,
        "messages": messages or [{"role": "user", "content": "hi"}],
        "_headers": {"x-caller-skill": caller},
    }
    if metadata is not None:
        request["scillm_metadata"] = metadata
    request.update(extra)
    return request


def _policy_config() -> ProxyConfig:
    return ProxyConfig(
        caller_profiles={
            "create-qras": CallerProfile(
                allowed_models=["qra-deepseek-pool"],
                deny_model_patterns=["gpt-*", "claude-*", "vlm-*"],
                require_scillm_metadata=["batch_id", "item_id"],
                allow_tools=False,
                allow_files=False,
                allow_images=False,
                allow_pdfs=False,
                allow_streaming=False,
                max_timeout_s=60,
            ),
            "pdf-extraction-review": CallerProfile(
                allowed_models=["vlm", "gemini-flash"],
                allow_tools=False,
                allow_images=True,
                allow_pdfs=True,
                allow_files=True,
            ),
        },
        fallbacks={"vlm": ["vlm-claude", "vlm-codex"]},
    )


@pytest.mark.asyncio
async def test_profile_requires_batch_metadata():
    middleware = CallerPolicyMiddleware(_policy_config())

    with pytest.raises(MiddlewareReject) as exc:
        await middleware.pre_call(_request(metadata={"batch_id": "b1"}))

    assert exc.value.status_code == 403
    assert "item_id" in exc.value.message


@pytest.mark.asyncio
async def test_profile_rejects_denied_model():
    middleware = CallerPolicyMiddleware(_policy_config())

    with pytest.raises(MiddlewareReject) as exc:
        await middleware.pre_call(
            _request(model="gpt-5.5", metadata={"batch_id": "b1", "item_id": "i1"})
        )

    assert "not in allowed_models" in exc.value.message


@pytest.mark.asyncio
async def test_profile_rejects_tools_and_files():
    middleware = CallerPolicyMiddleware(_policy_config())

    with pytest.raises(MiddlewareReject) as tools_exc:
        await middleware.pre_call(
            _request(
                metadata={"batch_id": "b1", "item_id": "i1"},
                tools=[{"type": "function", "function": {"name": "x", "parameters": {}}}],
            )
        )
    assert "tools are not allowed" in tools_exc.value.message

    file_only_config = ProxyConfig(
        caller_profiles={
            "no-files": CallerProfile(
                allowed_models=["vlm"],
                allow_tools=False,
                allow_files=False,
                allow_images=False,
                allow_pdfs=False,
            )
        }
    )
    file_middleware = CallerPolicyMiddleware(file_only_config)
    image_messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        ],
    }]
    with pytest.raises(MiddlewareReject) as image_exc:
        await file_middleware.pre_call(
            _request(caller="no-files", model="vlm", messages=image_messages)
        )
    assert "files are not allowed" in image_exc.value.message


@pytest.mark.asyncio
async def test_model_pool_policy_uses_pool_as_allowed_target():
    middleware = CallerPolicyMiddleware(_policy_config())
    request = _request(
        model="deepseek-ai/DeepSeek-V3-0324-TEE",
        metadata={"batch_id": "b1", "item_id": "i1"},
        _scillm_pool="qra-deepseek-pool",
    )

    allowed = await middleware.pre_call(request)

    assert allowed is request
    assert request["_policy_max_timeout_s"] == 60
    assert request["_dynamic_timeout_ms"] == 60_000


@pytest.mark.asyncio
async def test_deny_patterns_apply_to_fallback_chain():
    middleware = CallerPolicyMiddleware(
        ProxyConfig(
            caller_profiles={
                "reviewer": CallerProfile(
                    allowed_models=["vlm"],
                    deny_model_patterns=["vlm-codex"],
                )
            },
            fallbacks={"vlm": ["vlm-claude", "vlm-codex"]},
        )
    )

    with pytest.raises(MiddlewareReject) as exc:
        await middleware.pre_call(_request(caller="reviewer", model="vlm"))

    assert "vlm-codex" in exc.value.message


def test_multimodal_text_request_policy_target_is_vlm():
    request = _request(
        model="vlm",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
            ],
        }],
    )

    assert request_capabilities(request)["images"] is True
    assert policy_target_model(request) == "vlm"


def test_config_loads_caller_profiles(tmp_path):
    config_path = tmp_path / "proxy_config.yaml"
    config_path.write_text(
        """
model_list:
  - model_name: qra-deepseek-pool
    scillm_params:
      model: Qwen/Qwen3.6-27B-TEE
      api_base: https://llm.chutes.ai/v1
caller_profiles:
  create-qras:
    allowed_models: [qra-deepseek-pool]
    require_scillm_metadata: [batch_id, item_id]
    allow_tools: "false"
    allow_files: false
    max_timeout_s: 60
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert "create-qras" in config.caller_profiles
    assert config.caller_profiles["create-qras"].allow_tools is False
    assert config.caller_profiles["create-qras"].allow_files is False
    assert config.caller_profiles["create-qras"].require_scillm_metadata == ["batch_id", "item_id"]


@pytest.mark.asyncio
async def test_capabilities_endpoint_reports_profiles_and_adapters(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        proxy_app,
        "_config",
        ProxyConfig(
            general=GeneralSettings(master_key=""),
            model_groups={
                "Qwen/Qwen3.6-27B-TEE": ModelGroup(
                    name="Qwen/Qwen3.6-27B-TEE",
                    deployments=[Deployment(model="Qwen/Qwen3.6-27B-TEE", api_base="https://llm.chutes.ai/v1")],
                )
            },
            caller_profiles={"worker": CallerProfile(allowed_models=["Qwen/Qwen3.6-27B-TEE"], allow_tools=False)},
        ),
    )

    transport = httpx.ASGITransport(app=proxy_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/scillm/capabilities")

    assert response.status_code == 200
    data = response.json()
    assert data["version"] == 1
    assert "Qwen/Qwen3.6-27B-TEE" in data["model_groups"]
    assert "opencode_go" in data["adapters"]
    assert data["caller_profiles"]["worker"]["allow_tools"] is False


def test_chutes_config_inventory_reports_stale_provider_models(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        proxy_app,
        "_config",
        ProxyConfig(
            general=GeneralSettings(master_key=""),
            model_groups={
                "deepseek-ai/DeepSeek-V3.2-TEE": ModelGroup(
                    name="deepseek-ai/DeepSeek-V3.2-TEE",
                    deployments=[Deployment(model="deepseek-ai/DeepSeek-V3.2-TEE", api_base="https://llm.chutes.ai/v1")],
                ),
                "deepseek-ai/DeepSeek-V3.1-TEE": ModelGroup(
                    name="deepseek-ai/DeepSeek-V3.1-TEE",
                    deployments=[Deployment(model="deepseek-ai/DeepSeek-V3.1-TEE", api_base="https://llm.chutes.ai/v1")],
                ),
            },
            fallbacks={"deepseek-ai/DeepSeek-V3.2-TEE": ["deepseek-ai/DeepSeek-V3.1-TEE"]},
            aliases={},
        ),
    )

    inventory = proxy_app._chutes_config_inventory(["deepseek-ai/DeepSeek-V3.2-TEE"])

    assert inventory["status"] == "config_drift"
    assert inventory["configured_available_models"] == ["deepseek-ai/DeepSeek-V3.2-TEE"]
    assert inventory["configured_unavailable_models"] == ["deepseek-ai/DeepSeek-V3.1-TEE"]
    assert inventory["unavailable_fallback_targets"] == ["deepseek-ai/DeepSeek-V3.1-TEE"]
    assert inventory["alias_resolutions"] == {}
