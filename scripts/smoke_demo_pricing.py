#!/usr/bin/env python3
"""
Smoke: verify that demo pricing causes sc_cost_usd_total to increase after one chat call.

Assumptions
- Proxy is running (default http://127.0.0.1:4010) with CHUTES_PRICING_FILE
  set to examples/pricing/example.json (e.g., via `make proxy-run-uv-demo-pricing`).

Behavior
- Scrape /metrics, sum sc_cost_usd_total.
- POST one JSON chat via /v1/chat/completions using the dev master key.
- Scrape /metrics again; assert delta > 0.
- Print a compact JSON result and exit 0/1.

Env (optional)
- PROXY_BASE (default: http://127.0.0.1:4010)
- MASTER_KEY (default: sk-dev-proxy-123)
- SMOKE_MODEL (default: gpt-chutes)
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error


def _get(url: str, headers: dict | None = None, timeout: float = 10.0) -> tuple[int, str, dict]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310
            body = r.read().decode("utf-8", errors="replace")
            return r.getcode(), body, dict(r.headers)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, body, dict(e.headers)
    except Exception as e:  # pragma: no cover
        return 0, str(e), {}


def _post(url: str, payload: dict, headers: dict | None = None, timeout: float = 15.0) -> tuple[int, str, dict]:
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
    except Exception as e:  # pragma: no cover
        return 0, str(e), {}


def _sum_cost(metrics_text: str) -> float:
    total = 0.0
    for line in metrics_text.splitlines():
        if not line or line.startswith("#"):
            continue
        if line.startswith("sc_cost_usd_total"):
            try:
                # exposition: <metric>{labels} value
                val = float(line.rsplit(" ", 1)[-1])
                total += val
            except Exception:
                pass
    return total


def main() -> int:
    base = os.getenv("PROXY_BASE", "http://127.0.0.1:4010").rstrip("/")
    master = os.getenv("MASTER_KEY", os.getenv("LITELLM_MASTER_KEY", "sk-dev-proxy-123"))
    model = os.getenv("SMOKE_MODEL", "gpt-chutes")

    # 1) Baseline metrics
    s1, body1, _ = _get(f"{base}/metrics")
    if s1 != 200:
        print(json.dumps({"ok": False, "step": "baseline_metrics", "status": s1, "error": body1[:200]}))
        return 1
    before = _sum_cost(body1)

    # 2) One JSON chat via proxy
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Return only {\"ok\":true} as JSON."}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 16,
    }
    s2, body2, h2 = _post(
        f"{base}/v1/chat/completions",
        payload,
        headers={"Authorization": f"Bearer {master}"},
        timeout=30.0,
    )
    if s2 != 200:
        print(json.dumps({"ok": False, "step": "chat", "status": s2, "error": body2[:240]}))
        return 1

    # 3) Allow metric scrape/update
    time.sleep(2.0)
    s3, body3, _ = _get(f"{base}/metrics")
    if s3 != 200:
        print(json.dumps({"ok": False, "step": "post_metrics", "status": s3, "error": body3[:200]}))
        return 1
    after = _sum_cost(body3)
    delta = after - before

    ok = delta > 0.0
    out = {
        "ok": ok,
        "status": 200,
        "cost_before": round(before, 9),
        "cost_after": round(after, 9),
        "delta": round(delta, 9),
        "proxy_base": base,
        "model": model,
    }
    print(json.dumps(out))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())

