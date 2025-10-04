"""
Demonstration of MCTS variant selection via SciLLM CodeWorld provider.

Prereqs:
  export CODEWORLD_BASE=http://127.0.0.1:8887
  # Optional deterministic seed
  export SCILLM_DETERMINISTIC_SEED=7

Run:
  python scenarios/mcts_codeworld_demo.py
"""

from __future__ import annotations

import os
from litellm import completion


def main() -> None:
    base = os.getenv("CODEWORLD_BASE", "http://127.0.0.1:8887")
    items = [
        {
            "task": "demo-mcts",
            "context": {
                "inputs": {"n": 100},
                "code_variants": {
                    "variant_linear": "def solve(ctx): return {'result': 1}",
                    "variant_heap": "def solve(ctx): return {'result': 1}",
                    "variant_radix": "def solve(ctx): return {'result': 1}",
                },
            },
        }
    ]

    resp = completion(
        model="codeworld",
        messages=[{"role": "user", "content": "Run adaptive search"}],
        custom_llm_provider="codeworld",
        items=items,
        provider={
            "name": "codeworld",
            "args": {
                "strategy": "mcts",
                "strategy_config": {"name": "mcts", "rollouts": 40, "depth": 6, "uct_c": 1.25},
            },
        },
        options={"session_id": "mcts-demo", "track_id": "t1", "max_seconds": 10},
        api_base=base,
    )
    print(resp.choices[0].message["content"])  # type: ignore[index]
    extra = getattr(resp, "additional_kwargs", {}).get("codeworld") if hasattr(resp, "additional_kwargs") else None
    if extra:
        print("MCTS:", extra["results"][0].get("mcts"))


if __name__ == "__main__":
    main()

