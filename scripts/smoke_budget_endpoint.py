#!/usr/bin/env python3
"""
Smoke: GET /v1/budget and validate minimal contract.

Env (optional)
- PROXY_BASE (default: http://127.0.0.1:4010)
"""
import json
import os
import sys
import urllib.request
import urllib.error


def _get(url: str, timeout: float = 10.0):
    req = urllib.request.Request(url)
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
    base = os.getenv("PROXY_BASE", "http://127.0.0.1:4010").rstrip("/")
    s, body, _ = _get(f"{base}/v1/budget")
    ok = False
    detail = {}
    try:
        data = json.loads(body)
        limit = data.get("limit")
        remaining = data.get("remaining")
        reset_at = data.get("reset_at")
        price = data.get("price_per_call_usd", 0.0)
        ok = (
            s == 200
            and isinstance(limit, int)
            and isinstance(remaining, int)
            and isinstance(reset_at, str)
            and isinstance(price, (int, float))
        )
        detail = {
            "limit": limit,
            "remaining": remaining,
            "reset_at": reset_at,
            "price_per_call_usd": price,
        }
    except Exception:
        detail = {"parse_error": body[:240]}

    print(json.dumps({"ok": ok, "status": s, "detail": detail}))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())

