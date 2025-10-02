#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import httpx
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

BASE = os.getenv("CODEWORLD_BASE", "http://127.0.0.1:8887")


def main() -> int:
    # health
    try:
        r = httpx.get(f"{BASE}/healthz", timeout=10.0)
        r.raise_for_status()
        health = r.json()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"healthz failed: {exc}"}, indent=2))
        return 1

    # Example dynamic scoring function
    scoring_py = """
def score(task, context, outputs, timings):
    expected = context.get('expected')
    result = outputs.get('result')
    correctness = 1.0 if (expected is not None and result == expected) else 0.0
    duration_ms = float(timings.get('duration_ms', 0))
    speed = max(0.0, min(1.0, 1.0 - duration_ms/1000.0))
    agg = 0.7*correctness + 0.3*speed
    return {"correctness": correctness, "speed": speed, "aggregate": agg}
"""

    body = {
        "messages": [{"role": "system", "content": "eval"}],
        "items": [
            {"task": "strategy_compare", "context": {"section_id": "CW1", "expected": 10, "code_variants": {"v1": "def solve(xs): return sum(xs)"}}},
            {"task": "strategy_compare", "context": {"section_id": "CW2", "expected": 10, "code_variants": {"v1": "def solve(xs): return sum(xs)"}}},
            {"task": "strategy_compare", "context": {"section_id": "CW3", "expected": 10, "code_variants": {"v1": "def solve(xs): return sum(xs)"}}},
        ],
        "provider": {"name": "codeworld", "args": {"metrics": ["correctness", "speed", "brevity"], "iterations": 1, "scoring": {"lang": "py", "entry": "score", "code": scoring_py}, "judge": True}},
        "options": {"max_seconds": 30},
    }
    try:
        rr = httpx.post(f"{BASE}/bridge/complete", json=body, timeout=60.0)
        rr.raise_for_status()
        data = rr.json()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"eval failed: {exc}"}, indent=2))
        return 1

    print(json.dumps({"ok": True, "summary": data.get("summary", {}), "statistics": data.get("statistics", {}), "signals": data.get("signals", {})}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
