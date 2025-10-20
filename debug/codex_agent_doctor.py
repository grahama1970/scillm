#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from urllib import request as rq


def _print(k, v):
    print(f"{k}: {v}")


def _get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    try:
        with rq.urlopen(url, timeout=timeout) as resp:
            return int(getattr(resp, "status", 0) or 0), resp.read().decode("utf-8", "ignore")
    except Exception as e:
        return 0, str(e)


def _post(url: str, body: dict, timeout: float = 30.0) -> tuple[int, dict|str]:
    try:
        data = json.dumps(body).encode("utf-8")
        req = rq.Request(url=url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with rq.urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 0) or 0)
            txt = resp.read().decode("utf-8", "ignore")
            try:
                return status, json.loads(txt)
            except Exception:
                return status, txt
    except Exception as e:
        return 0, str(e)


def main() -> int:
    base = (os.getenv("CODEX_AGENT_API_BASE") or "http://127.0.0.1:8089").rstrip("/")
    _print("base", base)

    # 1) healthz
    s, body = _get(base + "/healthz")
    _print("health.status", s)
    _print("health.body", body)
    if s != 200:
        print("hint: start sidecar: docker compose -f local/docker/compose.agents.yml up -d codex-sidecar")
        return 2

    # 2) chat ping
    s, body = _post(base + "/v1/chat/completions", {
        "model": "gpt-5",
        "messages": [{"role":"user","content":"Say 'doctor ok' and stop."}],
        "max_tokens": 16,
        "temperature": 0
    })
    _print("chat.status", s)
    _print("chat.choice.preview", (json.dumps(body)[0:120] if isinstance(body, dict) else str(body)[0:120]))
    if s != 200:
        print("hint: verify ~/.codex/auth.json is mounted; check container logs for codex sidecar")
        return 3

    # 3) judge strict JSON
    s, body = _post(base + "/v1/chat/completions", {
        "model": "gpt-5",
        "messages": [
            {"role":"system","content":"Return STRICT JSON only: {best_id:string, rationale_short:string}."},
            {"role":"user","content": json.dumps({"A":"clear","B":"speedy"})}
        ],
        "response_format": {"type":"json_object"},
        "max_tokens": 128,
        "temperature": 1
    }, timeout=60.0)
    _print("judge.status", s)
    _print("judge.body", json.dumps(body) if isinstance(body, dict) else str(body))
    if s != 200:
        return 4

    print("doctor: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())

