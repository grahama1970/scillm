#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio as aio
import json
import os
import sys
import time
from typing import Any, Dict, List, Tuple

from dotenv import find_dotenv, load_dotenv

# Prefer httpx transport to avoid lingering aiohttp sessions during short sanity runs
os.environ.setdefault("SCILLM_DISABLE_AIOHTTP", "1")
os.environ.setdefault("DISABLE_AIOHTTP_TRANSPORT", "True")

from scillm import parallel_acompletions_iter
from scillm.batch import _extract_content_from_response
from scillm.extras.env_utils import _env_float, _env_int
from scillm.extras.json_utils import clean_json_string
from litellm.llms.custom_httpx.async_client_cleanup import close_litellm_async_clients




def _scenario_definitions(system_prompt: str) -> List[Dict[str, Any]]:
    return [
        {
            "scenario": "echo_true",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "Return only {\"ok\": true} as JSON."},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 16,
            "temperature": 0,
        },
        {
            "scenario": "sum_chain",
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        "Compute 17 + 28 + 13. Respond strictly with a JSON object "
                        "{\"problem\":\"17+28+13\",\"answer\":58,\"explanation\":<brief string>}"
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "ArithmeticAnswer",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "problem": {"type": "string"},
                            "answer": {"type": "integer"},
                            "explanation": {"type": "string"},
                        },
                        "required": ["problem", "answer"],
                    },
                },
            },
            "max_tokens": 48,
            "temperature": 0,
        },
        {
            "scenario": "country_snapshot",
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        "Respond with JSON containing the keys country, capital, and continent for France. "
                        "Example shape: {\"country\":\"France\",\"capital\":\"Paris\",\"continent\":\"Europe\"}."
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "CountrySnapshot",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "country": {"type": "string"},
                            "capital": {"type": "string"},
                            "continent": {"type": "string"},
                        },
                        "required": ["country", "capital", "continent"],
                    },
                },
            },
            "max_tokens": 32,
            "temperature": 0,
        },
        {
            "scenario": "migration_plan",
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        "Create a three step plan for migrating a REST API to Chutes. "
                        "Respond strictly as {\"steps\":[{\"id\":1,\"task\":<string>,\"owner\":<string>}...],\"confidence\":<high|medium|low>}"
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "MigrationPlan",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "steps": {
                                "type": "array",
                                "minItems": 3,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "integer"},
                                        "task": {"type": "string"},
                                        "owner": {"type": "string"},
                                    },
                                    "required": ["id", "task", "owner"],
                                },
                            },
                            "confidence": {"type": "string"},
                        },
                        "required": ["steps"],
                    },
                },
            },
            "max_tokens": 160,
            "temperature": 0.1,
        },
        {
            "scenario": "decision_matrix",
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        "Compare the deployment strategies 'low_latency' and 'high_accuracy'. Respond as "
                        "{\"scores\":[{\"option\":\"low_latency\",\"score\":<0-1>,\"justification\":<string>},"
                        "{\"option\":\"high_accuracy\",\"score\":<0-1>,\"justification\":<string>}],\"winner\":<string from options>}."
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "DecisionMatrix",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "scores": {
                                "type": "array",
                                "minItems": 2,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "option": {"type": "string"},
                                        "score": {"type": "number"},
                                        "justification": {"type": "string"},
                                    },
                                    "required": ["option", "score", "justification"],
                                },
                            },
                            "winner": {"type": "string"},
                        },
                        "required": ["scores", "winner"],
                    },
                },
            },
            "max_tokens": 200,
            "temperature": 0.2,
        },
    ]


def _validate_payload(scenario: str, payload: Any) -> Tuple[bool, str | None]:
    if not isinstance(payload, dict):
        return False, "payload_not_dict"
    if scenario == "echo_true":
        return (payload.get("ok") is True, None if payload.get("ok") is True else "missing_ok_true")
    if scenario == "sum_chain":
        prob = payload.get("problem")
        ans = payload.get("answer")
        if prob != "17+28+13" or ans != 58:
            return False, f"mismatch:problem={prob},answer={ans}"
        return True, None
    if scenario == "country_snapshot":
        ctry = str(payload.get("country") or "").lower()
        capital = str(payload.get("capital") or "").lower()
        continent = str(payload.get("continent") or "").lower()
        ok = ctry == "france" and capital == "paris" and continent == "europe"
        return (ok, None if ok else "country_snapshot_mismatch")
    if scenario == "migration_plan":
        steps = payload.get("steps")
        if not isinstance(steps, list) or len(steps) < 3:
            return False, "missing_steps"
        for step in steps:
            if not isinstance(step, dict):
                return False, "invalid_step"
            if not isinstance(step.get("task"), str) or not step.get("task"):
                return False, "empty_task"
            if not isinstance(step.get("owner"), str) or not step.get("owner"):
                return False, "empty_owner"
        return True, None
    if scenario == "decision_matrix":
        scores = payload.get("scores")
        if not isinstance(scores, list) or len(scores) < 2:
            return False, "missing_scores"
        opts = {"low_latency", "high_accuracy"}
        seen = set()
        for entry in scores:
            if not isinstance(entry, dict):
                return False, "invalid_score_entry"
            opt = entry.get("option")
            if opt not in opts:
                return False, "unknown_option"
            if not isinstance(entry.get("justification"), str) or not entry.get("justification"):
                return False, "missing_justification"
            seen.add(opt)
        winner = payload.get("winner")
        if winner not in seen:
            return False, "invalid_winner"
        return True, None
    return False, "unknown_scenario"


async def main(argv: List[str] | None = None) -> int:
    load_dotenv(find_dotenv(), override=False)
    argv = argv or []
    if not argv:
        argv = ["--execute"]

    default_wall = _env_float("SCILLM_SANITY_WALL_TIME_S", 1800.0)
    default_timeout = _env_float("SCILLM_SANITY_TIMEOUT_S", 30.0)
    default_concurrency = _env_int("SCILLM_SANITY_CONCURRENCY", 3)

    parser = argparse.ArgumentParser(description="Chutes experimental model JSON sanity (5 probes via parallel_acompletions_iter)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="List probe payloads without executing")
    mode.add_argument("--execute", action="store_true", help="Perform live requests (default)")
    parser.add_argument("--tenacious", dest="tenacious", action="store_true", help="Enable retries/backoff within wall time")
    parser.add_argument("--no-tenacious", dest="tenacious", action="store_false", help="Disable retries")
    parser.set_defaults(tenacious=True)
    parser.add_argument("--retry-initial-delay", type=float, default=_env_float("SCILLM_BACKOFF_BASE", 0.5), help="Initial delay before retries (seconds)")
    parser.add_argument("--retry-max-delay", type=float, default=_env_float("SCILLM_BACKOFF_CAP_S", 30.0), help="Maximum delay between retries (seconds)")
    parser.add_argument("--retry-wall-time-s", type=float, default=default_wall, help="Wall time budget per request (seconds)")
    parser.add_argument("--request-timeout-s", type=float, default=default_timeout, help="Timeout per provider call (seconds)")
    parser.add_argument("--concurrency", type=int, default=default_concurrency, help="Parallel request concurrency")
    json_sanitize_default = os.getenv("SCILLM_JSON_SANITIZE", "0").lower() in {"1", "true", "yes", "on"}
    parser.add_argument("--model", dest="model_override", help="Override CHUTES_EXPERIMENTAL for this run")
    parser.add_argument("--json-sanitize", dest="json_sanitize", action="store_true", default=json_sanitize_default, help="Attempt to repair JSON on failure")
    parser.add_argument("--no-json-sanitize", dest="json_sanitize", action="store_false", help="Disable JSON repair attempts")
    parser.add_argument("--verbose", action="store_true", help="Print per-scenario progress")
    parser.add_argument("--json-summary", action="store_true", help="Print machine-readable JSON summary (default off)")
    parser.add_argument("--details", action="store_true", help="Show per-scenario PASS/FAIL rows (default hides them)")

    args = parser.parse_args(argv)

    if not args.dry_run and not args.execute:
        args.execute = True

    base = os.environ.get("CHUTES_API_BASE", "").strip()
    key = os.environ.get("CHUTES_API_KEY", "").strip()
    model_name = (args.model_override or os.environ.get("CHUTES_EXPERIMENTAL", "")).strip()
    # model_name = os.environ.get("CHUTES_TEXT_MODEL", "").strip()
    if not base or not key or not model_name:
        raise SystemExit("Missing CHUTES_API_BASE, CHUTES_API_KEY, or CHUTES_EXPERIMENTAL environment variables.")

    model_alias = "chutes/experimental"
    system_prompt = "You must respond with strictly valid JSON that satisfies the requested schema."
    scenario_defs = _scenario_definitions(system_prompt)

    batch_requests: List[Dict[str, Any]] = []
    for entry in scenario_defs:
        req = {
            "model": model_name,
            "messages": entry["messages"],
            "response_format": entry["response_format"],
            "max_tokens": entry["max_tokens"],
            "temperature": entry["temperature"],
            "artifacts": {"file_paths": [], "urls": []},
            "api_base": base,
            "api_key": key,
            "custom_llm_provider": "openai_like",
        }
        batch_requests.append({"scenario": entry["scenario"], "request": req})

    scenarios = [item["scenario"] for item in batch_requests]
    requests = [item["request"] for item in batch_requests]

    if args.dry_run and not args.execute:
        preview = {
            "mode": "dry-run",
            "count": len(batch_requests),
            "scenarios": scenarios,
            "model_alias": model_alias,
            "model": model_name,
        }
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0

    wall_time_s = args.retry_wall_time_s
    timeout_s = args.request_timeout_s

    raw_results: List[Dict[str, Any] | None] = [None] * len(requests)
    last_error = None
    start = time.time()

    try:
        async for entry in parallel_acompletions_iter(
            requests,
            concurrency=args.concurrency,
            tenacious=args.tenacious,
            wall_time_s=wall_time_s,
            timeout=timeout_s,
            backoff_base=args.retry_initial_delay,
            backoff_cap_s=args.retry_max_delay,
        ):
            idx = entry.get("index", 0)
            if idx < 0 or idx >= len(requests):
                continue
            req = entry.get("request") or requests[idx]
            ok = bool(entry.get("ok")) and not entry.get("error")
            err_msg = entry.get("error")
            if err_msg:
                last_error = err_msg
            if ok:
                resp = entry.get("response")
                content = entry.get("content")
                if content is None:
                    content = _extract_content_from_response(resp)
                raw_results[idx] = {
                    "request": req,
                    "error": None,
                    "content": content,
                    "attempts": entry.get("attempts"),
                    "elapsed_s": entry.get("elapsed_s"),
                }
                if args.verbose:
                    preview = (content or "")[:120].replace("\n", " ")
                    print(f"SCENARIO {scenarios[idx]} -> OK {preview}", flush=True)
            else:
                raw_results[idx] = {
                    "request": req,
                    "error": err_msg or "unknown_error",
                    "content": entry.get("content"),
                    "attempts": entry.get("attempts"),
                    "elapsed_s": entry.get("elapsed_s"),
                }
                if args.verbose:
                    print(f"SCENARIO {scenarios[idx]} -> ERR {err_msg}", flush=True)
    except Exception as exc:  # pragma: no cover - safety net
        last_error = str(exc)
        raw_results = []
    finally:
        try:
            await close_litellm_async_clients()
        except Exception:
            pass

    if not raw_results or any(r is None for r in raw_results):
        raise SystemExit("Missing results from parallel_acompletions_iter")

    items: List[Dict[str, Any]] = []
    success = 0

    for idx, result in enumerate(raw_results):
        scenario = scenarios[idx]
        err = result.get("error")
        content = result.get("content") or ""
        parsed = None
        ok = False
        reason = None
        if err:
            reason = err
        else:
            raw_content = content.strip() if isinstance(content, str) else content
            if isinstance(raw_content, str):
                try:
                    parsed = json.loads(raw_content)
                except Exception:
                    if args.json_sanitize:
                        try:
                            parsed_candidate = clean_json_string(raw_content, return_dict=True)
                        except Exception:
                            parsed_candidate = None
                        if parsed_candidate is not None:
                            parsed = parsed_candidate
                            content = json.dumps(parsed_candidate)
                    if parsed is None:
                        reason = "invalid_json"
                else:
                    content = raw_content
            elif isinstance(raw_content, (dict, list)):
                parsed = raw_content
                content = json.dumps(parsed)
            else:
                reason = "unsupported_content"

            if parsed is not None and reason is None:
                ok, reason = _validate_payload(scenario, parsed)
        success += 1 if ok else 0
        items.append({
            "index": idx,
            "scenario": scenario,
            "ok": ok,
            "reason": reason,
            "content_head": (content or "")[:160].replace("\n", " "),
            "attempts": result.get("attempts"),
            "elapsed_s": result.get("elapsed_s"),
        })

    elapsed = round(time.time() - start, 3)
    failure = len(items) - success
    summary = {
        "ok": success == len(items) and last_error is None,
        "count": len(items),
        "success_count": success,
        "failure_count": failure,
        "error": last_error,
        "model": model_name,
        "items": items,
        "elapsed_s": elapsed,
    }
    verdict = "PASS" if summary["ok"] else "FAIL"
    reason_counts: Dict[str, int] = {}
    for item in items:
        if item.get("ok"):
            continue
        label = item.get("reason") or "unknown"
        reason_counts[label] = reason_counts.get(label, 0) + 1
    if reason_counts:
        reason_bits = ", ".join(f"{label}×{count}" for label, count in sorted(reason_counts.items()))
    else:
        reason_bits = "all_ok"
    print(
        f"RESULT {verdict} {success}/{len(items)} model={model_name} elapsed_s={elapsed} reasons={reason_bits}"
    )
    if args.json_summary:
        print(json.dumps(summary, ensure_ascii=False))
    if args.details or args.json_summary:
        print(
            f"SUMMARY chutes_experimental_json ok={1 if summary['ok'] else 0} "
            f"count={len(items)} success={success} failure={failure} elapsed_s={elapsed}"
        )
    if args.details:
        for item in items:
            status = "PASS" if item.get("ok") else "FAIL"
            reason = item.get("reason") or "ok"
            snippet = item.get("content_head") or ""
            if snippet:
                print(f"{status} {item['scenario']}: {reason} | {snippet}")
            else:
                print(f"{status} {item['scenario']}: {reason}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(aio.run(main(sys.argv[1:])))
    except RuntimeError:
        loop = aio.get_event_loop()
        raise SystemExit(loop.run_until_complete(main(sys.argv[1:])))


"""
 python scripts/sanity/chutes_experimental_json_sanity.py --execute 
"""
