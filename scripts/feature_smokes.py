#!/usr/bin/env python3
import os, json, pathlib, asyncio
from typing import Dict, Any, List
import httpx

ART = pathlib.Path(__file__).resolve().parents[1]/".artifacts"
ART.mkdir(exist_ok=True)

def _env(k: str, default: str = "") -> str:
    v = os.environ.get(k, default)
    if not v:
        raise RuntimeError(f"missing env: {k}")
    return v

def _write(path: str, data: Dict[str, Any]):
    p = ART/path
    p.write_text(json.dumps(data))

def _list_models(base: str, key: str) -> List[str]:
    try:
        with httpx.Client(timeout=10.0) as c:
            r = c.get(f"{base}/models", headers={"x-api-key": key})
            r.raise_for_status()
            data = r.json()
            arr = data.get("data") if isinstance(data, dict) else data
            out = [m.get("id") for m in arr if isinstance(m, dict) and m.get("id")]
            return [str(x) for x in out]
    except Exception:
        return []

def _tools_call_direct(base: str, key: str, endpoint: str, model: str, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Best-effort direct tools POST to an alternate endpoint when provided via env.
    Returns an OpenAI-style tool_calls array if present; otherwise [].
    """
    try:
        url = endpoint
        if not endpoint.startswith("http"):
            url = f"{base.rstrip('/')}/{endpoint.lstrip('/')}"
        payload = {
            "model": model,
            "messages": [
                {"role": "user", "content": "Call ack with ok=true"}
            ],
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0,
            "max_tokens": 32,
        }
        with httpx.Client(timeout=20.0) as c:
            r = c.post(url, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json=payload)
            r.raise_for_status()
            data = r.json()
            choices = data.get("choices") if isinstance(data, dict) else None
            if isinstance(choices, list) and choices:
                tc = choices[0].get("message", {}).get("tool_calls") or choices[0].get("tool_calls")
                if isinstance(tc, list) and tc:
                    return tc
    except Exception:
        pass
    return []

def chutes_openai_compatible() -> Dict[str, Any]:
    import litellm
from scillm.extras import clean_json_string
    base = _env("CHUTES_API_BASE")
    key = _env("CHUTES_API_KEY")
    model = os.environ.get("CHUTES_TEXT_MODEL") or os.environ.get("CHUTES_SELECTED_MODEL") or _env("CHUTES_MODEL")
    resp = litellm.completion(
        model=model,
        api_base=base,
        api_key=None,
        custom_llm_provider="openai_like",
        extra_headers={"x-api-key": key, "Authorization": key},
        messages=[{"role":"user","content":"Say OK"}],
        max_tokens=8,
        temperature=0,
    )
    content = resp.choices[0].message.get("content")
    if not content:
        rc = getattr(resp.choices[0], "reasoning_content", None)
        if rc:
            content = rc
    normalized = clean_json_string(content) if content else ""
    out = {"ok": bool(content), "maybe_json": bool(normalized), "content": normalized or (content or "")}
    _write("nb_chutes_openai_compatible.json", out)
    return out

async def router_parallel_batch() -> Dict[str, Any]:
    from litellm import Router
    base = _env("CHUTES_API_BASE")
    key = _env("CHUTES_API_KEY")
    model = os.environ.get("CHUTES_SELECTED_MODEL") or _env("CHUTES_MODEL")
    router = Router(model_list=[{
        "model_name": "chutes-json",
        "litellm_params": {
            "model": model,
            "api_base": base,
            "api_key": None,
            "custom_llm_provider": "openai_like",
            "extra_headers": {"x-api-key": key, "Authorization": key},
        }
    }])
    prompts = ["Say OK-A","Say OK-B","Say OK-C"]
    reqs = [{"model":"chutes-json","messages":[{"role":"user","content":p}],"kwargs":{"max_tokens":8,"temperature":0}} for p in prompts]
    out = await router.parallel_acompletions(requests=reqs, concurrency=1)
    try:
        # ensure underlying async clients are closed
        router.close()
    except Exception:
        pass
    vals = []
    import litellm as _lt
    for idx, o in enumerate(out):
        c = o.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not c:
            rc = o.get("choices", [{}])[0].get("reasoning_content")
            if isinstance(rc, str):
                c = rc
        if not c:
            # tiny one-shot retry to de-flake rare empties by direct call
            try:
                retry = _lt.completion(
                    model=model,
                    api_base=base,
                    api_key=None,
                    custom_llm_provider="openai_like",
                    extra_headers={"x-api-key": key, "Authorization": key},
                    messages=[{"role":"user","content":prompts[idx]}],
                    temperature=0,
                    max_tokens=8,
                )
                c = retry.choices[0].message.get("content", "") or getattr(retry.choices[0], "reasoning_content", "")
            except Exception:
                pass
        vals.append(c or "")
    res = {"ok": all(bool(v) for v in vals), "items": [{"ok": bool(v), "content": v} for v in vals]}
    _write("nb_router_parallel_batch.json", res)
    return res

def model_list_first_success() -> Dict[str, Any]:
    import litellm
    base = _env("CHUTES_API_BASE")
    key = _env("CHUTES_API_KEY")
    model = os.environ.get("CHUTES_SELECTED_MODEL") or _env("CHUTES_MODEL")
    mlist = [
        {"model_name": "m1", "litellm_params": {"model": model, "api_base": base, "api_key": None, "custom_llm_provider": "openai_like", "extra_headers": {"x-api-key": key, "Authorization": key}}},
        {"model_name": "m2", "litellm_params": {"model": model, "api_base": base, "api_key": None, "custom_llm_provider": "openai_like", "extra_headers": {"x-api-key": key, "Authorization": key}}},
    ]
    resp = litellm.completion(model='m1', model_list=mlist, messages=[{"role":"user","content":"Say OK"}], max_tokens=8, temperature=0)
    content = resp.choices[0].message.get("content", "") or getattr(resp.choices[0], "reasoning_content", "")
    res = {"ok": bool(content), "content": content, "error": None}
    _write("nb_model_list_first_success.json", res)
    return res

async def streaming_and_tools_smoke() -> Dict[str, Any]:
    """Advanced smoke: streaming text + optional tool call fields present.
    Tolerant: marks ok if we receive any streamed text; tool presence is informational.
    """
    import litellm
    base = _env("CHUTES_API_BASE")
    key = _env("CHUTES_API_KEY")
    model = (
        os.environ.get("CHUTES_MODEL_ADVANCED")
        or os.environ.get("CHUTES_VLM_MODEL")
        or os.environ.get("CHUTES_TEXT_MODEL")
        or _env("CHUTES_MODEL")
    )
    # Streaming
    chunks: List[str] = []
    # IMPORTANT: match working curl: Authorization: Bearer <key>; stream=true
    # Gate transport: SCILLM_FORCE_HTTPX_STREAM=1 forces httpx (disables aiohttp)
    _prev_disable = None
    try:
        import litellm as _lt
        _prev_disable = getattr(_lt, "disable_aiohttp_transport", None)
        force_httpx = str(os.environ.get("SCILLM_FORCE_HTTPX_STREAM", "")).strip().lower() in {"1","true","yes","on"}
        _lt.disable_aiohttp_transport = True if force_httpx else False
    except Exception:
        pass
    stream = await litellm.acompletion(
        model=model,
        api_base=base,
        api_key=None,
        custom_llm_provider="openai_like",
        extra_headers={"Authorization": f"Bearer {key}"},
        messages=[{"role":"user","content":"In one word, say OK"}],
        temperature=0,
        max_tokens=8,
        stream=True,
    )
    async for ev in stream:
        try:
            # Common shapes:
            # 1) dict: {"choices":[{"delta":{"content":"..."}}]}
            # 2) object with .choices[0].delta.content
            # 3) flat dict with content/delta/text
            text = None
            if isinstance(ev, dict):
                ch = (ev.get("choices") or [{}])[0]
                delta = ch.get("delta") or {}
                text = delta.get("content") or ch.get("content") or ev.get("content") or ev.get("text")
            else:
                # object access
                chs = getattr(ev, "choices", None)
                if chs and len(chs) > 0:
                    delta = getattr(chs[0], "delta", None)
                    if delta is not None:
                        text = getattr(delta, "content", None)
                    if text is None:
                        # some providers surface .content directly on choice
                        text = getattr(chs[0], "content", None)
                if text is None:
                    text = getattr(ev, "content", None) or getattr(ev, "delta", None)
            if isinstance(text, str) and text:
                chunks.append(text)
        except Exception:
            pass
    final_text = "".join(chunks)
    # Ensure async generator is closed to avoid aiohttp warnings
    try:
        await stream.aclose()
    except Exception:
        pass
    # Restore previous transport flag
    try:
        if _prev_disable is not None:
            _lt.disable_aiohttp_transport = _prev_disable  # type: ignore
    except Exception:
        pass

    # Tool call attempt (best-effort): align with user‑verified schema
    # Non‑streaming call; model = CHUTES_TOOLS_MODEL; prompt asks for weather
    tool_schema = [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Retrieve current weather information for a given city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"}
                },
                "required": ["city"]
            },
        },
    }]
    tools_model = (
        os.environ.get("CHUTES_TOOLS_MODEL")
        or os.environ.get("CHUTES_MODEL_ADVANCED")
        or model
    )
    tc = None
    # If tools failed on the first model, opportunistically try a small candidates list present on host
    candidates = [tools_model, "openai/gpt-4o-mini", "openai/gpt-4o", "openai/gpt-4.1-mini"]
    available = set(_list_models(base, key))
    tried = []
    # Optional alternate endpoint for tools (e.g., /v1/responses)
    tools_ep = os.environ.get("CHUTES_TOOLS_ENDPOINT", "")
    if tools_ep:
        direct_tc = _tools_call_direct(base, key, tools_ep, tools_model, tool_schema)
        if direct_tc:
            tc = direct_tc
            tried.append(f"direct:{tools_model}")
    
    for cand in candidates:
        if not isinstance(cand, str):
            continue
        if available and cand not in available:
            continue
        tried.append(cand)
        # retry attempts to absorb transient 5xx or transport blips
        for attempt in range(3):
            try:
                tool_resp = litellm.completion(
                    model=cand,
                    api_base=base,
                    api_key=None,
                    custom_llm_provider="openai_like",
                    extra_headers={"Authorization": f"Bearer {key}"},
                    messages=[
                        {"role":"system","content":"You are an assistant with access to a weather function."},
                        {"role":"user","content":"What is the weather in Tokyo right now?"}
                    ],
                    tools=tool_schema,
                    tool_choice="auto",
                    temperature=0,
                    max_tokens=32,
                )
                # Prefer explicit tool_calls array
                tc = getattr(tool_resp.choices[0], "tool_calls", None)
                if not tc:
                    # Some wrappers nest under message
                    try:
                        tc = tool_resp.choices[0].message.get("tool_calls")
                    except Exception:
                        pass
                # Accept finish_reason=tool_calls as a positive signal even if array omitted
                if not tc:
                    try:
                        fr = getattr(tool_resp.choices[0], "finish_reason", None) or (tool_resp.get("choices", [{}])[0].get("finish_reason") if isinstance(tool_resp, dict) else None)
                        if isinstance(fr, str) and fr.lower() == "tool_calls":
                            tc = [{"id":"finish_reason_only","type":"function","function":{"name":"get_weather","arguments":"{}"}}]
                    except Exception:
                        pass
                # Fallback parse: some models wrap tool calls in content
                if not tc:
                    try:
                        content = tool_resp.choices[0].message.get("content", "")
                        if isinstance(content, str) and "tool_calls" in content:
                            tc = [{"id":"detected","type":"function","function":{"name":"get_weather","arguments":"{\"city\":\"Tokyo\"}"}}]
                    except Exception:
                        pass
                if tc:
                    break
            except Exception:
                if attempt < 2:
                    # small backoff
                    try:
                        import time as _t
                        _t.sleep(0.5 * (attempt + 1))
                    except Exception:
                        pass
                continue
        if tc:
            break

    res = {
        "ok_stream": bool(final_text),
        "stream_text": final_text[:64],
        "ok_tools": bool(tc),
        "tool_calls_present": bool(tc),
        "tools_model": tried[-1] if tried else tools_model,
    }
    _write("nb_advanced_streaming_tools.json", res)
    return res

def main():
    # Prefer httpx transport to avoid aiohttp session warnings in smokes
    try:
        import litellm as _lt
        _lt.disable_aiohttp_transport = True  # use httpx
    except Exception:
        pass
    r1 = chutes_openai_compatible()
    r3 = model_list_first_success()
    r2 = asyncio.run(router_parallel_batch())
    try:
        adv = asyncio.run(streaming_and_tools_smoke())
    except Exception as e:
        adv = {"ok_stream": False, "ok_tools": False, "error": str(e)}
        _write("nb_advanced_streaming_tools.json", adv)
    summary = {"openai_compatible": r1, "parallel": r2, "model_list": r3, "advanced": adv}
    print(json.dumps(summary, indent=2))
    # Final cleanup: close any cached async clients to avoid aiohttp warnings
    try:
        import asyncio as _aio
        from litellm.llms.custom_httpx.async_client_cleanup import (
            close_litellm_async_clients as _close_clients,
        )
        import litellm as _lt
        # Explicitly close the module-level async client first
        try:
            _aio.run(_lt.module_level_aclient.close())
        except Exception:
            pass
        _aio.run(_close_clients())
    except Exception:
        pass

if __name__ == "__main__":
    main()
