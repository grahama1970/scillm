#!/usr/bin/env python3
from __future__ import annotations
import os
from scillm.extras import ensure_codex_agent
from scillm.extras.multi_agents import answer_code_mcts_autogen_and_judge

def main() -> None:
    os.environ.setdefault("SCILLM_ENABLE_CODEWORLD", "1")
    ensure_codex_agent()  # uses CODEX_AGENT_API_BASE
    base = os.getenv("CODEWORLD_BASE", "http://127.0.0.1:8888")
    items=[{"task":"six improved fast inverse square root for gaming (C/C++)","context":{}}]
    res = answer_code_mcts_autogen_and_judge(items,
        n_variants=6, rollouts=12, depth=4, uct_c=1.3, temperature=0.0,
        codeworld_base=base, judge_model="gpt-5", timeout=120.0)
    cw = res.get("codeworld") or {}
    r0 = (cw.get("results") or [{}])[0]
    print("variants_present", bool(r0.get("code_variants")))
    print("mcts_best_value", (r0.get("mcts") or {}).get("best_value"))
    print("judge", res.get("judge"))

if __name__ == "__main__":
    main()

