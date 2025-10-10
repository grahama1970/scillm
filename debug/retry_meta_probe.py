#!/usr/bin/env python3
"""Show scillm_router.retries when enabled.

Usage:
  SCILLM_RETRY_META=1 CODEX_AGENT_ENABLE_METRICS=1 \
  CODEX_AGENT_API_BASE=http://127.0.0.1:8077 \
  python debug/retry_meta_probe.py
"""
import asyncio
import json
import os
from litellm import Router


async def main() -> int:
    r = Router(deterministic=True)
    resp = await r.acompletion(
        model="gpt-5",
        messages=[{"role": "user", "content": "Return STRICT JSON only: {\\\"ok\\\": true}"}],
        custom_llm_provider="codex-agent",
        api_base=os.getenv("CODEX_AGENT_API_BASE", "http://127.0.0.1:8077"),
        api_key=os.getenv("CODEX_AGENT_API_KEY", ""),
        timeout=20,
    )
    meta = getattr(resp, "additional_kwargs", {}).get("router", {})
    out = {
        "content": getattr(getattr(resp, "choices")[0].message, "content", None),
        "router_meta": meta,
    }
    print(json.dumps(out, indent=2))
    # Non-blocking: exit 0 regardless, this is a display probe
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
