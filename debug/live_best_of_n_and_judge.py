#!/usr/bin/env python3
from __future__ import annotations
import json, os, sys, time
from typing import Any, Dict, List

from scillm_codex_sdk.http import request_json

def _require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        print(f"ERROR: missing env {name}", file=sys.stderr)
        sys.exit(2)
    return v.rstrip('/')

def _post_chat(base: str, token: str, body: Dict[str, Any]) -> Dict[str, Any]:
    status, headers, data, meta = request_json(
        "POST",
        f"{base}/v1/chat/completions",
        headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
        json_body=body,
        timeout_s=60.0,
        retry_time_budget_s=60.0,
    )
    if not (200 <= status < 300):
        raise RuntimeError(f"HTTP {status}: {data}")
    return data if isinstance(data, dict) else {"choices": [{"message": {"content": json.dumps(data)}}]}

def main() -> int:
    base = os.getenv("CODEX_CLOUD_TASKS_BASE_URL") or os.getenv("CHUTES_API_BASE") or ""
    base = base.rstrip('/')
    if base.endswith('/v1'):
        base = base[:-3]
    if not base:
        print("ERROR: set CODEX_CLOUD_TASKS_BASE_URL or CHUTES_API_BASE", file=sys.stderr)
        return 2
    token = os.getenv("CODEX_CLOUD_TOKEN") or os.getenv("CHUTES_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not token:
        print("ERROR: set CODEX_CLOUD_TOKEN or CHUTES_API_KEY or OPENAI_API_KEY", file=sys.stderr)
        return 2

    gen_model = os.getenv("CODEX_CLOUD_MODEL") or os.getenv("CHUTES_MODEL") or "deepseek-ai/DeepSeek-R1"
    judge_model = os.getenv("JUDGE_MODEL") or gen_model
    n = int(os.getenv("BEST_OF_N", "3"))
    prompt = os.getenv(
        "PROMPT",
        "Propose an improved C/C++ fast inverse square root variant for real-time gaming physics. Return only a fenced code block.",
    )

    # Generate N candidates
    variants: List[Dict[str, Any]] = []
    for i in range(1, n + 1):
        body = {
            "model": gen_model,
            "temperature": 1.0,
            "messages": [
                {"role": "system", "content": "You are a senior performance engineer."},
                {"role": "user", "content": f"{prompt}\n\n// variant_id={i}"},
            ],
        }
        try:
            resp = _post_chat(base, token, body)
            content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
            variants.append({"id": i, "content": content})
            print(f"[gen] id={i} len={len(content)}")
        except Exception as e:
            variants.append({"id": i, "error": str(e)})
            print(f"[gen_error] id={i} err={e}")

    any_ok = any(v.get("content") for v in variants)
    if not any_ok:
        print(json.dumps({"base": base, "model": gen_model, "variants": variants}, ensure_ascii=False))
        return 2

    # Judge
    panel = []
    for v in variants:
        cid = v["id"]
        text = (v.get("content") or v.get("error") or "")[:6000]
        panel.append({"id": cid, "text": text})
    judge_sys = (
        "You are a strict judge. Read the candidate code variants and select the single best (1..N). "
        "Return strict JSON: {\"best_id\": <int>, \"reason\": <string>} with no extra text."
    )
    judge_user = "CANDIDATES:\n" + "\n\n".join(
        f"ID={p['id']}\n{text}" for p in panel for text in [p['text']]
    )
    judge_body = {
        "model": judge_model,
        "temperature": 1.0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": judge_sys},
            {"role": "user", "content": judge_user},
        ],
    }
    try:
        j = _post_chat(base, token, judge_body)
        content = j.get("choices", [{}])[0].get("message", {}).get("content", "{}")
        try:
            parsed = json.loads(content)
        except Exception:
            parsed = {"_raw": content}
        result = {"base": base, "gen_model": gen_model, "judge_model": judge_model, "variants": variants, "judge": parsed}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as e:
        print(json.dumps({"base": base, "gen_model": gen_model, "judge_model": judge_model, "variants": variants, "judge_error": str(e)}))
        return 1

if __name__ == "__main__":
    raise SystemExit(main())

