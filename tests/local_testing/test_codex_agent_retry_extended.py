import os
from litellm.llms.codex_agent import CodexAgentLLM
from litellm.utils import ModelResponse


class _FailTwiceThenOk:
    def __init__(self):
        self.calls = 0

    def post(self, url, json=None, headers=None, timeout=None):  # noqa: D401 - stub
        class R:
            def __init__(self, status_code, body):
                self.status_code = status_code
                self._body = body
                self.text = str(body)

            def json(self):
                return self._body

        self.calls += 1
        if self.calls < 3:
            return R(502, {"error": "temp"})
        return R(200, {"choices": [{"message": {"content": "done"}}]})


def test_codex_agent_retry_extended(monkeypatch):
    os.environ["CODEX_AGENT_ENABLE_METRICS"] = "1"
    os.environ["CODEX_AGENT_MAX_RETRIES"] = "3"
    os.environ["CODEX_AGENT_RETRY_BASE_MS"] = "5"
    os.environ["CODEX_AGENT_MAX_BACKOFF_MS"] = "20"

    agent = CodexAgentLLM()

    import httpx

    fake = _FailTwiceThenOk()
    monkeypatch.setattr(httpx.Client, "post", fake.post, raising=True)

    mr = ModelResponse()
    resp = agent.completion(
        model="codex-agent/mini",
        messages=[{"role": "user", "content": "ping"}],
        api_base="http://dummy",
        custom_prompt_dict={},
        model_response=mr,
        print_verbose=lambda *a, **k: None,
        encoding=None,
        api_key=None,
        logging_obj=None,
        optional_params={},
        client=None,
    )

    stats = resp.additional_kwargs["codex_agent"]["retry_stats"]
    assert stats["failures"] == 2
    assert stats["attempts"] == 2
    assert stats["final_status"] == 200
    assert stats["first_failure_status"] in (502, None)  # server code recorded
    assert isinstance(stats["retry_sequence"], list) and len(stats["retry_sequence"]) == 2
    assert "done" in resp.choices[0].message["content"]

