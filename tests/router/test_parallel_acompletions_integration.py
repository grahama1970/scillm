import os
import pytest

# Optional real-world integration test; skipped unless OpenAI credentials exist
requires_openai = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="requires OPENAI_API_KEY set to run integration test",
)


@pytest.mark.asyncio
@requires_openai
async def test_parallel_acompletions_real_world():
    # Enable the flag for this test run
    os.environ["LITELLM_ENABLE_PARALLEL_ACOMPLETIONS"] = "1"

    from litellm import Router
    from litellm.router_utils.parallel_acompletion import RouterParallelRequest

    router = Router(
        model_list=[
            {
                "model_name": "openai-gpt-3.5",
                "litellm_params": {
                    "model": "gpt-3.5-turbo",
                    "api_key": os.environ["OPENAI_API_KEY"],
                },
            }
        ]
    )

    requests = [
        RouterParallelRequest(
            model="openai-gpt-3.5",
            messages=[{"role": "user", "content": "Say 'hello'"}],
        ),
        RouterParallelRequest(
            model="openai-gpt-3.5",
            messages=[{"role": "user", "content": "Reply with the word 'ok'"}],
        ),
    ]

    results = await router.parallel_acompletions(
        requests, concurrency=2, preserve_order=True
    )

    assert len(results) == 2
    for r in results:
        assert r.exception is None
        assert r.response is not None
