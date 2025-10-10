import asyncio
import os
import json
import pytest

import litellm


@pytest.mark.asyncio
async def test_async_acompletion_explicit_key_baseurl_ok():
    # Ensure no env keys leak
    os.environ.pop("OPENAI_API_KEY", None)
    os.environ.pop("CHUTES_API_KEY", None)
    # Use mock_response to avoid network; test that explicit args flow without error
    msg = [{"role": "user", "content": "Return only {\\\"ok\\\":true} as JSON."}]
    resp = await litellm.acompletion(
        model="openai/zai-org/DUMMY",
        messages=msg,
        response_format={"type":"json_object"},
        api_key="sk-test",
        base_url="https://example.invalid/v1",
        custom_llm_provider="openai",
        mock_response=json.dumps({"choices":[{"message":{"content":"{\\\"ok\\\":true}"}}]}),
    )
    content = resp.choices[0].message.content  # type: ignore[attr-defined]
    # Should be valid JSON as mocked; explicit args must not error
    data = json.loads(content)
    assert isinstance(data, dict)
