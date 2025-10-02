"""Lean4 Router shim example.

This small adapter gives a Router-like call for Lean4 without touching the
LiteLLM provider registry. It mirrors the CodeWorld pattern so users call both
providers the same way.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List

from dotenv import find_dotenv, load_dotenv

from .lean4_provider import Lean4Provider

load_dotenv(find_dotenv())


class Lean4Router:
    def __init__(self, base: str):
        self.provider = Lean4Provider(base=base)

    async def acompletion(
        self,
        *,
        messages: List[Dict[str, Any]],
        items: List[Dict[str, Any]],
        flags: List[str] | None = None,
        timeout: float = 300.0,
    ) -> Dict[str, Any]:
        return await self.provider.acomplete(
            messages=messages,
            requirements=items,
            flags=flags,
            request_timeout=timeout,
        )


async def main() -> None:
    base = os.getenv("LEAN4_BRIDGE_BASE", "http://127.0.0.1:8787")
    router = Lean4Router(base)
    messages = [
        {"role": "system", "content": "Lean4 Router shim demo"},
        {"role": "user", "content": "Prove simple arithmetic identities"},
    ]
    items = [
        {"requirement_text": "0 + n = n", "context": {"section_id": "S1"}},
        {"requirement_text": "m + n = n + m", "context": {"section_id": "S2"}},
    ]
    resp = await router.acompletion(messages=messages, items=items, flags=os.getenv("LEAN4_BRIDGE_FLAGS", "").split() or None)
    print(json.dumps(resp, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

