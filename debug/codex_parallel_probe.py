#!/usr/bin/env python3
"""Probe Router.parallel_acompletions() against a codex-agent endpoint.

Prints the returned content and attached scillm_router meta.

Usage:
  CODEX_AGENT_API_BASE=http://127.0.0.1:8077 \
  python debug/codex_parallel_probe.py

Notes:
  - CODEX_AGENT_API_BASE should NOT include '/v1'.
  - Works with the sidecar echo (CODEX_SIDECAR_ECHO=1) or a real gateway.
"""
from __future__ import annotations

import asyncio
import os
import json

from litellm import Router


async def main() -> int:
    base = os.getenv("CODEX_AGENT_API_BASE", "http://127.0.0.1:8077").rstrip("/")
    reqs = [
        {
            "model": "gpt-5",
            "messages": [
                {"role": "system", "content": "Return STRICT JSON only: {\"ok\":true}"}
            ],
            "kwargs": {
                "custom_llm_provider": "codex-agent",
                "api_base": base,
                "api_key": os.getenv("CODEX_AGENT_API_KEY", ""),
                "response_mode": "schema_first",
                "json_schema": {
                    "name": "ok",
                    "schema": {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                    },
                },
                "retry_enabled": True,
                "honor_retry_after": True,
                "timeout": 30,
            },
        }
    ]
    r = Router(deterministic=True)
    out = await r.parallel_acompletions(reqs, max_concurrency=1)
    # Support both dict and ParallelResult wrappers
    resp = out[0] if isinstance(out[0], dict) else out[0].response
    content = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content")
    meta = resp.get("scillm_router")
    print(json.dumps({"content": content, "scillm_router": meta}, ensure_ascii=False, indent=2))
    # Non-empty string content and present meta are success criteria
    ok = isinstance(content, str) and meta is not None
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

