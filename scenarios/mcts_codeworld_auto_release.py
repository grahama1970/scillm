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
- CODEWORLD_BASE (default http://127.0.0.1:8887)
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
    base = os.getenv("CODEWORLD_BASE", "http://127.0.0.1:8887").rstrip("/")
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
    resp_alias = completion(
        model="codeworld/mcts",
        custom_llm_provider="codeworld",
        messages=[{"role":"user","content":"Alias check"}],
        items=items,
        api_base=base,
    )
    extra_alias: Dict[str, Any] = getattr(resp_alias, "additional_kwargs", {}).get("codeworld") or {}
    mcts_alias = (extra_alias.get("results") or [{}])[0].get("mcts") or {}
    _require("best_variant" in mcts_alias, "alias call missing mcts.best_variant")

    # 2) :auto generation path (gate must be enabled by env)
    if str(os.getenv("CODEWORLD_ENABLE_MCTS_GENERATE", "1")).lower() in ("0", "false", "no"):
        print("[info] CODEWORLD_ENABLE_MCTS_GENERATE is disabled; skipping :auto call.")
        sys.exit(0)

    resp_auto = completion(
        model="codeworld/mcts:auto",
        custom_llm_provider="codeworld",
        messages=[{"role":"user","content":"Generate approaches then search"}],
        api_base=base,
    )
    extra_auto: Dict[str, Any] = getattr(resp_auto, "additional_kwargs", {}).get("codeworld") or {}
    stats = (extra_auto.get("run_manifest") or {}).get("mcts_stats") or {}
    print("mcts_stats:", json.dumps(stats, indent=2))
    _require("best_variant" in stats and stats.get("best_variant") is not None, ":auto missing run_manifest.mcts_stats.best_variant")
    _require("seed" in stats, ":auto missing run_manifest.mcts_stats.seed")
    sys.exit(0)


if __name__ == "__main__":
    main()

