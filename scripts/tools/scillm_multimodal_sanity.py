#!/usr/bin/env python3
from __future__ import annotations

"""
SciLLM multimodal sanity: verifies VLM with both curl and scillm.

Usage:
  # Activate env and load .env (expects CHUTES_API_BASE, CHUTES_API_KEY, CHUTES_VLM_MODEL)
  source .venv/bin/activate
  set -a; [ -f .env ] && source .env; set +a
  PYTHONPATH=$(pwd)/src python scripts/tools/scillm_multimodal_sanity.py --run-curl

Flags:
  --image-url URL   Publicly-fetchable image URL (default: picsum)
  --model ID        VLM model id (default: $CHUTES_VLM_MODEL)
  --run-curl        Execute the curl probe (otherwise prints only)
  --timeout SEC     Request timeout (default: 30)
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
from typing import Any, Dict, List

import httpx


def _env(name: str) -> str:
    v = (os.getenv(name) or "").strip()
    if not v:
        print(f"ENV_MISSING {name}")
        sys.exit(12)
    return v


def build_curl(image_url: str, model: str, timeout: float) -> str:
    base = _env("CHUTES_API_BASE").rstrip("/")
    key = _env("CHUTES_API_KEY")
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image in one short sentence."},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ],
        "stream": False,
        "max_tokens": 128,
        "temperature": 0.2,
    }
    data = json.dumps(body, separators=(",", ":"))
    cmd = (
        f"curl -sS -X POST {shlex.quote(base)}/chat/completions "
        f"-H 'Authorization: Bearer {shlex.quote(key)}' "
        f"-H 'Content-Type: application/json' "
        f"--data {shlex.quote(data)}"
    )
    return cmd


def run_scillm(image_url: str, model: str, timeout: float) -> Dict[str, Any]:
    # Defer import so this script can run outside tests without full deps
    from extractor.pipeline.utils.chutes_scillm import chutes_chat

    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": [
            {"type": "text", "text": "Describe this image in one short sentence."},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]}
    ]
    resp = chutes_chat(model=model, messages=messages, timeout=timeout, max_tokens=128, temperature=0.2)
    return resp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image-url", default="https://picsum.photos/seed/scillm/256/256")
    ap.add_argument("--model", default=os.getenv("CHUTES_VLM_MODEL", ""))
    ap.add_argument("--run-curl", action="store_true")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    if not args.model:
        print("MODEL_MISSING set --model or CHUTES_VLM_MODEL")
        sys.exit(2)

    # 1) Print (and optionally run) curl sanity
    cmd = build_curl(args.image_url, args.model, args.timeout)
    print("CURL_CMD", cmd)
    if args.run_curl:
        try:
            r = subprocess.run(cmd, shell=True, check=False, capture_output=True, text=True)
            print("CURL_HTTP", r.returncode)
            if r.stdout:
                try:
                    j = json.loads(r.stdout)
                    content = (j.get("choices") or [{}])[0].get("message", {}).get("content", "")
                    print("CURL_CONTENT_NONEMPTY", int(bool(content)))
                except Exception:
                    print("CURL_RAW", r.stdout[:400])
            if r.stderr:
                print("CURL_ERR", r.stderr.strip()[:300])
        except Exception as e:
            print("CURL_EXCEPTION", e)

    # 2) scillm chutes helper sanity
    try:
        j = run_scillm(args.image_url, args.model, args.timeout)
        content = (j.get("choices") or [{}])[0].get("message", {}).get("content", "")
        print("SCILLM_CONTENT_NONEMPTY", int(bool(content)))
        if not content:
            print(json.dumps(j, ensure_ascii=False))
            sys.exit(41)
    except httpx.HTTPStatusError as he:
        print("SCILLM_HTTP_STATUS", getattr(he.response, "status_code", None))
        print("SCILLM_HTTP_BODY", (getattr(he.response, "text", "") or "")[:300])
        sys.exit(42)
    except Exception as e:
        print("SCILLM_EXCEPTION", e)
        sys.exit(43)

    print('{"ok": true}')


if __name__ == "__main__":
    main()

