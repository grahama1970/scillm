#!/usr/bin/env python3
"""
Grounded QA model comparison runner (experimental; live scenario)

Goal:
  - Compare two chat models (A, B) on grounded Q&A items with an internal judge.
  - Inputs: items JSONL (question + optional evidence), N, model_a, model_b, judge_model, retrieval k.
  - Behavior: (optional) retrieve evidence → call A and B (JSON output) → judge → write per‑item JSONL + summary.
  - Output: supported% per model, grounded pass rate, latency p50/p95, sample diffs.

Usage:
  python scripts/compare_grounded_qa.py \
    --items data/items.jsonl --n 50 \
    --model-a "openai/gpt-4o-mini" --model-b "openai/gpt-4o" \
    --judge-model "openai/gpt-4o-mini" \
    --out local/artifacts/compare

Notes:
  - This script is a live scenario; it will make provider calls.
  - Items JSONL format (minimum): {"id": "q1", "question": "...", "evidence": [{"id": "e1", "text": "..."}, ...]}
  - Retrieval is pluggable in future; for now, evidence in items is passed through.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from dotenv import find_dotenv, load_dotenv
import litellm

load_dotenv(find_dotenv())


def _read_jsonl(path: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if limit is not None and i >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _ensure_dir(p: str) -> None:
    Path(p).mkdir(parents=True, exist_ok=True)


def _build_messages(question: str, evidence: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ev_list = []
    for e in evidence or []:
        ev_list.append({"id": e.get("id"), "text": e.get("text")})
    sys_prompt = (
        "You are a grounded QA assistant. Answer only using the provided evidence. "
        "Return strict JSON with keys: answer (string), evidence_refs (array of ids), "
        "grounded (boolean), grounded_score (0..1), rationale_short (string)."
    )
    user_msg = {
        "role": "user",
        "content": json.dumps({"question": question, "evidence": ev_list}, ensure_ascii=False),
    }
    return [
        {"role": "system", "content": sys_prompt},
        user_msg,
    ]


def _judge_messages(item: Dict[str, Any], out_a: Dict[str, Any], out_b: Dict[str, Any]) -> List[Dict[str, Any]]:
    judge_sys = (
        "You are a grounded QA judge. Given a question, evidence, and two model outputs (A and B) "
        "decide for each whether the answer is 'supported' by evidence and which is better. "
        "Return strict JSON: {supported_a: bool, supported_b: bool, better: 'A'|'B'|'tie', "
        "confidence: number(0..1), rationale_short: string}."
    )
    payload = {
        "question": item.get("question"),
        "evidence": item.get("evidence") or [],
        "model_a": out_a,
        "model_b": out_b,
    }
    return [
        {"role": "system", "content": judge_sys},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _provider_kwargs(use_codex: bool) -> Dict[str, Any]:
    if not use_codex:
        return {}
    base = os.getenv("CODEX_AGENT_API_BASE")
    key = os.getenv("CODEX_AGENT_API_KEY")
    kw: Dict[str, Any] = {"custom_llm_provider": "codex-agent"}
    if base:
        kw["api_base"] = base
    if key:
        kw["api_key"] = key
    return kw


def _call_json(model: str, messages: List[Dict[str, Any]], timeout: float, **extra) -> (Dict[str, Any], float):
    t0 = time.time()
    resp = litellm.completion(
        model=model,
        messages=messages,
        request_timeout=timeout,
        temperature=0,
        response_format={"type": "json_object"},
        max_tokens=256,
        **extra,
    )
    dt = time.time() - t0
    content = None
    try:
        content = resp.choices[0].message["content"]  # type: ignore[index]
    except Exception:
        try:
            content = resp.choices[0].message.content
        except Exception:
            content = None
    try:
        data = json.loads(content) if isinstance(content, str) else (content or {})
    except Exception:
        data = {"parse_error": True, "raw": content}
    return data, dt


def p50_p95(samples: List[float]) -> (float, float):
    if not samples:
        return 0.0, 0.0
    s = sorted(samples)
    def pct(p: float) -> float:
        i = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
        return s[i]
    return pct(50.0), pct(95.0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", required=True, help="Path to items JSONL (question + optional evidence)")
    ap.add_argument("--n", type=int, default=50, help="Max items to evaluate")
    ap.add_argument("--model-a", required=True)
    ap.add_argument("--model-b", required=True)
    ap.add_argument("--judge-model", required=True)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--out", default="local/artifacts/compare")
    ap.add_argument("--enable-retries", action="store_true", help="Enable 429 retry layer (SCILLM_RETRY_ENABLED=1)")
    # Optional: route via codex-agent provider for resilience/metrics
    ap.add_argument("--use-codex-a", action="store_true")
    ap.add_argument("--use-codex-b", action="store_true")
    ap.add_argument("--use-codex-judge", action="store_true")
    args = ap.parse_args()

    if args.enable_retries:
        os.environ["SCILLM_RETRY_ENABLED"] = "1"

    items = _read_jsonl(args.items, args.n)
    if not items:
        print(json.dumps({"ok": False, "error": "no items"}))
        return

    ts = int(time.time())
    out_dir = os.path.join(args.out, f"run_{ts}")
    _ensure_dir(out_dir)
    out_path = os.path.join(out_dir, "results.jsonl")

    lat_a: List[float] = []
    lat_b: List[float] = []
    grounded_pass_a = 0
    grounded_pass_b = 0
    judge_supported_a = 0
    judge_supported_b = 0
    diffs: List[Dict[str, Any]] = []

    with open(out_path, "w", encoding="utf-8") as w:
        for it in items:
            q = it.get("question") or ""
            ev = it.get("evidence") or []
            msgs = _build_messages(q, ev)
            out_a, dt_a = _call_json(
                args.model_a, msgs, args.timeout, **_provider_kwargs(args.use_codex_a)
            )
            out_b, dt_b = _call_json(
                args.model_b, msgs, args.timeout, **_provider_kwargs(args.use_codex_b)
            )
            lat_a.append(dt_a)
            lat_b.append(dt_b)
            grounded_pass_a += 1 if bool(out_a.get("grounded")) else 0
            grounded_pass_b += 1 if bool(out_b.get("grounded")) else 0

            jmsgs = _judge_messages(it, out_a, out_b)
            jres, dt_j = _call_json(
                args.judge_model, jmsgs, args.timeout, **_provider_kwargs(args.use_codex_judge)
            )
            judge_supported_a += 1 if bool(jres.get("supported_a")) else 0
            judge_supported_b += 1 if bool(jres.get("supported_b")) else 0

            w.write(json.dumps({
                "id": it.get("id"),
                "question": q,
                "model_a": args.model_a,
                "model_b": args.model_b,
                "judge_model": args.judge_model,
                "out_a": out_a,
                "out_b": out_b,
                "judge": jres,
                "latency_s": {"a": dt_a, "b": dt_b, "judge": dt_j},
            }, ensure_ascii=False) + "\n")

            try:
                if (out_a.get("answer") or "") != (out_b.get("answer") or ""):
                    diffs.append({
                        "id": it.get("id"), "question": q,
                        "a": out_a.get("answer"), "b": out_b.get("answer"),
                    })
            except Exception:
                pass

    p50_a, p95_a = p50_p95(lat_a)
    p50_b, p95_b = p50_p95(lat_b)
    n = max(1, len(items))
    summary = {
        "n": n,
        "models": {"a": args.model_a, "b": args.model_b, "judge": args.judge_model},
        "grounded_pass_rate": {
            "a": grounded_pass_a / n,
            "b": grounded_pass_b / n,
        },
        "judge_supported_rate": {
            "a": judge_supported_a / n,
            "b": judge_supported_b / n,
        },
        "latency_s": {
            "a": {"p50": p50_a, "p95": p95_a},
            "b": {"p50": p50_b, "p95": p95_b},
        },
        "diff_samples": diffs[:10],
        "results_path": out_path,
    }
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps({"ok": True, "summary": summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
