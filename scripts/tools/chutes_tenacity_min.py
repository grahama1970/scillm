#!/usr/bin/env python3
from __future__ import annotations

import asyncio as aio
import json
import os
import time
from typing import Any, Dict, List

from scillm.batch import parallel_acompletions_env  # type: ignore


def requests_5() -> List[Dict[str, Any]]:
    return [
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
                {"role": "user", "content": "What is the most common color of an apple? Respond with the schema {fruit:<string>, color:<string>}."},
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
        {
            "messages": [
                {"role": "system", "content": "Only respond in well formatted JSON"},
                {"role": "user", "content": "Return {\"now_utc\": <ISO8601 string>, \"note\": <string 'ok'>}."},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 32,
            "temperature": 0,
        },
        {
            "messages": [
                {"role": "system", "content": "Only respond in well formatted JSON"},
                {"role": "user", "content": "Classify 'apple' as a fruit or company; return {\"category\":<string>}."},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 16,
            "temperature": 0,
        },
    ]


async def run_until_all_or_wall():
    per_batch_wall = float(os.environ.get("TENACITY_WALL_S", "120") or 120.0)
    all_wall = float(os.environ.get("TENACITY_ALL_WALL_S", "900") or 900.0)
    conc = int(os.environ.get("TENACITY_CONCURRENCY", "3") or 3)
    timeout = float(os.environ.get("TENACITY_TIMEOUT", "20") or 20.0)
    t0 = time.perf_counter()
    pending = requests_5()
    rounds = 0
    results_acc: List[Dict[str, Any]] = [{} for _ in pending]
    while pending and (time.perf_counter() - t0) < all_wall:
        rounds += 1
        res = await parallel_acompletions_env(
            pending,
            tenacious=True,
            wall_time_s=per_batch_wall,
            timeout=timeout,
            concurrency=conc,
        )
        # Mark successes and collect failures to retry
        new_pending: List[Dict[str, Any]] = []
        idx = 0
        for i, r in enumerate(res):
            if isinstance(r, dict) and r.get("error"):
                new_pending.append(pending[i])
                results_acc[i] = {"ok": False, "error": r.get("error"), "status": r.get("status")}
            else:
                try:
                    content = (
                        r.get("choices", [{}])[0].get("message", {}).get("content", "")
                        if isinstance(r, dict)
                        else r.choices[0].message.get("content")
                    )
                except Exception:
                    content = ""
                if content:
                    results_acc[i] = {"ok": True, "content_head": (content or "")[:160]}
                else:
                    new_pending.append(pending[i])
                    results_acc[i] = {"ok": False, "error": "empty"}
            idx += 1
        pending = new_pending
        if not pending:
            break
    print(json.dumps({
        "batch": "parallel_acompletions_env",
        "rounds": rounds,
        "elapsed_s": round(time.perf_counter() - t0, 2),
        "results": results_acc,
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        raise SystemExit(aio.run(run_until_all_or_wall()))
    except RuntimeError:
        loop = aio.get_event_loop()
        raise SystemExit(loop.run_until_complete(run_until_all_or_wall()))

