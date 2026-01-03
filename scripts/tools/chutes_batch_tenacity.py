#!/usr/bin/env python3
from __future__ import annotations

import asyncio as aio
import json
import os
import time
from typing import Any, Dict, List

from scillm.batch import parallel_acompletions_env  # pulls CHUTES_* env
from scillm import Router  # OpenAI-like router

# 5 JSON prompts
REQUESTS: List[Dict[str, Any]] = [
    {
        "messages": [
            {"role": "system", "content": "Only respond in well formatted JSON"},
            {"role": "user",   "content": "Return only {\"ok\":true} as JSON."},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 16,
        "temperature": 0,
    },
    {
        "messages": [
            {"role": "system", "content": "Only respond in well formatted JSON"},
            {"role": "user",   "content": "What is the most common color of an apple? Respond with {fruit:<string>, color:<string>}."},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 64,
        "temperature": 0.2,
    },
    {
        "messages": [
            {"role": "system", "content": "Only respond in well formatted JSON"},
            {"role": "user",   "content": "Compute 7+5 and return {\"sum\": <number>} strictly as JSON."},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 16,
        "temperature": 0,
    },
    {
        "messages": [
            {"role": "system", "content": "Only respond in well formatted JSON"},
            {"role": "user",   "content": "Return {\"now_utc\": <ISO8601 string>, \"note\": \"ok\"} strictly as JSON."},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 32,
        "temperature": 0,
    },
    {
        "messages": [
            {"role": "system", "content": "Only respond in well formatted JSON"},
            {"role": "user",   "content": "Classify 'apple' as a fruit or company; return {\"category\":<string>} strictly as JSON."},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 16,
        "temperature": 0,
    },
]


def _extract_content(resp: Any) -> str:
    try:
        if isinstance(resp, dict):
            return (resp.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
        return (resp.choices[0].message.get("content") or "").strip()
    except Exception:
        return ""


async def phase_parallel_until_all():
    # Per‑attempt + batch controls from env
    per_attempt_timeout = float(os.environ.get("TENACITY_TIMEOUT", "20") or 20.0)
    batch_wall_s       = float(os.environ.get("TENACITY_WALL_S", "120") or 120.0)
    overall_wall_s     = float(os.environ.get("TENACITY_ALL_WALL_S", "900") or 900.0)
    conc               = int(os.environ.get("TENACITY_CONCURRENCY", "3") or 3)

    t0 = time.perf_counter()
    done = [False]*len(REQUESTS)
    results: List[Dict[str, Any]] = [{"ok": False, "error": "pending"} for _ in REQUESTS]
    rounds = 0

    while not all(done) and (time.perf_counter() - t0) < overall_wall_s:
        rounds += 1
        # Ask for all items each round; accept first success per slot
        res = await parallel_acompletions_env(
            REQUESTS,
            tenacious=True,
            wall_time_s=batch_wall_s,
            timeout=per_attempt_timeout,
            concurrency=conc,
        )
        for i, r in enumerate(res):
            if done[i]:
                continue
            content = _extract_content(r)
            if content:
                done[i] = True
                results[i] = {"ok": True, "content_head": content[:160]}
            else:
                results[i] = {"ok": False, "error": "empty"}
        if not all(done):
            await aio.sleep(1.0)  # avoid hammering when pool is down

    # Mark timeouts
    for i, ok in enumerate(done):
        if not ok and results[i].get("error") == "pending":
            results[i] = {"ok": False, "error": "timeout"}

    print(json.dumps({
        "phase": "parallel_acompletions_env",
        "rounds": rounds,
        "elapsed_s": round(time.perf_counter() - t0, 2),
        "results": results,
    }, ensure_ascii=False))


async def phase_router_with_backoff():
    base  = os.environ["CHUTES_API_BASE"].rstrip("/")
    key   = os.environ["CHUTES_API_KEY"]
    model = os.environ.get("CHUTES_MODEL_ID") or os.environ["CHUTES_TEXT_MODEL"]
    alt   = os.environ.get("CHUTES_TEXT_MODEL_ALT1", "").strip()
    model_list = [
        {"model_name": "chutes/text", "litellm_params": {
            "custom_llm_provider": "openai_like",
            "model": model, "api_base": base, "api_key": key}},
    ]
    if alt:
        model_list.append({"model_name": "chutes/text", "litellm_params": {
            "custom_llm_provider": "openai_like",
            "model": alt, "api_base": base, "api_key": key}})

    router = Router(model_list=model_list)

    per_attempt_timeout = float(os.environ.get("TENACITY_TIMEOUT", "20") or 20.0)
    overall_wall_s      = float(os.environ.get("TENACITY_ALL_WALL_S", "900") or 900.0)

    async def one_with_backoff(req: Dict[str, Any]) -> Dict[str, Any]:
        start = time.perf_counter()
        attempt = 0
        delay = 0.5
        while (time.perf_counter() - start) < overall_wall_s:
            attempt += 1
            try:
                resp = await router.acompletion(
                    model=model,
                    messages=req["messages"],
                    response_format=req.get("response_format"),
                    max_tokens=req.get("max_tokens"),
                    temperature=req.get("temperature"),
                    timeout=per_attempt_timeout,
                )
                content = _extract_content(resp)
                if content:
                    return {"ok": True, "attempts": attempt, "content_head": content[:160]}
            except Exception:
                pass  # treat as transient
            await aio.sleep(delay)
            delay = min(30.0, delay * 2)
        return {"ok": False, "error": "timeout", "attempts": attempt}

    t0 = time.perf_counter()
    results = await aio.gather(*[one_with_backoff(r) for r in REQUESTS])
    print(json.dumps({
        "phase": "router_acompletion_with_backoff",
        "elapsed_s": round(time.perf_counter() - t0, 2),
        "results": results,
    }, ensure_ascii=False))


async def main():
    await phase_parallel_until_all()
    await phase_router_with_backoff()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(aio.run(main()))
    except RuntimeError:
        loop = aio.get_event_loop()
        raise SystemExit(loop.run_until_complete(main()))

