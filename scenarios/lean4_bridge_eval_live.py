#!/usr/bin/env python3
# Live/E2E eval for the Lean4 bridge. Requires LEAN4_BRIDGE_BASE and a running server.
from __future__ import annotations

import json
import os
import sys
import httpx
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

BASE = os.getenv("CERTAINLY_BRIDGE_BASE", os.getenv("LEAN4_BRIDGE_BASE", "http://127.0.0.1:8787"))


def main() -> int:
    try:
        # health check
        r = httpx.get(f"{BASE}/healthz", timeout=10.0)
        r.raise_for_status()
        health = r.json()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"healthz failed: {exc}"}, indent=2))
        return 1

    body = {
        "messages": [{"role": "system", "content": "eval"}],
        "lean4_requirements": [
            {"requirement_text": "0 + n = n"},
            {"requirement_text": "m + n = n + m"},
            {"requirement_text": "(n * 0) = 0"},
        ],
        "lean4_flags": ["--deterministic", "--no-llm"],
        "max_seconds": 120,
    }
    try:
        rr = httpx.post(f"{BASE}/bridge/complete", json=body, timeout=180.0)
        rr.raise_for_status()
        data = rr.json()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"eval batch failed: {exc}"}, indent=2))
        return 1

    stats = data.get("summary", {})
    out = {"ok": True, "health": health, "summary": stats}
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
