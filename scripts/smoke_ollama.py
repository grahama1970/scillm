#!/usr/bin/env python3
"""
Smoke: verify Ollama is reachable and can serve a basic chat request.

This runs BEFORE proxy smokes to distinguish "Ollama hung" from "proxy broken".

Env (optional)
- OLLAMA_BASE (default: http://127.0.0.1:11434)
- OLLAMA_MODEL (default: qwen2.5:0.5b)
- OLLAMA_TIMEOUT (default: 45)
"""
import json
import os
import sys
import urllib.request
import urllib.error


def _post(url: str, payload: dict, timeout: float = 10.0):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310
            return r.getcode(), r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return 0, str(e)


def _get(url: str, timeout: float = 5.0):
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310
            return r.getcode(), r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return 0, str(e)


def main() -> int:
    base = os.getenv("OLLAMA_BASE", os.getenv("OLLAMA_API_BASE", "http://127.0.0.1:11434")).rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "qwen2.5:0.5b")
    timeout = float(os.getenv("OLLAMA_TIMEOUT", "45"))

    # 1) Check Ollama API is reachable
    s1, body1 = _get(f"{base}/api/tags", timeout=5.0)
    if s1 == 0:
        out = {"ok": False, "step": "tags", "status": 0, "error": "ollama unreachable", "base": base}
        print(json.dumps(out))
        return 1

    # 2) Check model is available
    try:
        models = [m["name"] for m in json.loads(body1).get("models", [])]
    except Exception:
        models = []
    # Match with or without tag suffix
    model_found = any(m == model or m.startswith(f"{model}:") or model.startswith(f"{m}:") for m in models)
    if not model_found and model not in " ".join(models):
        out = {"ok": False, "step": "model_check", "error": f"model '{model}' not found", "available": models}
        print(json.dumps(out))
        return 1

    # 3) Unload model first to kill any hung runner, then reload fresh
    import time
    _post(f"{base}/api/generate", {"model": model, "keep_alive": 0}, timeout=10.0)
    time.sleep(2)

    # 4) Chat completion — the real test
    s2, body2 = _post(
        f"{base}/api/chat",
        {"model": model, "messages": [{"role": "user", "content": "say ok"}], "stream": False},
        timeout=timeout,
    )
    if s2 == 0:
        out = {
            "ok": False,
            "step": "chat",
            "status": 0,
            "error": f"ollama chat timed out after {timeout}s (runner likely hung)",
            "model": model,
            "base": base,
            "action": "restart ollama: sudo systemctl restart ollama",
        }
        print(json.dumps(out))
        return 1

    if s2 != 200:
        out = {"ok": False, "step": "chat", "status": s2, "error": body2[:200], "model": model}
        print(json.dumps(out))
        return 1

    # Parse response
    try:
        content = json.loads(body2).get("message", {}).get("content", "")[:50]
    except Exception:
        content = body2[:50]

    out = {"ok": True, "status": 200, "model": model, "response": content, "base": base}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
