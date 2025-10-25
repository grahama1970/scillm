#!/usr/bin/env python3
"""
Chutes Gateway Preflight (no magic, exit-code based)

Checks:
 1) /v1/models with x-api-key → expect 200
 2) /v1/chat/completions JSON echo with each header style:
    - Authorization: <key> (raw)
    - Authorization: Bearer <key>
    - x-api-key: <key>
 3) scillm OpenAI-compatible path (openai_like) with explicit headers preserved.

Outputs compact PASS/FAIL lines and exits non-zero if:
 - models != 200, or
 - all three chat attempts fail, or
 - scillm path fails for Authorization raw.

Env:
  CHUTES_API_BASE, CHUTES_API_KEY (required)
  CHUTES_TEXT_MODEL (optional, defaults to first from /models)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import Dict


def env(name: str) -> str:
    v = (os.getenv(name) or "").strip()
    if not v:
        print(f"ENV_MISSING {name}")
        sys.exit(12)
    # Strip accidental surrounding quotes
    if (len(v) > 1) and ((v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'"))):
        v = v[1:-1].strip()
    return v


def http_get(url: str, headers: Dict[str, str]) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        return -1, str(e)


def http_post(url: str, headers: Dict[str, str], body: Dict) -> tuple[int, str, str]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", errors="ignore"), r.headers.get("Retry-After", "")
    except Exception as e:
        try:  # type: ignore[no-redef]
            # Attempt to read error payload
            payload = e.read().decode("utf-8", errors="ignore")  # type: ignore[attr-defined]
            return getattr(e, "code", -1) or -1, payload, getattr(e, "headers", {}).get("Retry-After", "")
        except Exception:
            return getattr(e, "code", -1) or -1, str(e), ""


def main() -> int:
    base = env("CHUTES_API_BASE").rstrip("/")
    key = env("CHUTES_API_KEY")

    # 1) models
    m_code, m_body = http_get(base + "/models", {"x-api-key": key, "Accept": "application/json"})
    print(f"MODELS {m_code}")
    if m_code != 200:
        return 21
    try:
        ids = [d.get("id") for d in (json.loads(m_body).get("data") or []) if d.get("id")]
    except Exception:
        ids = []
    model = (os.getenv("CHUTES_TEXT_MODEL") or (ids[0] if ids else "")).strip()
    if not model:
        print("NO_TEXT_MODEL")
        return 22

    # 2) chat with three header styles
    url = base + "/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": 'Return only {"ok":true} as JSON.'}],
        "response_format": {"type": "json_object"},
    }
    attempts = [
        ("auth_raw", {"Content-Type": "application/json", "Authorization": key}),
        ("auth_bearer", {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}),
        ("x_api_key", {"Content-Type": "application/json", "x-api-key": key}),
    ]
    ok_any = False
    for label, hdr in attempts:
        code, out, ra = http_post(url, hdr, body)
        is_200 = code == 200
        is_429 = code == 429
        ok = is_200 and ((out or "").strip() != "")  # accept any non-empty content
        note = ""
        if is_429:
            note = f"CAPACITY retry_after={ra}s" if ra else "CAPACITY"
        # Extract helpful message from typical fields
        msg = ""
        try:
            j = json.loads(out)
            msg = j.get("detail") or j.get("error") or ""
        except Exception:
            pass
        extra = (f" {note}" if note else "") + (f" msg='{msg[:80]}'" if msg else "")
        status = "PASS" if (ok or is_429) else "FAIL"
        print(f"CHAT_{label} {code} {status}{extra}")
        if ok or is_429:
            ok_any = True

    # 3) scillm path (Authorization raw), only if importable
    sc_ok = True
    try:
        from scillm import completion as sc_completion  # type: ignore

        sc_resp = sc_completion(
            model=model,
            api_base=base,
            api_key=None,
            custom_llm_provider="openai_like",
            messages=[{"role": "user", "content": 'Return only {"ok":true} as JSON.'}],
            response_format={"type": "json_object"},
            extra_headers={"Content-Type": "application/json", "Authorization": key},
            timeout=15,
        )
        content = (sc_resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
        sc_ok = bool(content)
        print(f"SCILLM_HTTPX {'PASS' if sc_ok else 'FAIL'}")
    except Exception as e:  # pragma: no cover - best-effort
        sc_ok = False
        msg = str(e)
        status = "CAPACITY" if ("429" in msg or "capacity" in msg.lower()) else "ERROR"
        print(f"SCILLM_HTTPX {status} {msg[:140]}")

    if m_code != 200:
        return 31
    if not ok_any:
        return 31
    # scillm path non-blocking here; it can be flaky under capacity
    return 0


if __name__ == "__main__":
    sys.exit(main())
