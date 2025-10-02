#!/usr/bin/env python3
"""Live Lean4 bridge scenario via Lean4Provider (CodeWorld-parity).

Calls the Lean4 bridge `/bridge/complete` using the same provider pattern as
`feature_recipes/codeworld_provider.py`.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from textwrap import dedent

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

# Accept CERTAINLY_BRIDGE_BASE alias
BRIDGE_BASE = os.getenv("CERTAINLY_BRIDGE_BASE", os.getenv("LEAN4_BRIDGE_BASE", "http://127.0.0.1:8787"))

# Defer import so running this script doesn't require httpx unless used
from feature_recipes.lean4_provider import Lean4Provider  # noqa: E402


async def main() -> None:
    provider = Lean4Provider(base=BRIDGE_BASE)
    messages = [
        {
            "role": "system",
            "content": "You are coordinating deterministic Lean4 batch proofs.",
        },
        {
            "role": "user",
            "content": "Prove simple arithmetic identities for a quick smoke.",
        },
    ]
    requirements = [
        {"requirement_text": "0 + n = n", "context": {"section_id": "S1"}},
        {"requirement_text": "m + n = n + m", "context": {"section_id": "S2"}},
    ]

    print(
        json.dumps(
            {
                "example_request": {
                    "base": BRIDGE_BASE,
                    "messages": messages,
                    "requirements": requirements,
                    "flags": os.getenv("LEAN4_BRIDGE_FLAGS", "").split()
                    if os.getenv("LEAN4_BRIDGE_FLAGS")
                    else [],
                }
            },
            indent=2,
        )
    )
    try:
        resp = await provider.acomplete(
            messages=messages,
            requirements=requirements,
            flags=os.getenv("LEAN4_BRIDGE_FLAGS", "").split() if os.getenv("LEAN4_BRIDGE_FLAGS") else None,
            request_timeout=float(os.getenv("LEAN4_BRIDGE_TIMEOUT", "300")),
        )
        print(json.dumps({"example_response": resp}, indent=2))
    except Exception as exc:
        # Connection errors should SKIP, not fail the orchestrator
        msg = str(exc)
        if "Connection refused" in msg or "ConnectError" in msg or "Timeout" in msg:
            print(
                json.dumps(
                    {
                        "skip": True,
                        "reason": "lean4 bridge not reachable",
                        "hint": "Start: PYTHONPATH=src uvicorn lean4_prover.bridge.server:app --port 8787",
                    },
                    indent=2,
                )
            )
            sys.exit(0)
        print(json.dumps({"error": msg}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
