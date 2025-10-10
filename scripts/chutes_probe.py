#!/usr/bin/env python3
"""
Skip-friendly Router→Chutes alias probe.

- Skips (exit 0) if CHUTES_API_KEY is not set.
- Calls Router with an openai/<org>/<model> alias and asserts non-empty content.
"""
import os
import sys
import asyncio

from litellm import Router


async def main() -> int:
    if not os.getenv("CHUTES_API_KEY"):
        print("[probe] CHUTES_API_KEY not set; skipping.")
        return 0
    # Accept custom alias via env to avoid hard-coding models
    model = os.getenv("CHUTES_PROBE_MODEL", "openai/Qwen/Qwen2.5-VL-72B-Instruct")
    r = Router(deterministic=True)
    messages = [
        {"role": "system", "content": "Return STRICT JSON only: {\"ok\": true}"},
        {"role": "user", "content": "ok"},
    ]
    resp = await r.acompletion(model=model, messages=messages, response_format={"type": "json_object"})
    # Validate
    try:
        content = resp.choices[0].message["content"]  # type: ignore[index]
    except Exception:
        content = getattr(getattr(resp.choices[0], "message", object()), "content", None)  # type: ignore[attr-defined]
    meta = getattr(resp, "additional_kwargs", {}).get("router", {})
    if not content or (isinstance(content, str) and not content.strip()):
        print("[probe] FAIL: empty content")
        print("[probe-meta]", meta)
        return 2
    if meta.get("error_type") not in (None, "ok"):
        print("[probe] WARN: error_type=", meta.get("error_type"))
        print("[probe-meta]", meta)
    print("[probe] OK: content + meta good")
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)

