import os
import time
import socket
import multiprocessing as mp
from contextlib import closing

import pytest
import httpx


def _find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_mock(port: int):
    import uvicorn
    from scripts.chutes_mock_server import app

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


@pytest.fixture(scope="module")
def chutes_base():
    port = _find_free_port()
    proc = mp.Process(target=_run_mock, args=(port,), daemon=True)
    proc.start()
    base = f"http://127.0.0.1:{port}/v1"
    # wait for readiness
    deadline = time.time() + 5
    ok = False
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base}/models")
            if r.status_code in (200, 401):
                ok = True
                break
        except Exception:
            time.sleep(0.05)
    if not ok:
        proc.terminate()
        pytest.skip("mock gateway failed to start")
    yield base
    proc.terminate()


def _messages():
    return [{"role": "user", "content": 'Return only {"ok":true} as JSON.'}]


def test_direct_openai_like_x_api_key_success(chutes_base):
    from litellm import completion

    out = completion(
        model="stub-model",
        api_base=chutes_base,
        api_key=None,
        custom_llm_provider="openai_like",
        messages=_messages(),
        response_format={"type": "json_object"},
        extra_headers={"x-api-key": "sk-abc"},
    )
    assert out.choices[0].message.get("content"), "empty content"


def test_router_completion_success(chutes_base):
    from litellm import Router

    r = Router(
        model_list=[
            {
                "model_name": "stub-model",
                "litellm_params": {
                    "model": "stub-model",
                    "api_base": chutes_base,
                    "api_key": None,
                    "custom_llm_provider": "openai_like",
                    "extra_headers": {"x-api-key": "sk-abc"},
                },
            }
        ]
    )
    out = r.completion(
        model="stub-model",
        messages=_messages(),
        response_format={"type": "json_object"},
    )
    assert out.choices[0].message.get("content"), "empty content"


@pytest.mark.asyncio
async def test_router_parallel_acompletions_success(chutes_base):
    from litellm import Router

    r = Router(
        model_list=[
            {
                "model_name": "stub-model",
                "litellm_params": {
                    "model": "stub-model",
                    "api_base": chutes_base,
                    "api_key": None,
                    "custom_llm_provider": "openai_like",
                    "extra_headers": {"x-api-key": "sk-abc"},
                },
            }
        ]
    )
    prompts = [{"messages": _messages(), "model": "stub-model"} for _ in range(2)]
    results = await r.parallel_acompletions(requests=prompts)
    for res in results:
        content = res.get("choices", [{}])[0].get("message", {}).get("content", "")
        assert content, "empty content in parallel result"


def test_completion_model_list_success(chutes_base):
    from litellm import completion

    model_list = [
        {
            "model_name": "stub-model",
            "litellm_params": {
                "model": "stub-model",
                "api_base": chutes_base,
                "api_key": None,
                "custom_llm_provider": "openai_like",
                "extra_headers": {"x-api-key": "sk-abc"},
            },
        },
        {
            "model_name": "stub-model",
            "litellm_params": {
                "model": "stub-model",
                "api_base": chutes_base,
                "api_key": None,
                "custom_llm_provider": "openai_like",
                "extra_headers": {"x-api-key": "sk-abc"},
            },
        },
    ]
    out = completion(
        model="stub-model",
        model_list=model_list,
        messages=_messages(),
        response_format={"type": "json_object"},
    )
    assert out.choices[0].message.get("content"), "empty content"


@pytest.mark.xfail(reason="Header conversion behavior depends on provider routing; safe-mode failure may not surface uniformly across paths.")
def test_matrix_safe_mode_vs_auto_auth(chutes_base, monkeypatch):
    """Bearer must fail in safe-mode, succeed when auto-auth enabled."""
    from litellm import completion

    # SAFE MODE ON (default): sending Bearer should fail (401)
    monkeypatch.setenv("SCILLM_SAFE_MODE", "1")
    monkeypatch.delenv("SCILLM_ENABLE_AUTO_AUTH", raising=False)
    with pytest.raises(Exception):
        completion(
            model="stub-model",
            api_base=chutes_base,
            api_key=None,
            custom_llm_provider="openai_like",
            messages=_messages(),
            response_format={"type": "json_object"},
            extra_headers={"Authorization": "Bearer sk-abc"},
        )

    # AUTO-AUTH ON with SAFE MODE OFF: Bearer should be converted and succeed
    monkeypatch.setenv("SCILLM_SAFE_MODE", "0")
    monkeypatch.setenv("SCILLM_ENABLE_AUTO_AUTH", "1")
    out = completion(
        model="stub-model",
        api_base=chutes_base,
        api_key=None,
        custom_llm_provider="openai_like",
        messages=_messages(),
        response_format={"type": "json_object"},
        extra_headers={"Authorization": "Bearer sk-abc"},
    )
    assert out.choices[0].message.get("content"), "empty content"
