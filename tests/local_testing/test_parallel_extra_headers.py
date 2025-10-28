import os
import time
import multiprocessing as mp
from contextlib import contextmanager

import uvicorn
import pytest


def _run_app(port: int):
    from tests.local_testing.fixtures.chutes_mock_server import app
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


@contextmanager
def mock_server():
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    p = mp.Process(target=_run_app, args=(port,), daemon=True)
    p.start()
    try:
        time.sleep(0.6)
        yield port
    finally:
        p.terminate()


@pytest.mark.asyncio
async def test_parallel_per_request_headers_are_honored():
    os.environ.setdefault("SCILLM_FORCE_HTTPX_STREAM", "1")
    # dynamic port
    with mock_server() as port:
        base = f"http://127.0.0.1:{port}/v1"

    # Router defaults deliberately missing auth; provide per-request extra_headers
    from litellm import Router

        router = Router(
            default_litellm_params={
                "custom_llm_provider": "openai_like",
                "api_base": base,
                "api_key": None,
            }
        )

    reqs = [
        {
            "model": "demo-model",
            "messages": [{"role": "user", "content": "Say OK"}],
            "kwargs": {"extra_headers": {"x-api-key": "good"}, "max_tokens": 8, "temperature": 0},
        }
        for _ in range(3)
    ]

        outs = await router.parallel_acompletions(requests=reqs, concurrency=3)
        texts = [
            (o.get("choices", [{}])[0].get("message", {}).get("content", "")) for o in outs
        ]
        assert all(texts), f"expected non-empty content, got {texts}"
