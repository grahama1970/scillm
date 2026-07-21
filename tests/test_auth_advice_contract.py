from scillm.proxy.errors import ProxyError


def test_authentication_error_advice_names_configured_key_not_dev_literal() -> None:
    payload = ProxyError(401, "Invalid API key", "authentication_error").to_dict()

    advice = payload["error"]["advice"]
    assert "configured proxy key" in advice
    assert "SCILLM_PROXY_KEY" in advice
    assert "SCILLM_MASTER_KEY" in advice
    assert "LITELLM_MASTER_KEY" in advice
    assert "sk-dev-proxy-123" not in advice
