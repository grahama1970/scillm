#!/usr/bin/env python3
from __future__ import annotations

import asyncio as aio
import json

from scillm import parallel_acompletions_simple_env  # uses CHUTES_* env


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
    {
        "messages": [
            {"role": "system", "content": "Only respond in well formatted JSON"},
            {"role": "user", "content": "Return {\"now_utc\": <ISO8601 string>, \"note\": \"ok\"} strictly as JSON."},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 32,
        "temperature": 0,
    },
    {
        "messages": [
            {"role": "system", "content": "Only respond in well formatted JSON"},
            {"role": "user", "content": "Classify 'apple' as a fruit or company; return {\"category\":<string>} strictly as JSON."},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 16,
        "temperature": 0,
    },
]


async def main():
    out = await parallel_acompletions_simple_env(
        REQUESTS,
        tenacious=True,
        wall_time_s=900,   # increase for overnight windows
        timeout=20,
        concurrency=3,
    )
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    try:
        raise SystemExit(aio.run(main()))
    except RuntimeError:
        loop = aio.get_event_loop()
        raise SystemExit(loop.run_until_complete(main()))

