import os
from litellm.llms.codex_agent import CodexAgentLLM
from litellm.utils import ModelResponse


class _Fail500TwiceThen200:
    def __init__(self):
        self.calls = 0

    def post(self, url, json=None, headers=None, timeout=None):  # noqa: D401 - stub
        class R:
            def __init__(self, code, body):
                self.status_code = code
                self._body = body
                self.text = str(body)

            def json(self):
                return self._body

        self.calls += 1
        if self.calls < 3:
            return R(500, {"error": "boom"})
        return R(200, {"choices": [{"message": {"content": "ok"}}]})


def test_retry_status_sequence(monkeypatch):
    os.environ["CODEX_AGENT_ENABLE_METRICS"] = "1"
    os.environ["CODEX_AGENT_MAX_RETRIES"] = "3"
    os.environ["CODEX_AGENT_RETRY_BASE_MS"] = "5"
    os.environ["CODEX_AGENT_MAX_BACKOFF_MS"] = "40"

    agent = CodexAgentLLM()

    import httpx

    fake = _Fail500TwiceThen200()
    monkeypatch.setattr(httpx.Client, "post", fake.post, raising=True)

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
        client=None,
    )
    stats = resp.additional_kwargs["codex_agent"]["retry_stats"]
    assert stats["failures"] == 2
    assert stats["final_status"] == 200
    # first two attempts failed with 500; final recorded status is 200
    assert stats["statuses"][0:2] == [500, 500]
    assert stats["statuses"][-1] == 200
    assert len(stats["retry_sequence"]) == 2

