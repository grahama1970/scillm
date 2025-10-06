import asyncio
import json
import pytest
import litellm


@pytest.mark.asyncio
async def test_router_parallel_openai_async_export_available():
    Router = getattr(litellm, "Router", None)
    assert Router is not None, "Router not exported"
    r = Router(model_list=[])
    assert hasattr(r, "parallel_acompletions"), "parallel_acompletions missing"

    async def _ovr(*, model: str, messages, **kwargs):  # type: ignore
        return {"choices":[{"message":{"content":"{\"ok\":true}"}}]}
    r._acompletion_override = _ovr  # type: ignore[attr-defined]
    msg = [{"role":"user","content":"Return only {\\\"ok\\\":true} as JSON."}]
    out = await r.parallel_acompletions([{
        "model":"openai/zai-org/GLM-4.5-Air",
        "messages": msg,
        "custom_llm_provider":"openai",
        "response_format":{"type":"json_object"}
    }])
    assert isinstance(out, list) and isinstance(out[0], dict)
    assert json.loads(out[0]["choices"][0]["message"]["content"]) == {"ok": True}
