#!/usr/bin/env python3
"""Live CodeWorld bridge scenario via CodeWorldProvider.

Calls the CodeWorld bridge `/bridge/complete` and prints example_request/example_response.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

from feature_recipes.codeworld_provider import CodeWorldProvider


async def main() -> None:
    base = os.getenv("CODEWORLD_BASE", "http://127.0.0.1:8887")
    token = os.getenv("CODEWORLD_TOKEN")
    provider = CodeWorldProvider(base=base, token=token)

    messages = [
        {"role": "system", "content": "CodeWorld bridge smoke"},
        {"role": "user", "content": "Compare two strategies and pick one."},
    ]

    metrics = [m.strip() for m in os.getenv("CODEWORLD_METRICS", "correctness,speed").split(",") if m.strip()]
    iterations = int(os.getenv("CODEWORLD_ITERATIONS", "1"))
    languages = [l.strip() for l in os.getenv("CODEWORLD_ALLOWED_LANGUAGES", "python").split(",") if l.strip()]
    timeout = float(os.getenv("CODEWORLD_TIMEOUT_SECONDS", "60"))

    print(json.dumps({
        "example_request": {
            "messages": messages,
            "codeworld_metrics": metrics,
            "codeworld_iterations": iterations,
            "codeworld_allowed_languages": languages,
            "request_timeout": timeout,
        }
    }, indent=2))

    try:
        resp = await provider.acomplete(
            messages=messages,
            metrics=metrics,
            iterations=iterations,
            allowed_languages=languages,
            request_timeout=timeout,
        )
        print(json.dumps({"example_response": resp}, indent=2))
    except Exception as exc:
        msg = str(exc)
        if "Connection" in msg or "refused" in msg or "Failed to establish" in msg:
            print(json.dumps({"skip": True, "reason": "codeworld bridge not reachable", "hint": "Start: PYTHONPATH=src uvicorn codeworld.bridge.server:app --port 8887"}, indent=2))
            sys.exit(0)
        print(json.dumps({"error": msg}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

