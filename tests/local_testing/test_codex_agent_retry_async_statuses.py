import os
import asyncio
from litellm.llms.codex_agent import CodexAgentLLM
from litellm.utils import ModelResponse


class _AsyncFailTwiceThenOk:
    def __init__(self):
        self.calls = 0

    async def post(self, url, json=None, headers=None, timeout=None):
        class R:
            def __init__(self, code, body):
                self.status_code = code
                self._b = body

            def json(self):
                return self._b

        self.calls += 1
        if self.calls < 3:
            return R(500, {"error": "temp"})
        return R(200, {"choices": [{"message": {"content": "ok"}}]})


async def _run():
    os.environ["CODEX_AGENT_ENABLE_METRICS"] = "1"
    os.environ["CODEX_AGENT_MAX_RETRIES"] = "3"
    os.environ["CODEX_AGENT_RETRY_BASE_MS"] = "5"
    os.environ["CODEX_AGENT_MAX_BACKOFF_MS"] = "40"
    agent = CodexAgentLLM()
    client = _AsyncFailTwiceThenOk()
    mr = ModelResponse()
    resp = await agent.acompletion(
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
        client=client,
    )
    stats = resp.additional_kwargs["codex_agent"]["retry_stats"]
    assert stats["failures"] == 2
    assert stats["final_status"] == 200
    assert stats["statuses"][:2] == [500, 500]
    assert stats["statuses"][-1] == 200
    assert len(stats["retry_sequence"]) == 2
    assert "ok" in resp.choices[0].message["content"]


def test_async_retry_statuses():
    asyncio.run(_run())

