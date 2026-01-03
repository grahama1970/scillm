#!/usr/bin/env python3
from __future__ import annotations

import asyncio as aio
import json
import os

from scillm import parallel_acompletions


REQUESTS = [
    {
        "messages": [
            {"role": "system", "content": "Only respond in well formatted JSON"},
            {"role": "user", "content": "Return only {\"ok\":true} as JSON."},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 16,
        "temperature": 0,
    },
    {
        "messages": [
            {"role": "system", "content": "Only respond in well formatted JSON"},
            {"role": "user", "content": "What is the most common color of an apple? Respond with {fruit:<string>, color:<string>}."},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 64,
        "temperature": 0.2,
    },
    {
        "messages": [
            {"role": "system", "content": "Only respond in well formatted JSON"},
            {"role": "user", "content": "Compute 7+5 and return {\"sum\": <number>} strictly as JSON."},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 16,
        "temperature": 0,
    },
]


async def main() -> int:
    # Pull env (fail fast if missing)
    base = os.environ["CHUTES_API_BASE"].rstrip("/")
    key = os.environ["CHUTES_API_KEY"]
    model = os.environ.get("CHUTES_MODEL_ID") or os.environ["CHUTES_TEXT_MODEL"]

    # Use Router via model_list without importing Router (helper will construct it)
    model_list = [{
        "model_name": "chutes/text",
        "litellm_params": {
            "custom_llm_provider": "openai_like",
            "model": model,
            "api_base": base,
            "api_key": key,
        },
    }]

    # One call: tenacity + semaphore built‑in; returns normalized result objects
    res = await parallel_acompletions(
        REQUESTS,
        model_list=model_list,
        concurrency=3,
        wall_time_s=60,   # bump for flaky pools/overnight runs
        timeout=20,
    )

    # Print full request and raw response (or error) per item
    verb: list[dict] = []
    for r in res:
        req = r.get("request") or {}
        resp = r.get("response")
        # Normalize response to a dict when possible
        if resp is not None and not isinstance(resp, dict):
            if hasattr(resp, "model_dump"):
                resp = resp.model_dump()
            elif hasattr(resp, "to_dict"):
                resp = resp.to_dict()
            else:
                try:
                    resp = json.loads(json.dumps(resp, default=str))
                except Exception:
                    resp = {"_repr": str(resp)}
        verb.append({
            "index": r.get("index"),
            "request": req,
            "response": resp,
            "error": r.get("error"),
        })
    print(json.dumps(verb, ensure_ascii=False))
    try:
        from scillm import shutdown  # type: ignore
        shutdown()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(aio.run(main()))
    except RuntimeError:
        loop = aio.get_event_loop()
        raise SystemExit(loop.run_until_complete(main()))
