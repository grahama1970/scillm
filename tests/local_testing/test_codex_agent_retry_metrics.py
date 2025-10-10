import os
from litellm.llms.codex_agent import CodexAgentLLM
from litellm.utils import ModelResponse


class _TwoFailuresThenOK:
    def __init__(self):
        self.n = 0

    def post(self, url, json=None, headers=None, timeout=None):  # noqa: D401 - stub
        class R:
            def __init__(self, code, body):
                self.status_code = code
                self._b = body
                self.text = str(body)

            def json(self):
                return self._b

        self.n += 1
        if self.n < 3:
            return R(502, {"error": "temporary"})
        return R(200, {"choices": [{"message": {"content": "done"}}]})


def test_retry_metrics_collected(monkeypatch):
    os.environ["CODEX_AGENT_ENABLE_METRICS"] = "1"
    os.environ["CODEX_AGENT_MAX_RETRIES"] = "3"
    os.environ["CODEX_AGENT_LOG_RETRIES"] = "0"
    os.environ["CODEX_AGENT_RETRY_BASE_MS"] = "1"
    os.environ["CODEX_AGENT_MAX_BACKOFF_MS"] = "4"

    agent = CodexAgentLLM()
    client = _TwoFailuresThenOK()
    mr = ModelResponse()
    resp = agent.completion(
        model="codex-agent/mini",
        messages=[{"role": "user", "content": "x"}],
        api_base="http://dummy",
        custom_prompt_dict={},
        model_response=mr,
        print_verbose=lambda *a, **k: None,
        encoding=None,
        api_key=None,
        logging_obj=None,
        optional_params={},
        client=client,
    )
    stats = resp.additional_kwargs["codex_agent"]["retry_stats"]
    assert stats["failures"] == 2
    assert stats["attempts"] == 2
    assert stats["total_sleep_ms"] >= 2  # very small but non-zero
    assert "done" in resp.choices[0].message["content"]

