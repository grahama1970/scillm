#!/usr/bin/env python3
from __future__ import annotations

import json, os, sys, time
from typing import Any, Dict

import httpx


def main() -> int:
    base = os.getenv("PROXY_BASE", "http://127.0.0.1:4001").rstrip("/")
    prom = os.getenv("PROM_BASE", "http://127.0.0.1:9090").rstrip("/")
    key = os.getenv("PROXY_KEY", os.getenv("LITELLM_MASTER_KEY", "sk-dev-proxy-123"))

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    url = f"{base}/v1/chat/completions"
    payload = {"model": os.getenv("SMOKE_MODEL", "text-auto"), "messages": [{"role": "user", "content": "Return only {\"ok\":true} as JSON."}], "max_tokens": 16, "temperature": 0}

    # Prom delta before
    q = "sum(increase(sc_cost_usd_total[5m]))"
    try:
        r = httpx.get(f"{prom}/api/v1/query", params={"query": q}, timeout=5.0)
        before = float((r.json()["data"]["result"][0]["value"][1]) if r.status_code == 200 and r.json().get("data", {}).get("result") else 0.0)
    except Exception:
        before = 0.0

    ok = True
    # Make call
    try:
        r = httpx.post(url, headers=headers, json=payload, timeout=30.0)
        code = r.status_code
        print(f"chat status={code}")
        if 200 <= code < 300:
            # Headers
            for h in ("x-ratelimit-limit", "x-ratelimit-remaining-requests", "x-budget-reset-at", "x-estimated-cost-usd"):
                present = h in r.headers
                print(f"header {h} present={present}")
                ok = ok and present
            # Body
            try:
                j = r.json()
            except Exception:
                print("body invalid json")
                return 31
            pricing = (((j.get("additional_kwargs") or {}).get("scillm") or {}).get("pricing") or {})
            est = pricing.get("estimated_cost_usd")
            print(f"meta estimated_cost_usd={est}")
            ok = ok and (isinstance(est, (int, float)) and est >= 0)
        else:
            # Accept 402/429 with proper type; skip cost checks
            try:
                j = r.json()
            except Exception:
                j = {}
            et = (j.get("error") or {}).get("type")
            print(f"error.type={et}")
            ok = ok and (et in {"budget_exhausted", "payg_confirmation_required"})
    except Exception as e:
        print(f"request error: {e}")
        ok = False

    # Prom delta after (allow 2s scrape)
    time.sleep(2)
    try:
        r2 = httpx.get(f"{prom}/api/v1/query", params={"query": q}, timeout=5.0)
        after = float((r2.json()["data"]["result"][0]["value"][1]) if r2.status_code == 200 and r2.json().get("data", {}).get("result") else 0.0)
    except Exception:
        after = before

    print(json.dumps({"ok": bool(ok), "prom_delta": max(0.0, after - before)}, indent=2))
    return 0 if ok else 32


if __name__ == "__main__":
    sys.exit(main())

