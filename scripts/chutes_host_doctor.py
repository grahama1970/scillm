#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, List

import httpx

try:
    # Prefer the built-in LiteLLM cleaner for fenced/prose JSON
    from scillm.extras import clean_json_string as _clean_json
except Exception:  # pragma: no cover
    _clean_json = None  # type: ignore


def _normalize_json_text(s: str | None) -> str:
    if not s:
        return ""
    if _clean_json is not None:
        try:
            return _clean_json(s) or ""
        except Exception:
            pass
    # Fallback: minimal fence strip if the helper is unavailable
    t = s.strip()
    if t.startswith("```") and t.endswith("```"):
        t = "\n".join(t.splitlines()[1:-1]).strip()
    return t


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Poll a Chutes host until ready, then run JSON chat + small batch")
    ap.add_argument("--slug", default=os.getenv("SLUG"), help="Per-host chute slug, e.g., graham-anderson-unsloth-gemma-3-4b-it")
    ap.add_argument("--model", default=os.getenv("CHUTES_MODEL", "unsloth/gemma-3-4b-it"))
    ap.add_argument("--timeout", type=int, default=int(os.getenv("CHUTES_HOST_POLL_TIMEOUT", "480")))
    ap.add_argument("--interval", type=int, default=int(os.getenv("CHUTES_HOST_POLL_INTERVAL", "12")))
    ap.add_argument("--concurrency", type=int, default=2)
    return ap.parse_args()


def require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        print(f"error: env {name} missing", file=sys.stderr)
        sys.exit(2)
    return v


def poll_models(base: str, key: str, timeout_s: int, interval_s: int) -> Dict[str, Any]:
    url = f"{base.rstrip('/')}/models"
    t0 = time.monotonic()
    last_status = None
    with httpx.Client(timeout=20.0) as client:
        while True:
            try:
                r = client.get(url, headers={"Authorization": f"Bearer {key}"})
                last_status = r.status_code
                if r.status_code == 200:
                    return {"ok": True, "elapsed_sec": time.monotonic() - t0, "body": r.json()}
            except Exception as e:
                last_status = str(e)
            if time.monotonic() - t0 >= timeout_s:
                return {"ok": False, "elapsed_sec": time.monotonic() - t0, "status": last_status}
            time.sleep(interval_s)


async def run_batch(base: str, key: str, model: str, concurrency: int) -> Dict[str, Any]:
    # Import here to avoid imposing scillm dependency for mere polling
    from scillm import Router

    r = Router(model_list=[{"model_name": "host", "litellm_params": {"model": model, "api_base": base, "api_key": key, "custom_llm_provider": "openai_like"}}])
    prompts = [
        'Respond with JSON only. Keys: {"hello": string}. Say hello.',
        'Respond with JSON only. Keys: {"sum": number}. Compute 5+6.',
    ]
    reqs: List[Dict[str, Any]] = [
        {"model": model, "messages": [{"role": "user", "content": p}], "response_format": {"type": "json_object"}, "temperature": 0, "max_tokens": 16}
        for p in prompts
    ]
    res = await r.parallel_acompletions(requests=reqs, concurrency=concurrency, return_exceptions=True)
    ok = 0
    sample: List[str] = []
    for item in res:
        if isinstance(item, Exception):
            continue
    c = item.get("choices", [{}])[0].get("message", {}).get("content")
    c = _normalize_json_text(c)
        if c:
            ok += 1
            if len(sample) < 2:
                sample.append(c)
    return {"ok": ok, "total": len(res), "sample": sample}


def run_single(base: str, key: str, model: str) -> Dict[str, Any]:
    from scillm import completion

    out = completion(
        model=model,
        api_base=base,
        api_key=key,
        custom_llm_provider="openai_like",
        messages=[{"role": "user", "content": 'Return only {"ok":true} as JSON.'}],
        response_format={"type": "json_object"},
        temperature=0,
        max_tokens=16,
    )
    content = out.choices[0].message.get("content", "")
    return {"content": _normalize_json_text(content), "model": out.model}


def main() -> int:
    args = parse_args()
    key = require_env("CHUTES_API_KEY")
    if not args.slug:
        print("error: --slug or SLUG env is required", file=sys.stderr)
        return 2
    base_host = f"https://{args.slug}.chutes.ai"
    chat_base = f"{base_host}/v1"

    # 1) Poll readiness
    ready = poll_models(chat_base, key, timeout_s=args.timeout, interval_s=args.interval)
    summary: Dict[str, Any] = {"host": base_host, "chat_base": chat_base, "model": args.model, "readiness": ready}
    if not ready.get("ok"):
        print(json.dumps({"overall_ok": False, **summary}))
        return 1

    # 2) Single JSON chat
    one = run_single(chat_base, key, args.model)
    summary["single"] = one

    # 3) Batch
    batch = asyncio.run(run_batch(chat_base, key, args.model, args.concurrency))
    summary["batch"] = batch

    overall_ok = bool(one.get("content")) and batch.get("ok") == batch.get("total") and batch.get("total", 0) > 0
    print(json.dumps({"overall_ok": overall_ok, **summary}))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
