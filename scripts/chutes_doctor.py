#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from typing import Any, Dict

from scillm.extras.chutes_simple import chutes_chat_json


def main() -> None:
    base = os.environ.get("CHUTES_API_BASE", "").strip()
    key = os.environ.get("CHUTES_API_KEY", "").strip()
    model = os.environ.get("CHUTES_TEXT_MODEL", "").strip()
    if not base or not key or not model:
        print(json.dumps({
            "ok": False,
            "error": "Missing CHUTES_API_BASE/CHUTES_API_KEY/CHUTES_TEXT_MODEL",
        }))
        return
    try:
        resp = chutes_chat_json(messages=[{"role": "user", "content": 'Return only {"ok":true} as JSON.'}], model=model, max_tokens=16, temperature=0)
        content = resp.choices[0].message.get("content", "")
        served = getattr(resp, "scillm_meta", {}).get("served_model") if hasattr(resp, "scillm_meta") else None
        print(json.dumps({
            "ok": True,
            "served_model": served,
            "content_head": (content or "")[:200],
        }))
    except Exception as e:
        print(json.dumps({"ok": False, "error": type(e).__name__, "msg": str(e)}))


if __name__ == "__main__":
    main()

