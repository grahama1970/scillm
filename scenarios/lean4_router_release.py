#!/usr/bin/env python3
"""Lean4 via Router (custom provider) — live scenario.

Requires: LITELLM_ENABLE_LEAN4=1 and a running Lean4 bridge.
"""
from __future__ import annotations

import json
import os
import sys
from dotenv import find_dotenv, load_dotenv

from litellm import Router

load_dotenv(find_dotenv())

if os.getenv("LITELLM_ENABLE_LEAN4") != "1":
    print("Skipping Lean4 Router scenario (set LITELLM_ENABLE_LEAN4=1).")
    sys.exit(0)

BASE = os.getenv("CERTAINLY_BRIDGE_BASE", os.getenv("LEAN4_BRIDGE_BASE", "http://127.0.0.1:8787"))

# Enable provider by default for local runs if not explicitly disabled
os.environ.setdefault("LITELLM_ENABLE_LEAN4", "1")

model_list = [
    {
        "model_name": "lean4-bridge",
        "litellm_params": {
            "model": "lean4/bridge",  # logical label; not used by backend
            "custom_llm_provider": "lean4",
            "api_base": BASE,
        },
    }
]

router = Router(model_list=model_list)

messages = [
    {"role": "system", "content": "Lean4 Router scenario"},
    {"role": "user", "content": "Prove simple arithmetic identities"},
]

items = [
    {"requirement_text": "0 + n = n", "context": {"section_id": "S1"}},
    {"requirement_text": "m + n = n + m", "context": {"section_id": "S2"}},
]

print(json.dumps({"model_list": model_list, "messages": messages, "items": items}, indent=2))

try:
    out = router.completion(
        model="lean4-bridge",
        messages=messages,
        # Send items via optional_params per provider adapter
        items=items,
        max_seconds=180,
    )
    payload = out.model_dump() if hasattr(out, "model_dump") else str(out)
    print(json.dumps({"example_response": payload}, indent=2))
except Exception as exc:
    msg = str(exc)
    if "Connection" in msg or "refused" in msg or "Failed to establish" in msg:
        print(json.dumps({"skip": True, "reason": "lean4 bridge not reachable", "hint": "Start bridge on 8787"}, indent=2))
        sys.exit(0)
    print(json.dumps({"error": msg}, indent=2))
    sys.exit(1)
