#!/usr/bin/env python3
from __future__ import annotations

"""
Minimal Chutes batch demo (one call, simple output)

Purpose
- Prove we can build a small inference batch and get results via SciLLM
  using Router.parallel_acompletions. No autoscale or advanced flags.

Requirements
- SCILLM_ENABLE_CHUTES_AUTOSTART=1 (opt‑in provider)
- CHUTES_API_KEY set; Chutes environment configured

Run
  PYTHONPATH=src:. \
  SCILLM_ENABLE_CHUTES_AUTOSTART=1 \
  python debug/chutes_parallel_batch_min.py \
    --chute-name mistral_chute_test \
    --model microsoft/DialoGPT-medium \
    --n 5 --concurrency 3
"""

import argparse
import asyncio
import json
import os
from typing import Any, Dict, List, TypedDict, Union

from scillm import Router
try:
    from scillm.extras import clean_json_string as _clean_json
except Exception:  # pragma: no cover
    _clean_json = None


def _messages(prompt: str) -> List[Dict[str, Any]]:
    return [{"role": "user", "content": prompt}]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chute-name", default=os.getenv("CHUTES_CHUTE_NAME", "mistral_chute_test"))
    ap.add_argument("--model", default=os.getenv("CHUTES_MODEL", "microsoft/DialoGPT-medium"))
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--ephemeral-end", action="store_true")
    ap.add_argument("--max-tokens", type=int, default=int(os.getenv("CHUTES_MAX_TOKENS", "64")))
    return ap.parse_args()


class BatchResult(TypedDict):
    results: List[Union[Dict[str, Any], BaseException]]
    ok: int
    total: int
    concurrency: int
    warmup_seconds: float


async def chutes_parallel_batch(
    chute_name: str,
    model: str,
    n: int = 5,
    concurrency: int = 3,
    max_tokens: int = 64,
    ephemeral_end: bool = False,
) -> BatchResult:
    os.environ.setdefault("SCILLM_ENABLE_CHUTES_AUTOSTART", "1")
    model_id = f"chutes:{chute_name}/{model}"

    # Ensure chute is ready and capture warmup
    try:
        from scillm.extras.chutes import aensure as _aensure
        ch = await _aensure(chute_name, ttl_sec=600)
        warmup_seconds = float(getattr(ch, "warmup_seconds", 0.0) or 0.0)
    except Exception:
        warmup_seconds = 0.0

    router = Router(
        model_list=[{
            "model_name": "chute",
            "litellm_params": {
                "model": model_id,
                "custom_llm_provider": "chutes",
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "max_tokens": max_tokens,
            },
        }]
    )

    base_prompts = [
        'Respond with JSON only. Keys: {"message": string}. Say hello.',
        'Respond with JSON only. Keys: {"sum": number}. Compute 12+8.',
        'Respond with JSON only. Keys: {"palindrome": boolean}. Is "level" a palindrome?',
        'Respond with JSON only. Keys: {"upper": string}. Uppercase "scillm".',
        'Respond with JSON only. Keys: {"min": number}. Minimum of [9,4,7].',
    ]
    prompts = (base_prompts * ((n + len(base_prompts) - 1) // len(base_prompts)))[: n]
    requests: List[Dict[str, Any]] = [{"model": model_id, "messages": _messages(p)} for p in prompts]

    results = await router.parallel_acompletions(requests=requests, concurrency=concurrency, return_exceptions=True)

    ok = 0
    for r in results:
        if isinstance(r, BaseException):
            continue
        content = r.get("choices", [{}])[0].get("message", {}).get("content")
        if _clean_json is not None:
            try:
                content = _clean_json(content)
            except Exception:
                pass
        if content:
            ok += 1

    if ephemeral_end:
        try:
            from scillm.extras.chutes import close as close_chute
            close_chute(chute_name)
        except Exception:
            pass

    return BatchResult(results=results, ok=ok, total=len(results), concurrency=concurrency, warmup_seconds=warmup_seconds)


async def main() -> int:
    args = parse_args()
    br = await chutes_parallel_batch(
        chute_name=args.chute_name,
        model=args.model,
        n=args.n,
        concurrency=args.concurrency,
        max_tokens=args.max_tokens,
        ephemeral_end=args.ephemeral_end,
    )
    # Print only a tiny summary; the function returns the complete result object.
    print(json.dumps({"ok": br["ok"], "total": br["total"], "concurrency": br["concurrency"]}))
    return 0 if br["ok"] == br["total"] and br["total"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
