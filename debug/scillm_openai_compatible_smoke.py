#!/usr/bin/env python
"""
SciLLM ↔ OpenAI-compatible (Chutes) smoke:
 - Direct completion (JSON) on text model
 - Router.completion
 - Router.parallel_acompletions (2 requests)
 - completion(model_list) first-success

Reads env:
  CHUTES_API_BASE, CHUTES_API_KEY, CHUTES_TEXT_MODEL
Optional:
  CHUTES_AUTH_STYLE=bearer|x-api-key|raw  (default: bearer)

Exits:
  0 on success; non-zero otherwise. Prints minimal PASS/FAIL lines.
"""
import os, sys, json, time
import asyncio
from scillm import completion
from litellm import Router

base = os.environ.get("CHUTES_API_BASE")
key  = os.environ.get("CHUTES_API_KEY")
model_text = os.environ.get("CHUTES_TEXT_MODEL")
auth_style = os.environ.get("CHUTES_AUTH_STYLE", "bearer").strip().lower()

if not base or not key or not model_text:
    print("ENV_MISSING CHUTES_API_BASE/CHUTES_API_KEY/CHUTES_TEXT_MODEL", file=sys.stderr)
    sys.exit(12)

def hdrs():
    if auth_style == "x-api-key":
        return {"x-api-key": key, "Content-Type": "application/json", "Accept": "application/json"}
    if auth_style == "raw":
        return {"Authorization": key, "Content-Type": "application/json", "Accept": "application/json"}
    # bearer
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "application/json"}

def json_ok(resp: dict) -> bool:
    try:
        s = resp["choices"][0]["message"]["content"]
        return isinstance(s, str) and len(s) > 0
    except Exception:
        return False

failures = []

# 1) Direct completion JSON
try:
    r = completion(
        model=model_text,
        custom_llm_provider="openai_like",
        api_base=base,
        api_key=None,
        messages=[{"role":"user","content":"Return only {\\\"ok\\\":true} as JSON."}],
        response_format={"type":"json_object"},
        extra_headers=hdrs(),
        timeout=30,
    )
    ok = json_ok(r)
    print(f"DIRECT_JSON: {'PASS' if ok else 'FAIL'}")
    if not ok:
        failures.append('direct')
except Exception as e:
    print(f"DIRECT_JSON: FAIL {e.__class__.__name__}")
    failures.append('direct')

try:
    router = Router(
        model_list=[{
            "model_name": model_text,
            "litellm_params": {
                "model": model_text,
                "custom_llm_provider": "openai_like",
                "api_base": base,
                "api_key": None,
                "extra_headers": hdrs(),
            }
        }],
        routing_strategy="simple-shuffle",
    )
    r = router.completion(
        model=model_text,
        messages=[{"role":"user","content":"Return only {\\\"ok\\\":true} as JSON."}],
        response_format={"type":"json_object"},
        timeout=30,
    )
    ok = json_ok(r)
    print(f"ROUTER_JSON: {'PASS' if ok else 'FAIL'}")
    if not ok:
        failures.append('router')
except Exception as e:
    print(f"ROUTER_JSON: FAIL {e.__class__.__name__}")
    failures.append('router')

try:
    async def _run_parallel():
        items = [
            {"model": model_text, "messages": [{"role":"user","content":"Return {\\\"ok\\\":true} as JSON."}], "response_format": {"type":"json_object"}},
            {"model": model_text, "messages": [{"role":"user","content":"Return {\\\"ok\\\":true} as JSON."}], "response_format": {"type":"json_object"}},
        ]
        return await router.parallel_acompletions(requests=items)
    results = asyncio.run(_run_parallel())
    oks = [json_ok(r) for r in results]
    ok = all(oks)
    print(f"PARALLEL_JSON: {'PASS' if ok else 'FAIL'} {sum(oks)}/{len(oks)}")
    if not ok:
        failures.append('parallel')
except Exception as e:
    print(f"PARALLEL_JSON: FAIL {e.__class__.__name__}")
    failures.append('parallel')

# 4) completion(model_list) first-success
try:
    deploy = {
        "model_name": model_text,
        "litellm_params": {
            "model": model_text,
            "custom_llm_provider": "openai_like",
            "api_base": base,
            "api_key": None,
            "extra_headers": hdrs(),
        },
    }
    r = completion(
        model=model_text,
        model_list=[deploy, deploy],
        messages=[{"role":"user","content":"Return only {\\\"ok\\\":true} as JSON."}],
        response_format={"type":"json_object"},
        timeout=30,
    )
    ok = json_ok(r)
    print(f"MODEL_LIST_JSON: {'PASS' if ok else 'FAIL'}")
    if not ok:
        failures.append('model_list')
except Exception as e:
    print(f"MODEL_LIST_JSON: FAIL {e.__class__.__name__}")
    failures.append('model_list')

sys.exit(0 if not failures else 31)
