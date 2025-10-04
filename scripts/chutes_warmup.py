#!/usr/bin/env python3
"""
Chutes.ai warmup utility for SciLLM

Priming common models avoids cold‑start latency on first live request.

Reads from environment:
  - CHUTES_API_KEY  (required)
  - CHUTES_API_BASE (default: https://api.chutes.ai/v1)
  - LITELLM_*_MODEL (optional; scans common names to build a deduped warmup set)

Usage:
  python scripts/chutes_warmup.py                # warm up deduped models from env
  python scripts/chutes_warmup.py --dry-run      # print plan only
  python scripts/chutes_warmup.py --test DEFAULT # tiny completion after warmup

Notes:
  - Requests are minimal (max_tokens=8) and temperature=0.
  - This script is skip‑friendly: if CHUTES_API_KEY is missing it exits 0.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import time
from typing import Dict, List, Tuple

from dotenv import find_dotenv, load_dotenv
import litellm

load_dotenv(find_dotenv())


def _env_models() -> Dict[str, str]:
    names = [
        "LITELLM_DEFAULT_MODEL",
        "LITELLM_SMALL_VLM_MODEL",
        "LITELLM_MED_VLM_MODEL",
        "LITELLM_LARGE_VLLM_MODEL",
        "LITELLM_SMALL_TEXT_MODEL",
        "LITELLM_MED_TEXT_MODEL",
        "LITELLM_LARGE_TEXT_MODEL",
    ]
    out: Dict[str, str] = {}
    for k in names:
        v = os.getenv(k)
        if v:
            out[k] = v.strip().strip('"')
    return out


async def _warm_one(model: str, api_key: str, api_base: str, timeout_s: float = 30.0) -> Tuple[str, bool, float, str]:
    t0 = time.time()
    try:
        resp = await litellm.acompletion(
            model=model,
            api_key=api_key,
            api_base=api_base,
            messages=[{"role": "system", "content": "ping"}],
            request_timeout=timeout_s,
            max_tokens=8,
            temperature=0,
        )
        dt = time.time() - t0
        ok = bool(getattr(resp, "choices", None))
        return model, ok, dt, "ok"
    except Exception as e:  # noqa: BLE001
        dt = time.time() - t0
        return model, False, dt, f"error: {type(e).__name__}: {e}"


async def _test_one(model: str, api_key: str, api_base: str, timeout_s: float = 30.0) -> str:
    resp = await litellm.acompletion(
        model=model,
        api_key=api_key,
        api_base=api_base,
        messages=[{"role": "user", "content": "Reply 'pong' only."}],
        request_timeout=timeout_s,
        max_tokens=16,
        temperature=0,
    )
    try:
        return resp.choices[0].message.content or ""
    except Exception:
        return ""


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--test", choices=[
        "DEFAULT", "SMALL_VLM", "MED_VLM", "LARGE", "SMALL", "MED", "LARGE_TEXT"
    ])
    args = p.parse_args()

    api_key = os.getenv("CHUTES_API_KEY")
    api_base = (os.getenv("CHUTES_API_BASE") or "https://api.chutes.ai/v1").rstrip("/")
    if not api_key:
        print("chutes-warmup: CHUTES_API_KEY not set; skipping (exit 0).")
        return

    models = _env_models()
    unique: List[str] = list(dict.fromkeys(models.values()))

    print("chutes-warmup: plan")
    for k, v in models.items():
        print(f"  {k}: {v}")
    if args.dry_run:
        return

    res = await asyncio.gather(*[_warm_one(m, api_key, api_base) for m in unique])
    print("\nchutes-warmup: results")
    for m, ok, dt, msg in res:
        s = "OK" if ok else "FAIL"
        print(f"  {m:60s} {s:4s} {dt*1000:6.1f} ms  {msg}")

    if args.test:
        slot_map = {
            "DEFAULT": "LITELLM_DEFAULT_MODEL",
            "SMALL_VLM": "LITELLM_SMALL_VLM_MODEL",
            "MED_VLM": "LITELLM_MED_VLM_MODEL",
            "LARGE": "LITELLM_LARGE_VLLM_MODEL",
            "SMALL": "LITELLM_SMALL_TEXT_MODEL",
            "MED": "LITELLM_MED_TEXT_MODEL",
            "LARGE_TEXT": "LITELLM_LARGE_TEXT_MODEL",
        }
        slot = slot_map[args.test]
        mdl = models.get(slot)
        if mdl:
            out = await _test_one(mdl, api_key, api_base)
            print(f"\nchutes-warmup: test {args.test} → {mdl}: {out}")
        else:
            print(f"chutes-warmup: slot {args.test} not set; skipping test")


if __name__ == "__main__":
    asyncio.run(main())

