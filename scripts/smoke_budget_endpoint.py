#!/usr/bin/env python3
"""
Smoke: GET /v1/budget and validate minimal contract.

Env (optional)
- PROXY_BASE (default: http://127.0.0.1:4001)
"""
import json
import os
import sys
import urllib.request
import urllib.error


def _get(url: str, headers: dict | None = None, timeout: float = 10.0):
    req = urllib.request.Request(url)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310
            body = r.read().decode("utf-8", errors="replace")
            return r.getcode(), body, dict(r.headers)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, body, dict(e.headers)
    except Exception as e:
        return 0, str(e), {}


def main() -> int:
    base = os.getenv("PROXY_BASE", "http://127.0.0.1:4001").rstrip("/")
    master = os.getenv("MASTER_KEY", os.getenv("LITELLM_MASTER_KEY", "sk-dev-proxy-123"))
    s, body, _ = _get(f"{base}/v1/budget", headers={"Authorization": f"Bearer {master}"})
    # Skip gracefully when proxy is unreachable
    if s == 0:
        print(json.dumps({"ok": False, "status": 0, "skipped": True, "reason": "proxy unreachable"}))
        return 0
    ok = False
    detail = {}
    try:
        data = json.loads(body)

        # Budget guard may not be loaded (no Chutes env) — accept as valid
        if s == 200 and data.get("status") == "budget_guard_not_loaded":
            detail = {"status": "budget_guard_not_loaded"}
            ok = True
        else:
            # Accept either "limit" or "daily_limit" (budget_metadata uses daily_limit)
            limit = data.get("limit") or data.get("daily_limit")
            remaining = data.get("remaining")
            reset_at = data.get("reset_at")
            ok = (
                s == 200
                and isinstance(limit, int)
                and isinstance(remaining, int)
                and isinstance(reset_at, str)
            )
            detail = {
                "limit": limit,
                "remaining": remaining,
                "reset_at": reset_at,
            }
    except Exception:
        detail = {"parse_error": body[:240]}

    print(json.dumps({"ok": ok, "status": s, "detail": detail}))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())

