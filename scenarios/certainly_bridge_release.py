#!/usr/bin/env python3
"""Certainly (multi-prover surface) — live bridge scenario.

Backend is selected via CERTAINLY_BACKEND (default: lean4). Uses the same
canonical request/response as lean4, posting to /bridge/complete.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

import httpx
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

BASE = os.getenv("CERTAINLY_BRIDGE_BASE", os.getenv("LEAN4_BRIDGE_BASE", "http://127.0.0.1:8787")).rstrip("/")
BACKEND = (os.getenv("CERTAINLY_BACKEND") or "lean4").lower()


def main() -> int:
    try:
        httpx.get(f"{BASE}/healthz", timeout=5.0).raise_for_status()
    except Exception as exc:
        print(json.dumps({"skip": True, "reason": str(exc), "hint": "Start Lean4/Certainly bridge on 8787"}, indent=2))
        return 0

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": f"Certainly backend={BACKEND}"},
        {"role": "user", "content": "Prove simple arithmetic identities"},
    ]
    items = [
        {"requirement_text": "0 + n = n", "context": {"section_id": "C1"}},
        {"requirement_text": "m + n = n + m", "context": {"section_id": "C2"}},
    ]

    # For now the lean4 backend understands lean4_requirements; items also supported in provider.
    body = {"messages": messages, "lean4_requirements": items}
    try:
        r = httpx.post(f"{BASE}/bridge/complete", json=body, timeout=180.0)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        return 1

    print(json.dumps({"ok": True, "summary": data.get("summary", {}), "backend": BACKEND}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

