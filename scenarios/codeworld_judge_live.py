#!/usr/bin/env python3
from __future__ import annotations

"""CodeWorld judge demo (live E2E).

Sends three strategy variants and prints weighted vs lex leaderboards.
Deterministic, no LLM.
"""

import json
import os
import sys
from typing import Any, Dict, List

import httpx
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())
BASE = os.getenv("CODEWORLD_BASE", "http://127.0.0.1:8887")


def _post(body: Dict[str, Any]) -> Dict[str, Any]:
    r = httpx.post(f"{BASE}/bridge/complete", json=body, timeout=60.0)
    r.raise_for_status()
    return r.json()


def leaderboard_weighted(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for r in results:
        ctx = r.get("item", {}).get("context", {})
        vdict = ctx.get("code_variants") or {}
        name = next(iter(vdict.keys())) if vdict else "v?"
        rows.append({
            "name": name,
            "aggregate": r.get("aggregate_judge", r.get("scores", {}).get("aggregate", 0.0)),
            "scores": r.get("scores_judge", r.get("scores", {})),
        })
    return sorted(rows, key=lambda x: (x.get("aggregate") or 0.0), reverse=True)


def leaderboard_lex(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for r in results:
        ctx = r.get("item", {}).get("context", {})
        vdict = ctx.get("code_variants") or {}
        name = next(iter(vdict.keys())) if vdict else "v?"
        key = r.get("aggregate_judge_lex") or []
        rows.append({"name": name, "key": key})
    return sorted(rows, key=lambda x: (-(x["key"][0] if x.get("key") else 0), -(x["key"][1] if x.get("key") else 0), -(x["key"][2] if x.get("key") else 0)))


def main() -> int:
    # health
    try:
        httpx.get(f"{BASE}/healthz", timeout=5.0).raise_for_status()
    except Exception as exc:
        print(json.dumps({"skip": True, "reason": f"{exc}", "hint": "Start CodeWorld bridge on :8887"}, indent=2))
        return 0

    # Variants: same correctness; vary brevity and simulated speed
    variants = {
        "brief_fast": "def solve(xs):\n return sum(xs)",
        "verbose_fast": "def solve(xs):\n s=0\n for x in xs:\n  s+=x\n return s\n",
        "brief_slow": "def solve(xs):\n import time\n time.sleep(0.0)\n return sum(xs)",
    }

    # Weighted
    body_w = {
        "messages": [{"role": "system", "content": "judge demo"}],
        "items": [
            {"task": "strategy_compare", "context": {"section_id": "CWJ1", "expected": 10, "code_variants": {k: v}}}
            for k, v in variants.items()
        ],
        "provider": {"name": "codeworld", "args": {"judge": True, "judge_mode": "weighted"}},
        "options": {"max_seconds": 10, "session_id": "judge-demo", "track_id": "cw-alpha"},
    }
    data_w = _post(body_w)

    # Lex
    body_l = dict(body_w)
    body_l["provider"] = {"name": "codeworld", "args": {"judge": True, "judge_mode": "lex"}}
    data_l = _post(body_l)

    out = {
        "weighted": leaderboard_weighted(data_w.get("results", [])),
        "lex": leaderboard_lex(data_l.get("results", [])),
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

