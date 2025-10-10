import asyncio
import json
from typing import Any

import pytest

from litellm.router import Router
from litellm.utils import ModelResponse


async def _fake_resp(content: str) -> ModelResponse:
    mr = ModelResponse()
    mr.choices[0].message.content = content  # type: ignore[attr-defined]
    return mr


def _mk_router(deterministic: bool = False) -> Router:
    r = Router(model_list=[], deterministic=deterministic)
    async def _ovr(*, model: str, messages, stream=False, **kwargs):  # type: ignore[no-redef]
        # echo back content; honor response_format
        rf = kwargs.get("response_format") or {}
        if rf and rf.get("type") == "json_schema":
            # invalid JSON to trigger fallback
            return await _fake_resp("")
        return await _fake_resp(json.dumps({"ok": True}))
    r._acompletion_override = _ovr  # type: ignore[attr-defined]
    return r


@pytest.mark.asyncio
async def test_schema_first_fallback_and_json_valid():
    r = _mk_router()
    out = await r.acompletion(
        model="x",
        messages=[{"role": "user", "content": "hi"}],
        response_mode="schema_first",
        json_schema={"type": "object"},
    )
    content = out.choices[0].message.content  # type: ignore[attr-defined]
    json.loads(content)
    # Content should be valid JSON after fallback
    assert isinstance(content, str) and content.strip()


@pytest.mark.asyncio
async def test_deterministic_forces_temperature_and_serializes():
    r = _mk_router(deterministic=True)
    reqs = [
        {"model": "x", "messages": [{"role": "user", "content": f"q{i}"}]} for i in range(3)
    ]
    res = await r.parallel_acompletions(reqs, concurrency=3)
    assert len(res) == 3
    assert hasattr(res[0], "meta") and res[0].meta.get("deterministic") is True


@pytest.mark.asyncio
async def test_max_concurrency_cap():
    r = _mk_router()
    reqs = [
        {"model": "x", "messages": [{"role": "user", "content": f"q{i}"}]} for i in range(5)
    ]
    res = await r.parallel_acompletions(reqs, max_concurrency=2)
    assert len(res) == 5


@pytest.mark.asyncio
async def test_budget_requests_soft_and_hard():
    r = _mk_router()
    r.set_budget(max_requests=2, hard=False)
    reqs = [
        {"model": "x", "messages": [{"role": "user", "content": f"q{i}"}]} for i in range(3)
    ]
    res = await r.parallel_acompletions(reqs)
    assert len(res) == 2
    # hard mode raises
    r.set_budget(max_requests=1, hard=True)
    with pytest.raises(RuntimeError):
        await r.parallel_acompletions(reqs)
