#!/usr/bin/env python3
"""
Provider warm‑up probe (Chutes / Runpod / OpenAI‑compatible)

Runs the provider warm‑up script once, then performs a tiny completion
to verify the model responds promptly. Prints a JSON summary with timing.

Env requirements (depending on provider):
  - Chutes: CHUTES_API_KEY, optional CHUTES_API_BASE (default https://api.chutes.ai/v1)
  - Runpod: RUNPOD_API_KEY, RUNPOD_API_BASE

Models are read from LITELLM_*_MODEL envs or passed via --model.

Usage:
  python scenarios/provider_warmup_probe.py --provider chutes
  python scenarios/provider_warmup_probe.py --provider runpod --model my-model
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Optional

from dotenv import find_dotenv, load_dotenv
import litellm

load_dotenv(find_dotenv())


def _env_model() -> Optional[str]:
    for k in (
        "LITELLM_DEFAULT_MODEL",
        "LITELLM_SMALL_TEXT_MODEL",
        "LITELLM_MED_TEXT_MODEL",
        "LITELLM_LARGE_TEXT_MODEL",
    ):
        v = os.getenv(k)
        if v:
            return v.strip().strip('"')
    return None


def _warmup(provider: str) -> int:
    if provider == "chutes":
        cmd = [sys.executable, "scripts/chutes_warmup.py"]
    else:
        cmd = [sys.executable, "scripts/provider_warmup.py", "--provider", provider]
    return subprocess.call(cmd)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True, choices=["chutes", "runpod"], help="Provider to warm and probe")
    ap.add_argument("--model", help="Explicit model to probe (falls back to LITELLM_*_MODEL if unset)")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    provider = args.provider.lower().strip()
    model = args.model or _env_model()
    if not model:
        print(json.dumps({"ok": False, "error": "No model provided; set --model or LITELLM_*_MODEL"}))
        sys.exit(0)

    # Determine API base/key
    if provider == "chutes":
        api_key = os.getenv("CHUTES_API_KEY")
        api_base = (os.getenv("CHUTES_API_BASE") or "https://api.chutes.ai/v1").rstrip("/")
    else:
        api_key = os.getenv("RUNPOD_API_KEY")
        api_base = (os.getenv("RUNPOD_API_BASE") or "").rstrip("/")

    if not api_key or not api_base:
        print(json.dumps({"ok": False, "provider": provider, "error": "missing provider credentials", "hint": "set API key/base env"}))
        sys.exit(0)

    # Warm up
    rc = _warmup(provider)
    if rc != 0:
        print(json.dumps({"ok": False, "provider": provider, "error": f"warmup script failed rc={rc}"}))
        sys.exit(1)

    # Probe completion
    start = time.time()
    try:
        resp = litellm.completion(
            model=model,
            messages=[{"role": "user", "content": "Say 'pong'."}],
            api_key=api_key,
            api_base=api_base,
            request_timeout=args.timeout,
            max_tokens=8,
            temperature=0,
        )
        ok = bool(getattr(resp, "choices", None))
        content = None
        try:
            content = resp.choices[0].message["content"]  # type: ignore[index]
        except Exception:
            try:
                content = resp.choices[0].message.content
            except Exception:
                content = None
        elapsed_ms = int((time.time() - start) * 1000)
        print(json.dumps({"ok": ok, "provider": provider, "model": model, "elapsed_ms": elapsed_ms, "content": content}))
    except Exception as e:
        elapsed_ms = int((time.time() - start) * 1000)
        print(json.dumps({"ok": False, "provider": provider, "model": model, "elapsed_ms": elapsed_ms, "error": str(e)[:200]}))


if __name__ == "__main__":
    main()

