import os
import time

from litellm.llms.codex_agent import CodexAgentLLM
from litellm.utils import ModelResponse


class _FailThenSuccess:
    def __init__(self):
        self.calls = 0

    def post(self, url, json=None, headers=None, timeout=None):  # noqa: D401 - fake client
        class R:
            def __init__(self, status_code, body):
                self.status_code = status_code
                self._body = body
                self.text = str(body)

            def json(self):
                return self._body

        self.calls += 1
        if self.calls < 3:
            return R(500, {"error": "boom"})
        return R(200, {"choices": [{"message": {"content": "ok"}}]})


def test_codex_agent_retry_deterministic(monkeypatch):
    os.environ["LITELLM_ENABLE_CODEX_AGENT"] = "1"
    os.environ["CODEX_AGENT_MAX_RETRIES"] = "3"
    os.environ["CODEX_AGENT_RETRY_BASE_MS"] = "50"
    os.environ["CODEX_AGENT_MAX_BACKOFF_MS"] = "200"
    os.environ["SCILLM_DETERMINISTIC_SEED"] = "11"
    os.environ["CODEX_AGENT_LOG_RETRIES"] = "0"

    fake_client = _FailThenSuccess()

    # Monkeypatch httpx.Client used in provider path
    import httpx

    monkeypatch.setattr(httpx.Client, "post", fake_client.post, raising=True)

    prov = CodexAgentLLM()
    mr = ModelResponse()
    t0 = time.time()
    out = prov.completion(
        model="codex-agent/mini",
        messages=[{"role": "user", "content": "ping"}],
        api_base="http://127.0.0.1:9999",
        custom_prompt_dict={},
        model_response=mr,
        print_verbose=lambda *a, **k: None,
        encoding=None,
        api_key=None,
        logging_obj=None,
        optional_params={},
        client=None,
    )
    elapsed = time.time() - t0
    assert fake_client.calls == 3
    content = getattr(out.choices[0].message, "content", None) or (
        out.choices[0].message.get("content") if isinstance(out.choices[0].message, dict) else None
    )
    assert content == "ok"
    assert elapsed < 2.0

