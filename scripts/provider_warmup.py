#!/usr/bin/env python3
"""
Generic warmup for OpenAI-compatible providers (e.g., Chutes.ai, Runpod).

Why: Many providers cold-start models/containers; a minimal completion primes latency.

Supported providers (by env):
  - chutes:  API base/key from CHUTES_API_BASE / CHUTES_API_KEY (default base https://api.chutes.ai/v1)
  - runpod:  API base/key from RUNPOD_API_BASE / RUNPOD_API_KEY

Models: discovered from common LITELLM_*_MODEL envs or passed via --models.

Usage examples:
  python scripts/provider_warmup.py --provider chutes
  python scripts/provider_warmup.py --provider runpod --models my-model-1,my-model-2
  python scripts/provider_warmup.py --provider chutes --dry-run

Notes:
  - Minimal tokens (max_tokens=8) and temperature=0.
  - Skip-friendly: if API key is missing, exits 0 (prints a note).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import time
from typing import Dict, Iterable, List, Tuple

from dotenv import find_dotenv, load_dotenv
import litellm

load_dotenv(find_dotenv())


COMMON_ENV_SLOTS = (
    "LITELLM_DEFAULT_MODEL",
    "LITELLM_SMALL_VLM_MODEL",
    "LITELLM_MED_VLM_MODEL",
    "LITELLM_LARGE_VLLM_MODEL",
    "LITELLM_SMALL_TEXT_MODEL",
    "LITELLM_MED_TEXT_MODEL",
    "LITELLM_LARGE_TEXT_MODEL",
)


def _env_models() -> List[str]:
    vals: List[str] = []
    for k in COMMON_ENV_SLOTS:
        v = os.getenv(k)
        if v:
            vals.append(v.strip().strip('"'))
    # dedupe, preserve order
    return list(dict.fromkeys(vals))


def _provider_conf(name: str) -> Tuple[str | None, str | None]:
    n = name.lower().strip()
    if n == "chutes":
        return (
            (os.getenv("CHUTES_API_BASE") or "https://api.chutes.ai/v1").rstrip("/"),
            os.getenv("CHUTES_API_KEY"),
        )
    if n == "runpod":
        return (
            (os.getenv("RUNPOD_API_BASE") or "").rstrip("/") or None,
            os.getenv("RUNPOD_API_KEY"),
        )
    # default: allow explicit --api-base/--api-key only
    return None, None


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


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--provider", required=True, help="Provider name: chutes | runpod | other")
    p.add_argument("--api-base", help="Override API base; defaults come from provider env")
    p.add_argument("--api-key", help="Override API key; defaults come from provider env")
    p.add_argument("--models", help="Comma-separated model list; falls back to LITELLM_*_MODEL envs")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    api_base_env, api_key_env = _provider_conf(args.provider)
    api_base = (args.api_base or api_base_env or "").rstrip("/")
    api_key = args.api_key or api_key_env or ""

    if not api_key:
        print(f"provider-warmup[{args.provider}]: no API key present; skipping (exit 0)")
        return
    if not api_base:
        print(f"provider-warmup[{args.provider}]: no API base present; skipping (exit 0)")
        return

    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    else:
        models = _env_models()
    unique = list(dict.fromkeys(models))

    print(f"provider-warmup[{args.provider}]: plan")
    for m in unique:
        print(f"  {m}")
    if args.dry_run:
        return

    res = await asyncio.gather(*[_warm_one(m, api_key, api_base) for m in unique])
    print(f"\nprovider-warmup[{args.provider}]: results")
    for m, ok, dt, msg in res:
        s = "OK" if ok else "FAIL"
        print(f"  {m:60s} {s:4s} {dt*1000:6.1f} ms  {msg}")


if __name__ == "__main__":
    asyncio.run(main())

