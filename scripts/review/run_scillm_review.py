#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import requests


def getenv_first(*keys: str, default: Optional[str] = None) -> Optional[str]:
    for k in keys:
        v = os.getenv(k)
        if v:
            return v
    return default


def resolve_api_base() -> str:
    base = getenv_first(
        "CODEX_AGENT_API_BASE",
        "OPENAI_API_BASE",
        default="http://127.0.0.1:8788",
    )
    # Normalize without trailing slash
    return base.rstrip("/")


def list_models(base: str) -> Dict[str, Any]:
    r = requests.get(f"{base}/v1/models", timeout=10)
    r.raise_for_status()
    return r.json()


def choose_model(model_list: Dict[str, Any]) -> str:
    env_model = getenv_first("REVIEW_MODEL")
    if env_model:
        return env_model
    # Heuristic: prefer first model that looks like gpt-5*/gpt-4.1* else first
    data = model_list.get("data") or []
    names = [m.get("id") for m in data if isinstance(m, dict) and m.get("id")]
    for pref in ("gpt-5", "gpt-4.1", "gpt-4o", "gpt-4", "gpt-3.5"):
        for n in names:
            if str(n).startswith(pref):
                return str(n)
    if names:
        return str(names[0])
    raise RuntimeError("No models returned by /v1/models; ensure codex-agent is running.")


def load_prompt() -> str:
    # Allow override
    p = getenv_first("REVIEW_PROMPT_FILE") or "docs/review_competition/prompt_review.md"
    path = Path(p)
    if path.exists():
        return path.read_text(encoding="utf-8")
    # Fallback minimal prompt
    return (
        "You are an expert code reviewer. Read this repository and produce a concise, actionable review focusing on readiness, risk, and merge blockers."
    )


def read_repo_context() -> str:
    # Keep lightweight: include PROJECT_READY summary + NEXT_STEPS if present
    parts: list[str] = []
    for rel in ("PROJECT_READY.md", "NEXT_STEPS.md", "README.md"):
        fp = Path(rel)
        if fp.exists():
            try:
                parts.append(f"\n\n=== {rel} ===\n\n" + fp.read_text(encoding="utf-8")[:20000])
            except Exception:
                pass
    return "".join(parts)


def chat_completion(base: str, model: str, system_prompt: str, user_prompt: str) -> str:
    url = f"{base}/v1/chat/completions"
    # High reasoning by default; allow override via REVIEW_REASONING (minimal|low|medium|high|disable)
    reasoning_effort = os.getenv("REVIEW_REASONING", "high").strip().lower()
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": float(os.getenv("REVIEW_TEMPERATURE", "0.2")),
    }
    # Include both shapes for compatibility with different OpenAI‑compatible gateways
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
        payload["reasoning"] = {"effort": reasoning_effort}
    headers = {"Content-Type": "application/json"}
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=120)
    # Fallbacks
    if r.status_code == 404:
        raise RuntimeError(
            f"Model '{model}' not found on base {base}; check /v1/models and set REVIEW_MODEL."
        )
    if r.status_code in (400, 502):
        # Try mini-agent deterministic endpoint
        env = {
            "model": model,
            "messages": payload["messages"],
            "tool_backend": "local",
            "use_tools": False,
            "max_iterations": 1,
        }
        r2 = requests.post(f"{base}/agent/run", headers=headers, data=json.dumps(env), timeout=120)
        r2.raise_for_status()
        data2 = r2.json()
        return str(data2.get("final_answer") or "")
    r.raise_for_status()
    data = r.json()
    try:
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        raise RuntimeError(f"Unexpected response shape: {data}") from e


def main() -> int:
    out_dir = Path("docs/review_competition")
    out_dir.mkdir(parents=True, exist_ok=True)

    base = resolve_api_base()
    print(f"[review] Using base: {base}")
    try:
        models = list_models(base)
    except Exception as e:
        print(f"[review] Failed to list models from {base}: {e}", file=sys.stderr)
        print("Tip: Start mini-agent or set CODEX_AGENT_API_BASE.", file=sys.stderr)
        return 2

    model = choose_model(models)
    print(f"[review] Selected model: {model}")

    system_prompt = "You are a careful, structured senior code reviewer."
    prompt = load_prompt() + "\n\n" + read_repo_context()

    try:
        content = chat_completion(base, model, system_prompt, prompt)
    except Exception as e:
        print(f"[review] Chat completion failed: {e}", file=sys.stderr)
        return 3

    out_file = out_dir / "02_gpt5_high.md"
    out_file.write_text(content, encoding="utf-8")
    print(f"[review] Wrote {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
