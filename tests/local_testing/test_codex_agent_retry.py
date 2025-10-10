import os
from types import SimpleNamespace

import pytest


class _DummyResp:
    def __init__(self, status_code: int, json_body: dict):
        self.status_code = status_code
        self._json = json_body
        self.text = str(json_body)

    def json(self):
        return self._json


def test_codex_agent_retries_monkeypatched(monkeypatch):
    os.environ.setdefault("CODEX_AGENT_MAX_RETRIES", "2")
    os.environ.setdefault("CODEX_AGENT_RETRY_BASE_MS", "1")

    from litellm.llms.codex_agent import CodexAgentLLM
    from litellm.utils import ModelResponse

    calls = {"n": 0}

    def fake_post(self, url, json=None, headers=None, timeout=None):  # httpx.Client.post signature-ish
        calls["n"] += 1
        if calls["n"] == 1:
            return _DummyResp(500, {"error": "transient"})
        return _DummyResp(200, {"choices": [{"message": {"content": "ok"}}]})

    import httpx

    monkeypatch.setattr(httpx.Client, "post", fake_post, raising=True)

    prov = CodexAgentLLM()
    mr = ModelResponse()
    out = prov.completion(
        model="codex-agent/gpt-5",
        messages=[{"role": "user", "content": "hi"}],
        api_base="http://dummy",
        custom_prompt_dict={},
        model_response=mr,
        print_verbose=lambda *a, **k: None,
        encoding=None,
        api_key=None,
        logging_obj=None,
        optional_params={},
        headers={},
        timeout=2.0,
        client=None,
    )
    # Should have retried once then succeeded
    assert calls["n"] == 2
    content = getattr(out.choices[0].message, "content", None) or (
        out.choices[0].message.get("content") if isinstance(out.choices[0].message, dict) else None
    )
    assert content == "ok"

