#!/usr/bin/env python3
"""
CodeWorld MCTS autogeneration via Chutes (OpenAI‑compatible) — paved path

Requirements:
- CodeWorld bridge running on :8888
- CHUTES_API_BASE, CHUTES_API_KEY set (bearer accepted)
- CHUTES_TEXT_MODEL set to a valid text model id from GET $CHUTES_API_BASE/models

This script posts /bridge/complete with provider.args.strategy=mcts and an
autogenerate block that calls the LLM at CHUTES via Authorization: Bearer.
"""
from __future__ import annotations

import os, json, sys, urllib.request

BASE = os.getenv("CODEWORLD_BASE", "http://127.0.0.1:8888").rstrip("/")
CHUTES_MODEL = os.getenv("CHUTES_TEXT_MODEL") or "Qwen/Qwen3-235B-A22B-Instruct-2507"

def main() -> int:
    url = BASE + "/bridge/complete"
    body = {
        "messages": [
            {"role": "system", "content": "CodeWorld MCTS via Chutes"},
            {"role": "user", "content": "Compare quicksort vs mergesort for arrays of 1024 integers."},
        ],
        "provider": {
            "name": "codeworld",
            "args": {
                "strategy": "mcts",
                "strategy_config": {
                    "rollouts": 16,
                    "depth": 6,
                    "uct_c": 1.4,
                    "autogenerate": {
                        "enabled": True,
                        "n": 3,
                        "model": CHUTES_MODEL,
                        "temperature": 0.0,
                        "max_tokens": 1200,
                    },
                },
            },
        },
        "options": {"max_seconds": 30},
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        out = json.loads(r.read().decode("utf-8"))
    results = out.get("results") or []
    first = results[0] if results else {}
    ok = bool(first.get("code_variants")) and bool(first.get("mcts"))
    print("OK", int(ok))
    return 0 if ok else 31

if __name__ == "__main__":
    sys.exit(main())

