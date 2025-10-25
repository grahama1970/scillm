#!/usr/bin/env python3
from __future__ import annotations

"""
Live batched calls to Certainly (Lean4) via scillm.

Runs two modes against a live bridge:
  1) single-call batch: one /bridge/complete with N items
  2) parallel batches: split into chunks and run via Router.parallel_acompletions

Env:
  CERTAINLY_BRIDGE_BASE or LEAN4_BRIDGE_BASE (default http://127.0.0.1:8787)

Exit codes:
  0 on success; non-zero otherwise. Prints PASS/FAIL lines and counts.
"""

import os, sys, asyncio, json
from typing import List, Dict, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))  # repo root
from litellm import Router  # type: ignore
from scillm import completion  # type: ignore


def _make_items(n: int) -> List[Dict[str, Any]]:
    return [{"id": f"r{i+1}", "requirement_text": "forall n : Nat, n = n"} for i in range(n)]


def single_call_batch(base: str, n: int, timeout: float = 30.0) -> bool:
    items = _make_items(n)
    resp = completion(
        model="certainly/bridge",
        custom_llm_provider="certainly",
        api_base=base,
        messages=[{"role": "system", "content": "Lean4 batch"}],
        items=items,
        max_seconds=10,
        timeout=timeout,
    )
    content = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
    ok = isinstance(content, str) and len(content) > 0
    print(f"SINGLE_CALL_BATCH {n} {'PASS' if ok else 'FAIL'}")
    return ok


async def parallel_batches(base: str, n: int, batch_size: int = 5, timeout: float = 30.0) -> bool:
    router = Router(
        model_list=[
            {
                "model_name": "certainly-bridge",
                "litellm_params": {
                    "model": "certainly/bridge",
                    "custom_llm_provider": "certainly",
                    "api_base": base,
                },
            }
        ]
    )
    items = _make_items(n)
    chunks = [items[i : i + batch_size] for i in range(0, len(items), batch_size)]
    reqs = []
    for ch in chunks:
        reqs.append(
            {
                "model": "certainly-bridge",
                "messages": [{"role": "system", "content": "Lean4 parallel batch"}],
                "kwargs": {"items": ch, "max_seconds": 10, "timeout": timeout},
            }
        )
    res = await router.parallel_acompletions(requests=reqs)
    oks = 0
    for r in res:
        try:
            c = (r.get("choices") or [{}])[0].get("message", {}).get("content", "")
            if isinstance(c, str) and c:
                oks += 1
        except Exception:
            pass
    ok = oks == len(res) == len(chunks)
    print(f"PARALLEL_BATCHES chunks={len(chunks)} oks={oks} {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> int:
    base = (os.getenv("CERTAINLY_BRIDGE_BASE") or os.getenv("LEAN4_BRIDGE_BASE") or "http://127.0.0.1:8787").rstrip("/")
    n = int(os.getenv("CBATCH_N", "10"))
    bsz = int(os.getenv("CBATCH_SIZE", "5"))
    t = float(os.getenv("CBATCH_TIMEOUT", "30"))

    ok1 = single_call_batch(base, n=n, timeout=t)
    ok2 = asyncio.run(parallel_batches(base, n=n, batch_size=bsz, timeout=t))
    return 0 if (ok1 and ok2) else 31


if __name__ == "__main__":
    sys.exit(main())

