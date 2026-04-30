import asyncio

import httpx
import pytest

from scillm.proxy.providers import streaming_timeout
from scillm.proxy.streaming import sse_liveness_wrapper


@pytest.mark.asyncio
async def test_sse_liveness_heartbeats_do_not_cancel_slow_provider():
    async def slow_stream():
        await asyncio.sleep(0.05)
        yield 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        yield "data: [DONE]\n\n"

    wrapped = sse_liveness_wrapper(
        slow_stream(),
        model="test-model",
        heartbeat_interval_s=0.01,
        overall_timeout_s=1.0,
    )

    chunks = []
    while True:
        chunk = await anext(wrapped)
        chunks.append(chunk)
        if '"content":"ok"' in chunk:
            break

    assert sum(chunk.startswith(": heartbeat") for chunk in chunks) >= 2
    assert '"content":"ok"' in chunks[-1]


@pytest.mark.asyncio
async def test_sse_liveness_enforces_overall_timeout():
    async def stalled_stream():
        await asyncio.sleep(60)
        yield "data: never\n\n"

    chunks = []
    async for chunk in sse_liveness_wrapper(
        stalled_stream(),
        model="test-model",
        heartbeat_interval_s=0.01,
        overall_timeout_s=0.03,
    ):
        chunks.append(chunk)

    assert any("timeout_error" in chunk for chunk in chunks)
    assert chunks[-1] == "data: [DONE]\n\n"


def test_streaming_timeout_uses_short_connect_and_unbounded_read():
    timeout = streaming_timeout(600)

    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 10.0
    assert timeout.read is None


@pytest.mark.asyncio
async def test_progress_done_event_precedes_done_sentinel():
    async def done_stream():
        yield "data: [DONE]\n\n"

    chunks = [
        chunk
        async for chunk in sse_liveness_wrapper(
            done_stream(),
            model="test-model",
            progress_events=True,
        )
    ]

    assert chunks[-2].startswith("event: done")
    assert chunks[-1] == "data: [DONE]\n\n"
