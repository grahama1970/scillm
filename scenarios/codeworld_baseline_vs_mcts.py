#!/usr/bin/env python3
"""
Baseline CodeWorld vs CodeWorld+MCTS side‑by‑side demo.

Runs the same items twice:
  1) Baseline CodeWorld (no strategy)
  2) CodeWorld with strategy="mcts" (sugar one‑liner)

Prints a compact summary and any MCTS telemetry returned.
Respects:
  - CODEWORLD_BASE (default http://127.0.0.1:8887)
  - SCILLM_DETERMINISTIC_SEED (deterministic runs)
  - CI auto‑scaling (MCTS) via SCILLM_CI=1 or GITHUB_ACTIONS=true
"""
from __future__ import annotations

import os
from litellm import completion


def _items():
    return [{
        "task": "demo-compare",
        "context": {
            "inputs": {"n": 100},
            "code_variants": {
                "variant_linear": "def solve(ctx): return {'result': 1}",
                "variant_heap": "def solve(ctx): return {'result': 1}",
                "variant_radix": "def solve(ctx): return {'result': 1}",
            }
        }
    }]


def _summarize(label: str, resp) -> None:
    print(f"\n== {label} ==")
    # Short assistant content
    try:
        print("assistant:", resp.choices[0].message["content"])  # type: ignore[index]
    except Exception:
        try:
            print("assistant:", resp.choices[0].message.content)
        except Exception:
            pass
    # Full CodeWorld payload & MCTS block if any
    cw = getattr(resp, "additional_kwargs", {}).get("codeworld") if hasattr(resp, "additional_kwargs") else None
    if cw:
        s = cw.get("summary") or {}
        print("summary:", {k: s.get(k) for k in ("items", "succeeded", "failed")})
        results = cw.get("results") or []
        if results:
            mcts = results[0].get("mcts")
            if mcts:
                print("mcts:", {k: mcts.get(k) for k in ("best_variant", "rollouts", "depth", "uct_c", "seed")})


def main() -> None:
    base = os.getenv("CODEWORLD_BASE", "http://127.0.0.1:8887")
    items = _items()
    messages = [{"role": "user", "content": "Compare baseline vs MCTS"}]

    # Baseline
    resp_base = completion(
        model="codeworld",
        custom_llm_provider="codeworld",
        messages=messages,
        items=items,
        options={"session_id": "compare", "track_id": "baseline", "max_seconds": 10},
        api_base=base,
    )
    _summarize("Baseline CodeWorld", resp_base)

    # MCTS (sugar)
    resp_mcts = completion(
        model="codeworld",
        custom_llm_provider="codeworld",
        messages=messages,
        items=items,
        strategy="mcts",
        # either uct_c or exploration_constant accepted
        exploration_constant=1.25,
        options={"session_id": "compare", "track_id": "mcts", "max_seconds": 10},
        api_base=base,
    )
    _summarize("CodeWorld + MCTS", resp_mcts)


if __name__ == "__main__":
    main()

