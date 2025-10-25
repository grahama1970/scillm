#!/usr/bin/env python3
"""
Mock auth negotiation smoke against the local chutes_mock_server.

Asserts:
- SAFE_MODE=1 (default) + Bearer → AuthenticationError (no mutation)
- SAFE_MODE=0 + ENABLE_AUTO_AUTH=1 + Bearer → success (converted to x-api-key)

Prints a one-line JSON summary and exits 0 only if both assertions pass.
"""
import json
import os
import signal
import subprocess
import sys
import time


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v if v is not None else default


def _curl_status(url: str, headers: dict) -> int:
    import httpx

    with httpx.Client(timeout=httpx.Timeout(timeout=5.0, connect=1.0)) as c:
        r = c.get(url, headers=headers)
        return r.status_code


def _wait_ready(base: str, timeout_s: float = 10.0) -> bool:
    import httpx

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout=2.0, connect=0.5)) as c:
                r = c.get(base + "/models")
                if r.status_code in (200, 401):
                    return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def _litellm_completion(base: str, key: str) -> str:
    import litellm

    # Call with Bearer on non-openai base
    resp = litellm.completion(
        model="any/mock",
        api_base=base + "/chat/completions",
        api_key=None,
        custom_llm_provider="openai_like",
        extra_headers={"Authorization": f"Bearer {key}"},
        messages=[{"role": "user", "content": "In one word, say OK"}],
        temperature=0,
        max_tokens=8,
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.get("content", "") or ""


def main() -> int:
    base = _env("SC_MOCK_BASE", "http://127.0.0.1:18093/v1")
    key = _env("SC_MOCK_KEY", "sk_mock_key")

    # Start mock server
    uvicorn_cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "scripts.chutes_mock_server:app",
        "--host",
        "127.0.0.1",
        "--port",
        "18093",
    ]
    proc = subprocess.Popen(uvicorn_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        if not _wait_ready(base):
            print(json.dumps({"ok": False, "error": "mock_not_ready"}))
            return 1

        # Sanity: Bearer=401; x-api-key=200 on /models
        s1 = _curl_status(base + "/models", {"Authorization": f"Bearer {key}"})
        s2 = _curl_status(base + "/models", {"x-api-key": key})

        # Negative: SAFE_MODE=1 (default), Bearer must fail through litellm
        os.environ.pop("SCILLM_ENABLE_AUTO_AUTH", None)
        os.environ["SCILLM_SAFE_MODE"] = "1"
        neg_ok = False
        neg_err = None
        try:
            _ = _litellm_completion(base, key)
        except Exception as e:
            neg_ok = True
            neg_err = type(e).__name__

        # Positive: enable auto-auth conversion and retry
        os.environ["SCILLM_SAFE_MODE"] = "0"
        os.environ["SCILLM_ENABLE_AUTO_AUTH"] = "1"
        pos_text = _litellm_completion(base, key)

        ok = (s1 == 401 and s2 == 200 and neg_ok and isinstance(pos_text, str) and len(pos_text) > 0)
        print(json.dumps({
            "ok": ok,
            "models_status": {"bearer": s1, "x_api_key": s2},
            "neg": {"ok": neg_ok, "error": neg_err},
            "pos": {"ok": bool(pos_text), "sample": pos_text[:16]},
        }))
        return 0 if ok else 2
    finally:
        try:
            proc.send_signal(signal.SIGTERM)
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
    

if __name__ == "__main__":
    raise SystemExit(main())

