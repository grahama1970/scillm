#!/usr/bin/env python3
from __future__ import annotations
import os, json
from scillm.extras.multi_agents import answer_code_autogen_and_judge_codex_only

def main() -> None:
    os.environ.setdefault("CODEX_AGENT_API_BASE", "http://127.0.0.1:8089")
    items = [{"task": "Six improved fast inverse square root variants for a gaming plugin (C/C++).", "context": {}}]
    res = answer_code_autogen_and_judge_codex_only(
        items,
        n_variants=6,
        generator_model="gpt-5",
        temperature=0.0,
        max_tokens=2000,
        judge_model="codex-agent/gpt-5",
        timeout=90.0,
    )
    vars = res.get("variants") or []
    print("variants_count", len(vars))
    print("judge", json.dumps(res.get("judge"), ensure_ascii=False))

if __name__ == "__main__":
    main()

