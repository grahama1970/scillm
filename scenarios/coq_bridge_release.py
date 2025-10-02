#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

BASE = os.getenv("COQ_BRIDGE_BASE", "http://127.0.0.1:8897")


async def main() -> None:
    body = {
        "messages": [{"role": "system", "content": "Coq bridge smoke"}],
        "items": [
            {"goal": "forall n m:nat, n + m = m + n"},
            {"goal": "forall A B:Prop, A /\\ B -> B /\\ A"}
        ],
        "options": {"max_seconds": 30},
        "provider": {"name": "coq", "args": {}},
    }
    print(json.dumps({"example_request": body}, indent=2))
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(BASE.rstrip('/') + '/bridge/complete', json=body)
            if r.status_code == 200:
                print(json.dumps({"example_response": r.json()}, indent=2))
                return
            print(json.dumps({"error": r.text, "status": r.status_code}, indent=2))
            sys.exit(1)
    except Exception as exc:
        msg = str(exc)
        if "Connection" in msg or "refused" in msg:
            print(json.dumps({"skip": True, "reason": "coq bridge not reachable", "hint": "PYTHONPATH=src uvicorn coq.bridge.server:app --port 8897"}, indent=2))
            sys.exit(0)
        print(json.dumps({"error": msg}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

