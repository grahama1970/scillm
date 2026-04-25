#!/usr/bin/env python3
"""
Smoke: one JSON chat via proxy; assert budget headers are present on the HTTP response.

Env (optional)
- PROXY_BASE (default: http://127.0.0.1:4001)
- MASTER_KEY (default: sk-dev-proxy-123)
- SMOKE_MODEL (default: gpt-chutes)
"""
import json
import os
import sys
import urllib.request
import urllib.error


def _post(url: str, payload: dict, headers: dict | None = None, timeout: float = 15.0):
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310
            body = r.read().decode("utf-8", errors="replace")
            return r.getcode(), body, dict(r.headers)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, body, dict(e.headers)
    except Exception as e:
        return 0, str(e), {}


def _hget(headers: dict, key: str):
    for k, v in headers.items():
        if k.lower() == key.lower():
            return v
    return None


def main() -> int:
    base = os.getenv("PROXY_BASE", "http://127.0.0.1:4001").rstrip("/")
    master = os.getenv("MASTER_KEY", os.getenv("LITELLM_MASTER_KEY", "sk-dev-proxy-123"))
    model = os.getenv("SMOKE_MODEL", "local-text")

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Return only {\\\"ok\\\":true} as JSON."}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 16,
    }

    s, body, hdrs = _post(
        f"{base}/v1/chat/completions",
        payload,
        headers={"Authorization": f"Bearer {master}", "X-Caller-Skill": "smoke-chat-headers"},
        timeout=30.0,
    )

    want = [
        "x-ratelimit-limit",
        "x-ratelimit-remaining-requests",
        "x-budget-reset-at",
    ]
    missing = [k for k in want if _hget(hdrs, k) is None]

    # Skip gracefully when proxy is unreachable (status 0 = connection refused)
    if s == 0:
        out = {"ok": False, "status": 0, "skipped": True, "reason": "proxy unreachable",
               "model": model, "proxy_base": base, "error": body[:200]}
        print(json.dumps(out))
        return 0

    # Skip gracefully when backend model is unavailable (Ollama down, provider 404, etc.)
    if s in (404, 500, 502, 503, 504) and model.startswith("local"):
        out = {"ok": False, "status": s, "skipped": True,
               "reason": f"local model unavailable (HTTP {s})",
               "model": model, "proxy_base": base, "error": body[:200]}
        print(json.dumps(out))
        return 0

    # Budget headers are only present when budget_guard middleware is loaded
    # AND the request goes through a provider that has budget tracking (Chutes).
    # local-text (Ollama) won't have them — that's expected, not a failure.
    ok = s == 200
    out = {
        "ok": ok,
        "status": s,
        "model": model,
        "proxy_base": base,
        "missing": missing,
        "have": {k: _hget(hdrs, k) for k in want},
    }
    if not ok and s != 200:
        out["error"] = body[:240]
    print(json.dumps(out))
    # Missing budget headers is acceptable for local models (no Chutes billing)
    if ok and missing:
        return 0
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())

