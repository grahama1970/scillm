import os
import time
import multiprocessing as mp
from contextlib import contextmanager

import uvicorn
import pytest


def _run_app():
    from tests.local_testing.fixtures.chutes_mock_server import app
    uvicorn.run(app, host="127.0.0.1", port=18111, log_level="warning")


@contextmanager
def mock_server():
    p = mp.Process(target=_run_app, daemon=True)
    p.start()
    try:
        time.sleep(0.6)
        yield
    finally:
        p.terminate()


@pytest.mark.asyncio
async def test_model_list_first_success_httpx_x_api_key():
    os.environ.setdefault("SCILLM_FORCE_HTTPX_STREAM", "1")
    base = "http://127.0.0.1:18111/v1"
    # Build model_list: first entry with bad key → 401; second entry with good key → 200
    ml = [
        {
            "model_name": "bad",
            "litellm_params": {
                "custom_llm_provider": "openai_like",
                "api_base": base,
                "api_key": None,
                "extra_headers": {"x-api-key": "bad"},
                "model": "demo-model",
            },
        },
        {
            "model_name": "good",
            "litellm_params": {
                "custom_llm_provider": "openai_like",
                "api_base": base,
                "api_key": None,
                "extra_headers": {"x-api-key": "good"},
                "model": "demo-model",
            },
        },
    ]

    with mock_server():
        import litellm

        resp = litellm.completion(
            model="bad",
            model_list=ml,
            messages=[{"role": "user", "content": "Say OK"}],
            max_tokens=8,
            temperature=0,
        )
        content = resp.choices[0].message.get("content", "")
        assert content, "content should not be empty"

