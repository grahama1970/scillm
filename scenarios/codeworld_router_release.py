#!/usr/bin/env python3
"""CodeWorld via Router (custom provider) — live scenario.

Uses custom_llm_provider="codeworld" to call the CodeWorld bridge.
"""
from __future__ import annotations

import json
import os
import sys
from dotenv import find_dotenv, load_dotenv

from litellm import Router

load_dotenv(find_dotenv())

BASE = os.getenv("CODEWORLD_BASE", "http://127.0.0.1:8887")

# Enable provider by default for local runs if not explicitly disabled
os.environ.setdefault("LITELLM_ENABLE_CODEWORLD", "1")

model_list = [
    {
        "model_name": "codeworld-bridge",
        "litellm_params": {
            "model": "codeworld/bridge",
            "custom_llm_provider": "codeworld",
            "api_base": BASE,
        },
    }
]

router = Router(model_list=model_list)

messages = [
    {"role": "system", "content": "CodeWorld Router scenario"},
    {"role": "user", "content": "Compare two strategies and pick one."},
]

items = [
    {"task": "strategy_compare", "context": {"section_id": "CW1"}},
    {"task": "strategy_compare", "context": {"section_id": "CW2"}},
]

print(json.dumps({"model_list": model_list, "messages": messages, "items": items}, indent=2))

try:
    out = router.completion(
        model="codeworld-bridge",
        messages=messages,
        items=items,
        options={"max_seconds": 60},
        codeworld_metrics=["correctness", "speed"],
        codeworld_iterations=1,
        codeworld_allowed_languages=["python"],
        request_timeout=60.0,
    )
    payload = out.model_dump() if hasattr(out, "model_dump") else str(out)
    print(json.dumps({"example_response": payload}, indent=2))
except Exception as exc:
    msg = str(exc)
    if "Connection" in msg or "refused" in msg or "Failed to establish" in msg:
        print(json.dumps({"skip": True, "reason": "codeworld bridge not reachable", "hint": "Start bridge on 8887"}, indent=2))
        sys.exit(0)
    print(json.dumps({"error": msg}, indent=2))
    sys.exit(1)
