#!/usr/bin/env python3
"""
CodeWorld MCTS (:auto alias) live release scenario.

Behavior
- Skip-friendly: if CODEWORLD_BASE (default :8887) isn't serving /healthz, exits 0 with a note.
- Otherwise:
  1) Calls model="codeworld/mcts" (alias sugar) with explicit variants.
  2) Calls model="codeworld/mcts:auto" (autogenerate N approaches), then verifies run_manifest.mcts_stats.
- Exits non-zero if the live call returns but required fields are missing.

Env
- CODEWORLD_BASE (default http://127.0.0.1:8888)
- CODEWORLD_ENABLE_MCTS_GENERATE=1 to enable :auto generation
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict

import httpx
from litellm import completion


def _probe(base: str) -> bool:
    try:
        with httpx.Client(timeout=3.0) as c:
            r = c.get(base.rstrip("/") + "/healthz")
            return r.status_code == 200
    except Exception:
        return False


def _require(cond: bool, msg: str) -> None:
    if not cond:
        print(f"[mcts:auto] requirement failed: {msg}")
        sys.exit(2)


def main() -> None:
    base = os.getenv("CODEWORLD_BASE", "http://127.0.0.1:8888").rstrip("/")
    if not _probe(base):
        print(f"[skip] CodeWorld bridge not reachable at {base}; skipping live :auto scenario.")
        sys.exit(0)

    # 1) Alias sugar: codeworld/mcts
    items = [{
        "task": "mcts-live",
        "context": {"code_variants": {
            "A": "def solve(ctx): return 1",
            "B": "def solve(ctx): return 2",
            "C": "def solve(ctx): return 3",
        }}
    }]
    # Prefer a direct bridge POST here to avoid provider import/gating surprises in live envs.
    # This exercises the same surface the provider uses under the hood.
    alias_payload = {
        "messages": [{"role": "user", "content": "Alias check"}],
        "items": items,
        "provider": {"name": "codeworld", "args": {"strategy": "mcts"}},
    }
    with httpx.Client(timeout=10.0) as c:
        r = c.post(base + "/bridge/complete", json=alias_payload)
        _require(r.status_code == 200, f"alias HTTP status {r.status_code}")
        extra_alias: Dict[str, Any] = r.json()
    mcts_alias = (extra_alias.get("results") or [{}])[0].get("mcts") or {}
    _require("best_variant" in mcts_alias, "alias call missing mcts.best_variant")

    # 2) Preferred: one-POST :auto (bridge generates variants via codex-agent and runs MCTS)
    if str(os.getenv("CODEWORLD_ENABLE_MCTS_GENERATE", "1")).lower() not in ("0", "false", "no"):
        one_post = {
            "messages": [{"role": "user", "content": "Autogenerate variants then search"}],
            "items": [{"task": "mcts-live-auto", "context": {}}],
            "provider": {
                "name": "codeworld",
                "args": {
                    "strategy": "mcts",
                    "strategy_config": {
                        "autogenerate": {
                            "enabled": True,
                            "n": 3,
                            # Allow bridge to read CODEX_AGENT_* env; callers may override:
                            # "generator_model": os.getenv("CODEX_AGENT_MODEL", "gpt-5"),
                            # "temperature": 0.0,
                            # "max_tokens": 2000,
                        },
                        "rollouts": 24,
                        "depth": 6,
                        "uct_c": 1.25,
                    },
                },
            },
        }
        tmo = float(os.getenv("CODEWORLD_ONEPOST_TIMEOUT_S", "60") or "60")
        with httpx.Client(timeout=tmo) as c:
            r2 = c.post(base + "/bridge/complete", json=one_post)
        if r2.status_code == 200:
            extra_auto = r2.json()
            stats = (extra_auto.get("run_manifest") or {}).get("mcts_stats") or {}
            if stats.get("best_variant") is not None and "seed" in stats:
                print("mcts_stats (one-POST):", json.dumps(stats, indent=2))
                sys.exit(0)
            else:
                print("[warn] one-POST :auto returned 200 but missing mcts_stats; falling back to two-step.")

    # 2b) Fallback: two-step (generate via codex-agent from this process, then MCTS on bridge)
    cab = os.getenv("CODEX_AGENT_API_BASE", "http://127.0.0.1:8089").rstrip("/")
    prompt = (
        "Generate exactly 3 JSON variants with fields id,title,complexity_tier,rationale,code,notes. "
        "Return STRICT JSON with top-level key 'variants'."
    )
    req = {
        "model": os.getenv("CODEX_AGENT_MODEL", "gpt-5"),
        "messages": [
            {"role": "system", "content": "You produce strict JSON only."},
            {"role": "user", "content": prompt},
        ],
    }
    with httpx.Client(timeout=30.0) as c:
        rr = c.post(cab + "/v1/chat/completions", json=req)
        _require(rr.status_code == 200, f"codex-agent status {rr.status_code}")
        content = rr.json()["choices"][0]["message"]["content"]
    from src.codeworld.bridge.server import _mcts_extract_variants_from_raw
    vars = _mcts_extract_variants_from_raw(content)
    _require(isinstance(vars, list) and vars, "codex-agent returned no variants")
    mapping = {}
    for i, v in enumerate(vars, 1):
        if not isinstance(v, dict):
            continue
        vid = v.get("id") or f"v{i}"
        code = v.get("code") if isinstance(v.get("code"), str) else ""
        mapping[vid] = code
    _require(mapping, "variants mapping empty")

    payload2 = {
        "messages": [{"role": "user", "content": "Search over generated variants"}],
        "items": [{"task": "mcts-live-auto", "context": {"code_variants": mapping}}],
        "provider": {"name": "codeworld", "args": {"strategy": "mcts"}},
    }
    with httpx.Client(timeout=30.0) as c:
        r3 = c.post(base + "/bridge/complete", json=payload2)
        _require(r3.status_code == 200, f":auto(MCTS) HTTP status {r3.status_code}")
        extra_auto = r3.json()
    stats = (extra_auto.get("run_manifest") or {}).get("mcts_stats") or {}
    print("mcts_stats (two-step):", json.dumps(stats, indent=2))
    _require("best_variant" in stats and stats.get("best_variant") is not None, ":auto missing run_manifest.mcts_stats.best_variant")
    _require("seed" in stats, ":auto missing run_manifest.mcts_stats.seed")
    sys.exit(0)


if __name__ == "__main__":
    main()
