#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

import httpx


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def main() -> int:
    p = argparse.ArgumentParser(description="codex-agent doctor: health, models, ping")
    p.add_argument("--base", default=os.getenv("CODEX_AGENT_API_BASE", "http://127.0.0.1:8788"), help="OpenAI-compatible base URL (no /v1)")
    p.add_argument("--model", default=os.getenv("CODEX_AGENT_MODEL", ""), help="Model id (if empty, fetch first from /v1/models)")
    p.add_argument("--timeout", type=float, default=8.0, help="Timeout seconds per call")
    args = p.parse_args()

    base = args.base.rstrip("/")
    headers = {"Content-Type": "application/json"}
    api_key = os.getenv("CODEX_AGENT_API_KEY", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    ok = True
    summary: Dict[str, Any] = {"base": base, "steps": []}

    try:
        with httpx.Client(timeout=args.timeout, headers=headers) as c:
            # health
            h = c.get(f"{base}/healthz")
            summary["steps"].append({"healthz": h.status_code, "body": _safe_json(h)})
            if h.status_code >= 400:
                ok = False

            # models
            m = c.get(f"{base}/v1/models")
            body = _safe_json(m)
            ids: List[str] = []
            if isinstance(body, dict) and isinstance(body.get("data"), list):
                ids = [str(x.get("id")) for x in body["data"] if isinstance(x, dict) and x.get("id")]
            summary["steps"].append({"models": m.status_code, "ids": ids})
            if m.status_code >= 400 or not ids:
                ok = False

            model = args.model or (ids[0] if ids else "")
            summary["model"] = model
            if not model:
                ok = False
            else:
                # quick ping with high reasoning
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": "Say hello."}],
                    "reasoning": {"effort": "high"},
                }
                r = c.post(f"{base}/v1/chat/completions", json=payload)
                rb = _safe_json(r)
                out = None
                try:
                    out = rb["choices"][0]["message"]["content"]
                except Exception:
                    out = str(rb)[:200]
                summary["steps"].append({"chat": r.status_code, "sample": out})
                if r.status_code >= 400:
                    ok = False
    except httpx.HTTPError as e:
        eprint("HTTP error:", e)
        ok = False
    except Exception as e:
        eprint("unexpected error:", e)
        ok = False

    print(json.dumps(summary, indent=2))
    return 0 if ok else 2


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return {"text": resp.text[:200]}


if __name__ == "__main__":
    sys.exit(main())

