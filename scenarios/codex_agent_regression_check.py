#!/usr/bin/env python3
"""Codex-Agent regression check (Router + parallel_acompletions).

Ensures:
- Parallel returns an OpenAI-shaped dict with 'choices' and 'scillm_router'.
- choices[0].message.content is a string (may be empty in provider_error case).
- scillm_router.error_type is one of {ok, invalid_json, empty_content, provider_error, timeout}.

Env:
- LITELLM_ENABLE_CODEX_AGENT=1 (required)
- CODEX_AGENT_API_BASE (default http://127.0.0.1:8077; omit /v1)
- CODEX_AGENT_API_KEY (optional)
- SCILLM_RETRY_META=1 and CODEX_AGENT_ENABLE_METRICS=1 (optional; prints retries when present)

Exit codes:
 0 = regression check passed
 2 = failed (missing keys or invalid shapes)
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict

from litellm import Router


def _ok_resp(resp: Dict[str, Any]) -> tuple[bool, str]:
    if not isinstance(resp, dict):
        return False, "response is not a dict"
    if "choices" not in resp:
        return False, "missing choices"
    if "scillm_router" not in resp:
        return False, "missing scillm_router"
    try:
        content = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content")
    except Exception:
        content = None
    if content is None:
        return False, "content is None"
    et = (resp.get("scillm_router") or {}).get("error_type")
    if et not in {"ok", "invalid_json", "empty_content", "provider_error", "timeout"}:
        return False, f"unexpected error_type: {et}"
    return True, "ok"


async def main() -> int:
    base = os.getenv("CODEX_AGENT_API_BASE", "http://127.0.0.1:8077").rstrip("/")
    key = os.getenv("CODEX_AGENT_API_KEY", "")
    if os.getenv("LITELLM_ENABLE_CODEX_AGENT") != "1":
        print(json.dumps({"skip": True, "reason": "LITELLM_ENABLE_CODEX_AGENT!=1"}, indent=2))
        return 0

    r = Router(deterministic=True)
    reqs = [
        {
            "model": "gpt-5",
            "messages": [
                {"role": "system", "content": "Return STRICT JSON only: {\"ok\": true}"}
            ],
            "kwargs": {
                "custom_llm_provider": "codex-agent",
                "api_base": base,
                "api_key": key,
                "response_mode": "schema_first",
                "json_schema": {
                    "name": "ok",
                    "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                },
                "timeout": 30,
            },
        }
    ]
    out = await r.parallel_acompletions(reqs, max_concurrency=1)
    resp = out[0] if isinstance(out[0], dict) else out[0].response
    ok, reason = _ok_resp(resp)
    print(json.dumps({"ok": ok, "reason": reason, "resp": resp}, ensure_ascii=False, indent=2))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

