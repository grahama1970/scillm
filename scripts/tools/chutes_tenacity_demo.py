#!/usr/bin/env python3
"""Chutes tenacity demo — 5 JSON calls via SciLLM.

Proves backoff by:
- using scillm.parallel_acompletions (tenacious=True) for 5 requests
- also exercising Router with a model_list (primary + optional alt)

Env required:
  CHUTES_API_BASE, CHUTES_API_KEY, and CHUTES_MODEL_ID or CHUTES_TEXT_MODEL

Optional:
  CHUTES_TEXT_MODEL_ALT1   # alternate model id for Router
  TENACITY_WALL_S=900      # wall-clock for batch retries
  TENACITY_TIMEOUT=20      # per-attempt HTTP timeout
  TENACITY_CONCURRENCY=3   # parallel batch concurrency
  SCILLM_USE_CURL_BIN=1    # use real curl under the hood if httpx flakes

Run:
  PYTHONPATH=/home/graham/workspace/experiments/litellm \
  python scripts/tools/chutes_tenacity_demo.py
"""
from __future__ import annotations

import asyncio as aio
import json
import os
import time
from typing import Any, Dict, List

from scillm import parallel_acompletions, Router  # type: ignore



def _requests(model: str) -> List[Dict[str, Any]]:
    return [
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "Only respond in well formatted JSON"},
                {"role": "user", "content": "Return only {\"ok\":true} as JSON."},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 16,
            "temperature": 0,
        },
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "Only respond in well formatted JSON"},
                {"role": "user", "content": "What is the most common color of an apple? Respond with the schema {fruit:<string>, color:<string>}."},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 64,
            "temperature": 0.2,
        },
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "Only respond in well formatted JSON"},
                {"role": "user", "content": "Compute 7+5 and return {\"sum\": <number>} strictly as JSON."},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 16,
            "temperature": 0,
        },
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "Only respond in well formatted JSON"},
                {"role": "user", "content": "Return {\"now_utc\": <ISO8601 string>, \"note\": <string 'ok'>}."},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 32,
            "temperature": 0,
        },
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "Only respond in well formatted JSON"},
                {"role": "user", "content": "Classify 'apple' as a fruit or company; return {\"category\":<string>}."},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 16,
            "temperature": 0,
        },
    ]


async def _run_parallel(reqs: List[Dict[str, Any]], *, base: str, key: str) -> List[Dict[str, Any]]:
    conc = int(os.environ.get("TENACITY_CONCURRENCY", "3") or 3)
    wall = float(os.environ.get("TENACITY_WALL_S", "900") or 900.0)
    timeout = float(os.environ.get("TENACITY_TIMEOUT", "20") or 20.0)
    t0 = time.perf_counter()
    results = await parallel_acompletions(
        reqs,
        api_base=base,
        api_key=key,
        tenacious=True,
        wall_time_s=wall,
        timeout=timeout,
        concurrency=conc,
    )
    out: List[Dict[str, Any]] = []
    for i, r in enumerate(results):
        item: Dict[str, Any] = {"index": i}
        if isinstance(r, dict) and r.get("error"):
            item.update({"ok": False, "error": r.get("error"), "status": r.get("status")})
        else:
            try:
                content = (
                    r.get("choices", [{}])[0].get("message", {}).get("content", "")
                    if isinstance(r, dict)
                    else r.choices[0].message.get("content")
                )
            except Exception:
                content = ""
            item.update({"ok": bool(content), "content_head": (content or "")[:160]})
        out.append(item)
    dur = time.perf_counter() - t0
    print(json.dumps({"batch": "parallel_acompletions", "elapsed_s": round(dur, 2), "results": out}, ensure_ascii=False))
    return out


async def _run_router(reqs: List[Dict[str, Any]], *, model_list: List[Dict[str, Any]]):
    router = Router(model_list=model_list)
    async def _one(rq: Dict[str, Any]) -> Dict[str, Any]:
        t0 = time.perf_counter()
        try:
            resp = await router.acompletion(
                model=rq["model"],
                messages=rq["messages"],
                response_format=rq.get("response_format"),
                max_tokens=rq.get("max_tokens"),
                temperature=rq.get("temperature"),
            )
            try:
                content = (
                    resp.get("choices", [{}])[0].get("message", {}).get("content", "")
                    if isinstance(resp, dict)
                    else resp.choices[0].message.get("content")
                )
            except Exception:
                content = ""
            return {"ok": bool(content), "content_head": (content or "")[:160], "elapsed_s": round(time.perf_counter()-t0, 2)}
        except Exception as e:
            return {"ok": False, "error": str(e), "elapsed_s": round(time.perf_counter()-t0, 2)}

    t0 = time.perf_counter()
    results = await aio.gather(*[_one(r) for r in reqs])
    print(json.dumps({"batch": "router_acompletion", "elapsed_s": round(time.perf_counter()-t0, 2), "results": results}, ensure_ascii=False))


async def main() -> int:
    try:
        base = os.environ["CHUTES_API_BASE"].rstrip("/")
        key = os.environ["CHUTES_API_KEY"]
        model = os.environ.get("CHUTES_MODEL_ID") or os.environ["CHUTES_TEXT_MODEL"]
    except KeyError as e:
        missing = str(e).strip("'")
        raise SystemExit(f"Missing required env: {missing}")
    alt = os.environ.get("CHUTES_TEXT_MODEL_ALT1", "").strip()

    reqs = _requests(model)
    await _run_parallel(reqs, base=base, key=key)

    model_list = [{
        "model_name": "chutes/text",
        "litellm_params": {
            "custom_llm_provider": "openai_like",
            "model": model,
            "api_base": base,
            "api_key": key,
        },
    }]
    if alt:
        model_list.append({
            "model_name": "chutes/text",
            "litellm_params": {
                "custom_llm_provider": "openai_like",
                "model": alt,
                "api_base": base,
                "api_key": key,
            },
        })
    await _run_router(reqs, model_list=model_list)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(aio.run(main()))
    except RuntimeError:
        loop = aio.get_event_loop()
        raise SystemExit(loop.run_until_complete(main()))
