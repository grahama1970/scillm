"""Lean4 bridge client recipe.

Minimal async client that calls the Lean4 bridge endpoint's `/bridge/complete`
with a deterministic, no-LLM batch and prints the shaped summary.

Env:
- `LEAN4_BRIDGE_BASE` (default: http://127.0.0.1:8787)
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict

import httpx
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

BASE = os.getenv("LEAN4_BRIDGE_BASE", "http://127.0.0.1:8787")


async def main() -> None:
    body: Dict[str, Any] = {
        "messages": [{"role": "system", "content": "demo"}],
        "lean4_requirements": [
            {"requirement_text": "0 + n = n", "context": {"section_id": "S1"}},
            {"requirement_text": "m + n = n + m", "context": {"section_id": "S2"}},
        ],
        "lean4_flags": ["--deterministic", "--no-llm"],
    }

    print(json.dumps({"example_request": body}, indent=2))
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(f"{BASE}/bridge/complete", json=body)
        r.raise_for_status()
        print(json.dumps({"example_response": r.json()}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
