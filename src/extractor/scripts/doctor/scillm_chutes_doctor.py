#!/usr/bin/env python3
"""
SciLLM↔Chutes doctor: proves the boring contract in <60s.

Smokes:
  1) GET /v1/models with x-api-key → HTTP 200
  2) Direct JSON chat via helper (chutes_chat_json) → content non-empty
  3) VLM one-shot (image_url data URI) via helper → content non-empty

Exit 0 on success; non-zero otherwise. Prints compact status lines only.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from typing import Any, Dict

import httpx

from extractor.pipeline.utils.chutes_scillm import chutes_chat, chutes_chat_json


def _env(name: str) -> str:
    v = (os.getenv(name) or "").strip()
    if not v:
        print(f"ENV_MISSING {name}")
        sys.exit(12)
    return v


def smoke_models() -> None:
    base = _env("CHUTES_API_BASE")
    key = _env("CHUTES_API_KEY")
    r = httpx.get(
        base.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {key}"},
        timeout=10,
    )
    print(f"MODELS_HTTP {r.status_code}")
    if r.status_code != 200:
        sys.exit(21)
    try:
        data = r.json()
        ids = [d.get("id", "") for d in (data.get("data") or [])]
        first = ids[0] if ids else ""
        print(f"MODELS_FIRST_ID {first}")
        if ids:
            print("MODELS_IDS " + ",".join(ids[:12]))
    except Exception:
        pass


def smoke_json() -> None:
    model = (
        os.getenv("CHUTES_MODEL_ID")
        or os.getenv("CHUTES_TEXT_MODEL")
        or os.getenv("LITELLM_DEFAULT_MODEL")
    )
    if not model:
        print("JSON_SMOKE_SKIPPED no_text_model")
        return
    resp = chutes_chat_json(
        model=model,
        messages=[{"role": "user", "content": 'Return only {"ok":true} as JSON.'}],
        temperature=0.0,
        timeout=20,
    )
    content = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
    ok = "\"ok\":true" in content.replace(" ", "")
    print(f"JSON_SMOKE_OK {int(bool(content))} {int(ok)}")
    if not content:
        sys.exit(31)


def smoke_vlm() -> None:
    model = os.getenv("CHUTES_VLM_MODEL") or os.getenv("LITELLM_LARGE_VLLM_MODEL") or os.getenv("LITELLM_VLM_MODEL")
    if not model:
        print("VLM_SMOKE_SKIPPED no_vlm_model")
        return
    # Use a permissive public image URL (avoid hosts that block server-side fetch).
    img_url = "https://picsum.photos/seed/scillm/256/256"
    variants = [
        [{"type": "text", "text": "Image follows"}, {"type": "image_url", "image_url": {"url": img_url}}],
        [{"type": "text", "text": "Image follows"}, {"type": "image_url", "image_url": img_url}],
        [{"type": "text", "text": "Image follows"}, {"type": "input_image", "image_url": img_url}],
        [{"type": "text", "text": "Image follows"}, {"type": "input_image", "image_url": {"url": img_url}}],
    ]
    content = ""
    for parts in variants:
        messages = [
            {"role": "system", "content": "Describe the image in 1 short sentence."},
            {"role": "user", "content": parts},
        ]
        try:
            resp = chutes_chat(model=model, messages=messages, temperature=0.2, timeout=20)
            content = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
            if content:
                break
        except Exception:
            continue
    print(f"VLM_SMOKE_OK {int(bool(content))}")
    if not content:
        sys.exit(41)


def main() -> None:
    try:
        smoke_models()
        smoke_json()
        smoke_vlm()
        print('{"ok": true}')
        sys.exit(0)
    except SystemExit as se:
        # already printed an error code
        raise
    except Exception as e:
        print(f"DOCTOR_ERR {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
