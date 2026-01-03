#!/usr/bin/env python3
"""Lightweight curl-based variant of the Chutes experimental JSON sanity probe.

This script mirrors the scenarios from chutes_experimental_json_sanity.py but
uses the system `curl` binary for every request so that developers can inspect
and replay the exact HTTP traffic without going through the SciLLM client.

python scripts/sanity/chutes_experimental_json_sanity_curl.py --execute --model moonshotai/Kimi-K2-Thinking
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Tuple

from dotenv import find_dotenv, load_dotenv

STATUS_MARKER = "__CURL_HTTP_STATUS__"


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
                        '{"problem":"17+28+13","answer":58,"explanation":<brief string>}'
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
                        'Example shape: {"country":"France","capital":"Paris","continent":"Europe"}.'
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
                        'Respond strictly as {"steps":[{"id":1,"task":<string>,"owner":<string>}...],"confidence":<high|medium|low>}'
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
                        '{"scores":[{"option":"low_latency","score":<0-1>,"justification":<string>},'
                        '{"option":"high_accuracy","score":<0-1>,"justification":<string>}],"winner":<string from options>}.'
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


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _repair_json_string(raw: str) -> str | None:
    text = raw.strip()
    lowered = text.lower()
    if lowered.startswith("```json"):
        text = text[text.find("\n") + 1 :]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = text[start : end + 1]
    try:
        json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return candidate


def _extract_message_and_json(raw_text: str) -> Tuple[str | None, str | None, Any]:
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return None, "response_not_json", None

    if isinstance(parsed, dict):
        error_obj = parsed.get("error")
        if error_obj:
            if isinstance(error_obj, dict):
                message = error_obj.get("message") or error_obj.get("type")
            else:
                message = str(error_obj)
            return None, message or "chutes_error", parsed

        choices = parsed.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0] or {}
            message = choice.get("message") or {}
            content = message.get("content")
            if isinstance(content, list):
                combined = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
                content = combined
            if isinstance(content, str):
                return content, None, parsed

        output = parsed.get("output") if isinstance(parsed, dict) else None
        if isinstance(output, dict):
            text = output.get("text")
            if isinstance(text, str):
                return text, None, parsed

    return None, "no_message_content", parsed


def _build_curl_command(endpoint: str, api_key: str, payload: Dict[str, Any], timeout: float, headers: List[str]) -> Tuple[List[str], str]:
    data = json.dumps(payload, ensure_ascii=False)
    cmd = [
        "curl",
        "--silent",
        "--show-error",
        "--location",
        "--max-time",
        str(max(timeout, 1e-3)),
        "--header",
        "Content-Type: application/json",
        "--header",
        f"Authorization: Bearer {api_key}",
    ]
    for header in headers:
        cmd.extend(["--header", header])
    cmd.extend([
        "--request",
        "POST",
        "--data-binary",
        data,
        "--url",
        endpoint,
        "--write-out",
        f"\n{STATUS_MARKER}%{{http_code}}",
    ])
    quoted = " ".join(shlex.quote(part) for part in cmd)
    return cmd, quoted


def _invoke_curl(endpoint: str, api_key: str, payload: Dict[str, Any], timeout: float, headers: List[str]) -> Dict[str, Any]:
    cmd, formatted = _build_curl_command(endpoint, api_key, payload, timeout, headers)
    start = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - start
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    body = stdout
    status_code = None
    if STATUS_MARKER in stdout:
        prefix, _, suffix = stdout.rpartition(STATUS_MARKER)
        body = prefix.rstrip("\n")
        candidate = suffix.strip()
        if candidate:
            try:
                status_code = int(candidate)
            except ValueError:
                status_code = None
    ok = proc.returncode == 0 and (status_code is None or status_code < 400)
    error = None
    if not ok:
        if proc.returncode != 0:
            error = f"curl_exit_{proc.returncode}"
        elif status_code is not None and status_code >= 400:
            error = f"http_{status_code}"
        if stderr:
            error = f"{error}:{stderr.strip()}" if error else stderr.strip()
    return {
        "ok": ok,
        "body": body,
        "status_code": status_code,
        "stderr": stderr.strip(),
        "returncode": proc.returncode,
        "elapsed_s": elapsed,
        "command": formatted,
        "error": error,
    }


def _format_curl_preview(endpoint: str, payload: Dict[str, Any], timeout: float, headers: List[str]) -> str:
    data = json.dumps(payload, ensure_ascii=False)
    parts = [
        "curl",
        "-sS",
        "-L",
        "--max-time",
        str(max(timeout, 1e-3)),
        "-H",
        "Content-Type: application/json",
        "-H",
        "Authorization: Bearer ${CHUTES_API_KEY}",
    ]
    for header in headers:
        parts.extend(["-H", header])
    parts.extend([
        "-X",
        "POST",
        "--data-binary",
        data,
        endpoint,
    ])
    return " ".join(shlex.quote(part) for part in parts)


def main(argv: List[str] | None = None) -> int:
    load_dotenv(find_dotenv(), override=False)
    if shutil.which("curl") is None:
        raise SystemExit("curl binary not found on PATH. Install curl to use this script.")

    argv = argv or []
    if not argv:
        argv = ["--execute"]

    default_timeout = _env_float("SCILLM_SANITY_TIMEOUT_S", 30.0)
    parser = argparse.ArgumentParser(
        description="Chutes experimental JSON sanity via curl (no SciLLM dependency)"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="List probe payloads without executing")
    mode.add_argument("--execute", action="store_true", help="Perform live requests (default)")
    parser.add_argument(
        "--request-timeout-s",
        type=float,
        default=default_timeout,
        help="curl --max-time value per request",
    )
    parser.add_argument(
        "--endpoint-path",
        default="/chat/completions",
        help="Relative path appended to CHUTES_API_BASE (default: /chat/completions)",
    )
    parser.add_argument("--model", dest="model_override", help="Override CHUTES_EXPERIMENTAL for this run")
    parser.add_argument("--verbose", action="store_true", help="Print per-scenario progress")
    parser.add_argument(
        "--verbose-json",
        action="store_true",
        help="Print the full JSON response body for each scenario",
    )
    parser.add_argument("--json-summary", action="store_true", help="Print machine-readable JSON summary")
    parser.add_argument("--details", action="store_true", help="Show per-scenario PASS/FAIL rows")
    parser.add_argument(
        "--json-sanitize",
        dest="json_sanitize",
        action="store_true",
        default=os.getenv("SCILLM_JSON_SANITIZE", "0").lower() in {"1", "true", "yes", "on"},
        help="Attempt to repair JSON responses on parse failure",
    )
    parser.add_argument("--no-json-sanitize", dest="json_sanitize", action="store_false")
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        help="Additional HTTP header (key: value). May be repeated.",
    )
    parser.add_argument(
        "--print-curl",
        action="store_true",
        help="Show the curl command used for each scenario (Authorization header masked)",
    )

    args = parser.parse_args(argv)
    if not args.dry_run and not args.execute:
        args.execute = True

    base = os.environ.get("CHUTES_API_BASE", "").strip()
    key = os.environ.get("CHUTES_API_KEY", "").strip()
    model_name = (args.model_override or os.environ.get("CHUTES_EXPERIMENTAL", "")).strip()
    if not base or not key or not model_name:
        raise SystemExit("Missing CHUTES_API_BASE, CHUTES_API_KEY, or CHUTES_EXPERIMENTAL environment variables.")

    endpoint = f"{base.rstrip('/')}{args.endpoint_path if args.endpoint_path.startswith('/') else '/' + args.endpoint_path}"
    system_prompt = "You must respond with strictly valid JSON that satisfies the requested schema."
    scenario_defs = _scenario_definitions(system_prompt)

    requests: List[Dict[str, Any]] = []
    for entry in scenario_defs:
        req = {
            "model": model_name,
            "messages": entry["messages"],
            "response_format": entry["response_format"],
            "max_tokens": entry["max_tokens"],
            "temperature": entry["temperature"],
        }
        requests.append({"scenario": entry["scenario"], "payload": req})

    if args.dry_run and not args.execute:
        preview = {
            "mode": "dry-run",
            "count": len(requests),
            "model": model_name,
            "endpoint": endpoint,
            "scenarios": [item["scenario"] for item in requests],
        }
        if args.print_curl:
            preview["curl_examples"] = {
                item["scenario"]: _format_curl_preview(endpoint, item["payload"], args.request_timeout_s, args.header)
                for item in requests
            }
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0

    items: List[Dict[str, Any]] = []
    success = 0
    last_error = None
    start = time.time()

    for idx, entry in enumerate(requests):
        scenario = entry["scenario"]
        payload = entry["payload"]
        if args.print_curl or args.verbose:
            preview_cmd = _format_curl_preview(endpoint, payload, args.request_timeout_s, args.header)
            if args.print_curl:
                print(f"CURL {scenario}: {preview_cmd}")
        result = _invoke_curl(endpoint, key, payload, args.request_timeout_s, args.header)
        content_head = None
        parsed_payload = None
        reason = None
        ok = result["ok"]
        content_text = None
        meta_response = None

        if not ok:
            reason = result.get("error") or "curl_failed"
        else:
            content_text, extraction_error, meta_response = _extract_message_and_json(result["body"])
            if not content_text:
                ok = False
                reason = extraction_error or "missing_content"
            else:
                try:
                    parsed_payload = json.loads(content_text)
                except json.JSONDecodeError:
                    if args.json_sanitize:
                        repaired = _repair_json_string(content_text)
                        if repaired:
                            try:
                                parsed_payload = json.loads(repaired)
                                content_text = repaired
                            except json.JSONDecodeError:
                                parsed_payload = None
                    if parsed_payload is None:
                        ok = False
                        reason = "invalid_json"
                if parsed_payload is not None and ok:
                    ok, reason = _validate_payload(scenario, parsed_payload)

        if ok:
            success += 1
        else:
            last_error = reason or last_error

        content_head = (content_text or "")[:160].replace("\n", " ") if content_text else (result.get("body", "")[:160].replace("\n", " ") if result.get("body") else None)
        if args.verbose:
            status_label = "OK" if ok else "ERR"
            snippet = content_head or ""
            print(f"SCENARIO {scenario} -> {status_label} {snippet}")
        if args.verbose_json:
            status_label = "PASS" if ok else "FAIL"
            reason_label = "ok" if ok else (reason or "unknown")
            divider = "=" * 24
            print(
                f"\n{divider} {scenario} | {status_label} ({reason_label}) {divider}"
            )
            body_preview = result.get("body") or ""
            print(body_preview if body_preview else "<empty body>")
            print(divider * 2)

        items.append(
            {
                "index": idx,
                "scenario": scenario,
                "ok": ok,
                "reason": reason,
                "curl_status": result.get("status_code"),
                "curl_exit": result.get("returncode"),
                "elapsed_s": round(result.get("elapsed_s", 0.0), 3),
                "content_head": content_head,
            }
        )

    elapsed = round(time.time() - start, 3)
    failure = len(items) - success
    summary = {
        "ok": success == len(items) and (last_error is None),
        "count": len(items),
        "success_count": success,
        "failure_count": failure,
        "error": last_error,
        "model": model_name,
        "endpoint": endpoint,
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
            f"SUMMARY chutes_experimental_json_curl ok={1 if summary['ok'] else 0} "
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
    raise SystemExit(main(sys.argv[1:]))
