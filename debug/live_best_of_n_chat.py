#!/usr/bin/env python3
from __future__ import annotations
import json, os, sys, time
from typing import Any, Dict, List

# Live best-of-N via an OpenAI-compatible /v1/chat/completions base.
# Uses env from .env (CHUTES_*) or CODEX_CLOUD_*.

def _env_flag(name: str) -> bool:
    v = os.getenv(name)
    return bool(v) and v not in ("0", "false", "False", "")

def _base_from_env() -> str:
    base = os.getenv("CODEX_CLOUD_TASKS_BASE_URL")
    if not base:
        base = os.getenv("CHUTES_API_BASE")
    if not base:
        raise SystemExit("ERROR: set CODEX_CLOUD_TASKS_BASE_URL or CHUTES_API_BASE")
    base = base.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base

def _token_from_env() -> str:
    tok = os.getenv("CODEX_CLOUD_TOKEN") or os.getenv("CHUTES_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not tok:
        raise SystemExit("ERROR: set CODEX_CLOUD_TOKEN or CHUTES_API_KEY or OPENAI_API_KEY")
    return tok

def request_chat(base: str, token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    from scillm_codex_sdk.http import request_json  # resilient HTTP with retries
    status, headers, data, meta = request_json(
        "POST",
        f"{base}/v1/chat/completions",
        headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
        json_body=body,
        timeout_s=60.0,
        retry_time_budget_s=60.0,
    )
    if not (200 <= status < 300):
        raise SystemExit(f"HTTP {status}: {data}")
    if isinstance(data, dict) and data.get("choices"):
        return data
    return {"choices": [{"message": {"content": json.dumps(data)}}]}

def main() -> int:
    base = _base_from_env()
    token = _token_from_env()
    model = os.getenv("CODEX_CLOUD_MODEL") or os.getenv("CHUTES_MODEL") or "deepseek-ai/DeepSeek-R1"
    n = int(os.getenv("BEST_OF_N", "6"))

    user_prompt = os.getenv(
        "PROMPT",
        "Propose one improved C/C++ fast inverse square root variant for real-time gaming physics."
        " Return only the code in a fenced block."
    )
    results: List[Dict[str, Any]] = []
    for i in range(n):
        body = {
            "model": model,
            "temperature": 1.0,
            "messages": [
                {"role": "system", "content": "You are a senior performance engineer."},
                {"role": "user", "content": user_prompt + f"\n\n// variant_id={i+1}"},
            ],
        }
        try:
            resp = request_chat(base, token, body)
            content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
            results.append({"id": i + 1, "content": content[:4000]})
            print(f"[ok] variant {i+1}: {len(content)} chars")
        except Exception as e:
            results.append({"id": i + 1, "error": str(e)})
            print(f"[err] variant {i+1}: {e}")
            continue

    # Print compact JSON for downstream tools
    print(json.dumps({"base": base, "model": model, "n": n, "variants": results}, ensure_ascii=False))
    # Exit non-zero if all failed
    any_ok = any("content" in v and v["content"] for v in results)
    return 0 if any_ok else 2

if __name__ == "__main__":
    raise SystemExit(main())

