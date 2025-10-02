#!/usr/bin/env python3
"""Certainly via Router (alias provider) — live scenario.

Uses the generic provider alias 'certainly' with backend placeholder.
"""
from __future__ import annotations

import json
import os
import sys
from dotenv import find_dotenv, load_dotenv

from litellm import Router

load_dotenv(find_dotenv())

if os.getenv("LITELLM_ENABLE_LEAN4") != "1" and os.getenv("LITELLM_ENABLE_CERTAINLY") != "1":
    print("Skipping Certainly Router scenario (set LITELLM_ENABLE_CERTAINLY=1 or LITELLM_ENABLE_LEAN4=1).")
    sys.exit(0)

BASE = os.getenv("CERTAINLY_BRIDGE_BASE", os.getenv("LEAN4_BRIDGE_BASE", "http://127.0.0.1:8787"))
BACKEND = (os.getenv("CERTAINLY_BACKEND") or "lean4").lower()

os.environ.setdefault("LITELLM_ENABLE_CERTAINLY", "1")

model_list = [
    {
        "model_name": "certainly-bridge",
        "litellm_params": {
            "model": "certainly/bridge",
            "custom_llm_provider": "certainly",
            "api_base": BASE,
        },
    }
]

router = Router(model_list=model_list)

messages = [
    {"role": "system", "content": "Certainly Router scenario"},
    {"role": "user", "content": "Prove simple arithmetic identities"},
]

items = [
    {"requirement_text": "0 + n = n", "context": {"section_id": "C1"}},
    {"requirement_text": "m + n = n + m", "context": {"section_id": "C2"}},
]

print(json.dumps({"model_list": model_list, "messages": messages, "items": items, "backend": BACKEND}, indent=2))

try:
    out = router.completion(
        model="certainly-bridge",
        messages=messages,
        items=items,
        backend=BACKEND,
        max_seconds=180,
    )
    payload = out.model_dump() if hasattr(out, "model_dump") else str(out)
    print(json.dumps({"example_response": payload}, indent=2))
except Exception as exc:
    msg = str(exc)
    if "Connection" in msg or "refused" in msg or "Failed to establish" in msg:
        print(json.dumps({"skip": True, "reason": "bridge not reachable", "hint": "Start bridge on 8787"}, indent=2))
        sys.exit(0)
    print(json.dumps({"error": msg}, indent=2))
    sys.exit(1)

