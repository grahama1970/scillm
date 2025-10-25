#!/usr/bin/env python3
from __future__ import annotations

"""
Certainly (Lean4) live batch with a mixed set of 10 requirements.

Composes 5 engineering-centric, 3 mathematics-centric, and 2 formal-methods
requirements, then runs:
  1) Single-call batch (/bridge/complete with 10 items)
  2) Optional parallel chunks via Router.parallel_acompletions

Env:
  CERTAINLY_BRIDGE_BASE or LEAN4_BRIDGE_BASE (default http://127.0.0.1:8787)
  MIXED_PARALLEL=1 to also run the parallel path

Exit 0 on success (non-empty content; proved summary reported). Prints p50/p95
if provided by the bridge.
"""

import os, sys, json, asyncio
from typing import List, Dict, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))  # repo root
from scillm import completion  # type: ignore
from litellm import Router  # type: ignore


def _items() -> List[Dict[str, Any]]:
    eng = [
        "A FIFO queue preserves order: pushing then popping returns the first element.",
        "Two's complement addition overflows only when signs match and result sign differs.",
        "Mutex ensures mutual exclusion: no two threads hold the lock simultaneously.",
        "CRC32 detects all single-bit errors in a message.",
        "Sorting with merge sort yields a non-decreasing sequence.",
    ]
    math = [
        "For all n : Nat, n + 0 = n.",
        "For all m n : Nat, m + n = n + m.",
        "For all x : Real, 0 ≤ x^2.",
    ]
    fm = [
        "For all p q : Prop, p → q → p.",
        "For all n : Nat, n = n.",
    ]
    texts = eng + math + fm
    out = [{"id": f"r{i+1}", "requirement_text": t} for i, t in enumerate(texts)]
    return out


def single_call_batch(base: str, items: List[Dict[str, Any]], timeout: float = 60.0) -> bool:
    resp = completion(
        model="certainly/bridge",
        custom_llm_provider="certainly",
        api_base=base,
        messages=[{"role": "system", "content": "Lean4 mixed batch"}],
        items=items,
        max_seconds=20,
        timeout=timeout,
    )
    content = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
    print("SINGLE_CONTENT_SNIPPET", (content or "")[:100])
    stats = (resp.get("additional_kwargs", {}) or {}).get("certainly", {}).get("statistics", {})
    p50 = stats.get("p50_item_ms"); p95 = stats.get("p95_item_ms")
    if p50 is not None or p95 is not None:
        print(f"ITEM_LATENCY p50_ms={p50} p95_ms={p95}")
    return bool(content)


async def parallel_chunks(base: str, items: List[Dict[str, Any]], chunk: int = 5, timeout: float = 60.0) -> bool:
    router = Router(model_list=[{"model_name":"certainly-bridge","litellm_params":{
        "model":"certainly/bridge","custom_llm_provider":"certainly","api_base":base
    }}])
    chunks = [items[i:i+chunk] for i in range(0, len(items), chunk)]
    reqs = [{"model":"certainly-bridge","messages":[{"role":"system","content":"Lean4 chunk"}],
             "kwargs":{"items": ch, "max_seconds": 20, "timeout": timeout}} for ch in chunks]
    res = await router.parallel_acompletions(requests=reqs)
    oks = 0
    for i, r in enumerate(res):
        try:
            c=(r.get("choices") or [{}])[0].get("message", {}).get("content", "")
            print(f"CHUNK_{i}_SNIPPET", (c or "")[:80])
            if c:
                oks += 1
        except Exception:
            pass
    print("PARALLEL_OK", oks, len(res))
    return oks == len(res) == len(chunks)


def main() -> int:
    base = (os.getenv("CERTAINLY_BRIDGE_BASE") or os.getenv("LEAN4_BRIDGE_BASE") or "http://127.0.0.1:8787").rstrip("/")
    items = _items()
    ok1 = single_call_batch(base, items)
    ok2 = True
    if os.getenv("MIXED_PARALLEL", "").lower() in {"1","true","yes"}:
        ok2 = asyncio.run(parallel_chunks(base, items))
    return 0 if (ok1 and ok2) else 31


if __name__ == "__main__":
    sys.exit(main())

