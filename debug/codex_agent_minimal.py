#!/usr/bin/env python3
"""
Minimal, dependency‑free codex‑agent calls (no CodeWorld, no LiteLLM).

Env:
  - CODEX_AGENT_API_BASE (default http://127.0.0.1:8089). No /v1 suffix.

Usage:
  python debug/codex_agent_minimal.py
"""
from __future__ import annotations

import json
import os
import sys
from urllib import request as rq


BASE = (os.getenv("CODEX_AGENT_API_BASE") or "http://127.0.0.1:8089").rstrip("/")


def _post(path: str, body: dict, timeout: float = 30.0) -> dict:
    url = BASE + path
    data = json.dumps(body).encode("utf-8")
    req = rq.Request(url=url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with rq.urlopen(req, timeout=timeout) as resp:
        status = int(getattr(resp, "status", 0) or 0)
        if status != 200:
            raise RuntimeError(f"HTTP {status} from {url}")
        return json.loads(resp.read().decode("utf-8", "ignore"))


def chat_ping(model: str = "gpt-5") -> str:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Say 'codex-agent chat ok' and stop."},
        ],
        "max_tokens": 64,
        "temperature": 0,
    }
    payload = _post("/v1/chat/completions", body)
    return (((payload.get("choices") or [{}])[0] or {}).get("message", {}) or {}).get("content", "")


def judge_json(model: str = "gpt-5") -> dict:
    user_payload = {
        "pair": "A vs B",
        "candidates": {
            "A": "Answer A: focuses on safety and clarity.",
            "B": "Answer B: adds speed optimizations but is less clear.",
        },
    }
    msgs = [
        {
            "role": "system",
            "content": "Return STRICT JSON only: {best_id:string, rationale_short:string}.",
        },
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]
    body = {
        "model": model,
        "messages": msgs,
        "temperature": 0,
        "max_tokens": 128,
        "response_format": {"type": "json_object"},
    }
    payload = _post("/v1/chat/completions", body, timeout=60.0)
    raw = (((payload.get("choices") or [{}])[0] or {}).get("message", {}) or {}).get("content", "")
    try:
        return json.loads(raw) if isinstance(raw, str) else {}
    except Exception:
        # Salvage first JSON object in the text if fences/leaks present
        import re
        m = re.search(r"\{.*\}", raw or "", re.S)
        return json.loads(m.group(0)) if m else {"best_id": None, "rationale_short": "parse_error"}


def main() -> None:
    print(f"BASE={BASE}")
    txt = chat_ping()
    print("CHAT_OK", bool(txt), "|", txt[:120].replace("\n", " "))
    res = judge_json()
    print("JUDGE_OK", bool(res and res.get("best_id")), "|", res)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERR:", type(e).__name__, str(e)[:200])
        sys.exit(1)

