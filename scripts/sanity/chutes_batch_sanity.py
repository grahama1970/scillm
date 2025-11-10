#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio as aio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from dotenv import find_dotenv, load_dotenv

from scillm import parallel_acompletions_iter
from scillm.extras.json_utils import clean_json_string
from scillm.batch import _extract_content_from_response


async def main(argv: List[str] | None = None) -> int:
    load_dotenv(find_dotenv(), override=False)
    argv = argv or []

    def _env_float(name: str, fallback: float) -> float:
        raw = os.getenv(name, "").strip()
        if not raw:
            return fallback
        try:
            return float(raw)
        except Exception:
            return fallback

    def _env_int(name: str, fallback: int) -> int:
        raw = os.getenv(name, "").strip()
        if not raw:
            return fallback
        try:
            return int(float(raw))
        except Exception:
            return fallback

    default_wall = _env_float("SCILLM_SANITY_WALL_TIME_S", 1800.0)
    default_timeout = _env_float("SCILLM_SANITY_TIMEOUT_S", 30.0)
    default_concurrency = _env_int("SCILLM_SANITY_CONCURRENCY", 3)

    parser = argparse.ArgumentParser(description="Chutes batch sanity (5 probes: JSON, text, VLM URL, VLM file, HTML)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Plan only: show artifacts & payload summaries")
    mode.add_argument("--execute", action="store_true", help="Perform network calls (required for real run)")
    parser.add_argument("--tenacious", dest="tenacious", action="store_true", help="Enable client-side retries/backoff within wall time")
    parser.add_argument("--no-tenacious", dest="tenacious", action="store_false", help="Disable client-side retries/backoff")
    parser.set_defaults(tenacious=True)
    parser.add_argument("--backoff-base", type=float, default=_env_float("SCILLM_BACKOFF_BASE", 0.5), help="Exponential backoff base seconds")
    parser.add_argument("--backoff-cap-s", type=float, default=_env_float("SCILLM_BACKOFF_CAP_S", 30.0), help="Max backoff interval seconds")
    parser.add_argument("--wall-time-s", type=float, default=default_wall, help="Overall wall time budget seconds")
    parser.add_argument("--timeout-s", type=float, default=default_timeout, help="Per-request provider timeout seconds")
    parser.add_argument("--concurrency", type=int, default=default_concurrency, help="Parallel request concurrency")
    json_sanitize_default = os.getenv("SCILLM_JSON_SANITIZE", "0").lower() in {"1", "true", "yes", "on"}
    parser.add_argument("--json-sanitize", dest="json_sanitize", action="store_true", default=json_sanitize_default,
                        help="Attempt to repair near-JSON outputs before failing strict parsing")
    parser.add_argument("--verbose", action="store_true", help="Print per-scenario progress and retry events")
    parser.add_argument("--no-json-sanitize", dest="json_sanitize", action="store_false", help="Disable repair attempts even if env enabled")
    if not argv:
        argv = ["--execute"]
    args = parser.parse_args(argv)

    wall_time_s = args.wall_time_s
    timeout_s = args.timeout_s
    concurrency = args.concurrency

    # Ensure local image auto-conversion
    os.environ.setdefault("SCILLM_AUTO_IMAGE_DATAURL", "1")
    base = os.environ.get("CHUTES_API_BASE", "").rstrip("/")
    key = os.environ.get("CHUTES_API_KEY")
    text_model = os.environ.get("CHUTES_TEXT_MODEL") or os.environ.get("CHUTES_MODEL_ID")
    vlm_model = os.environ.get("CHUTES_VLM_MODEL")
    if not base or not key or not text_model or not vlm_model:
        raise SystemExit("Missing required CHUTES_* environment variables (API base/key/models).")

    here = Path(__file__).resolve()
    repo_root = here.parents[2]
    sanity_assets = here.parent / "assets"
    html_fixture = sanity_assets / "inline_classification.html"
    image_fixture = repo_root / "docs" / "assets" / "screenshots" / "220px-Giant_Panda_Tai_Shan.JPG"
    if not html_fixture.exists():
        raise SystemExit(f"Missing HTML fixture: {html_fixture}")
    if not image_fixture.exists():
        raise SystemExit(f"Missing image fixture: {image_fixture}")

    html_label = "luminous-harvest"
    image_url = "https://upload.wikimedia.org/wikipedia/commons/thumb/0/0f/Grosser_Panda.JPG/2560px-Grosser_Panda.JPG"
    file_img_path = str(image_fixture)
    html_path = str(html_fixture)

    batch_requests: List[Dict[str, Any]] = [
        {
            "scenario": "json_probe",
            "request": {
                "messages": [
                    {"role": "system", "content": "Only respond in well formatted JSON"},
                    {"role": "user", "content": "Return only {\"ok\":true} as JSON."},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": 16,
                "temperature": 0,
                "artifacts": {"file_paths": [], "urls": []},
            },
        },
        {
            "scenario": "france_capital",
            "request": {
                "messages": [
                    {"role": "system", "content": "Only respond in well formatted JSON"},
                    {
                        "role": "user",
                        "content": "What is the capital of France? Respond with {country:<string>, capital:<string>} strictly as JSON.",
                    },
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": 32,
                "temperature": 0,
                "artifacts": {"file_paths": [], "urls": []},
            },
        },
        {
            "scenario": "vlm_https_image",
            "request": {
                "url": image_url,
                "messages": [
                    {"role": "system", "content": "Only respond in well formatted JSON"},
                    {
                        "role": "user",
                        "content": (
                            "Please describe the image at "
                            f"{image_url}"
                            " as {\"description\":<string>} strictly as JSON."
                        ),
                    },
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": 128,
                "temperature": 0.2,
                "artifacts": {"file_paths": [], "urls": [image_url]},
            },
        },
        {
            "scenario": "vlm_file_image",
            "request": {
                "file_path": file_img_path,
                "messages": [
                    {"role": "system", "content": "Only respond in well formatted JSON"},
                    {
                        "role": "user",
                        "content": (
                            "Describe the local image located at "
                            f"{file_img_path}"
                            " as {\"description\":<string>} strictly as JSON."
                        ),
                    },
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": 128,
                "temperature": 0.2,
                "artifacts": {"file_paths": [file_img_path], "urls": []},
            },
        },
        {
            "scenario": "html_inline_classification",
            "request": {
                "file_path": html_path,
                "messages": [
                    {"role": "system", "content": "Only respond in well formatted JSON"},
                    {
                        "role": "user",
                        "content": (
                            "Read the provided HTML document located at "
                            f"{html_path}"
                            " (appended separately) and return {\"category\":<string>} using the exact 'Classification Label' value."
                        ),
                    },
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": 16,
                "temperature": 0,
                "artifacts": {"file_paths": [html_path], "urls": []},
            },
        },
    ]

    scenarios = [entry["scenario"] for entry in batch_requests]
    requests = [entry["request"] for entry in batch_requests]

    # Preview / dry-run mode (default if --execute not supplied)
    if args.dry_run or not args.execute:
        preview_items = []
        for entry in batch_requests:
            payload = entry["request"]
            artifacts = payload.get("artifacts") or {}
            user_msg = ""
            for m in payload.get("messages", []):
                if m.get("role") == "user":
                    user_msg = m.get("content", "")
                    break
            preview_items.append({
                "scenario": entry["scenario"],
                "file_paths": artifacts.get("file_paths"),
                "urls": artifacts.get("urls"),
                "user_message": user_msg,
            })
        preview = {
            "mode": "dry-run",
            "tenacious": bool(args.tenacious),
            "retry_config": {
                "backoff_base": args.backoff_base if args.tenacious else None,
                "backoff_cap_s": args.backoff_cap_s if args.tenacious else None,
                "wall_time_s": wall_time_s,
            },
            "json_sanitize": bool(args.json_sanitize),
            "count": len(preview_items),
            "items": preview_items,
        }
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0

    model_list = [
        {
            "model_name": "chutes/text",
            "litellm_params": {
                "custom_llm_provider": "openai_like",
                "model": text_model,
                "api_base": base,
                "api_key": key,
            },
        },
        {
            "model_name": "chutes/vlm",
            "litellm_params": {
                "custom_llm_provider": "openai_like",
                "model": vlm_model,
                "api_base": base,
                "api_key": key,
            },
        },
    ]

    start = time.time()
    if args.verbose:
        print("INFO running parallel_acompletions_iter", flush=True)
    raw_results: List[Dict[str, Any] | None] = [None] * len(requests)
    last_error = None
    try:
        async for entry in parallel_acompletions_iter(
            requests,
            model_list=model_list,
            concurrency=concurrency,
            wall_time_s=wall_time_s,
            timeout=timeout_s,
            tenacious=args.tenacious,
            backoff_base=args.backoff_base,
            backoff_cap_s=args.backoff_cap_s,
        ):
            idx = entry.get("index", 0)
            req = entry.get("request") or requests[idx]
            ok = bool(entry.get("ok")) and not entry.get("error")
            err_msg = entry.get("error")
            if err_msg:
                last_error = err_msg
            status = "OK" if ok else "ERR"
            note = f" ({err_msg})" if err_msg else ""
            if args.verbose:
                scenario = scenarios[idx]
                print(f"SCENARIO {scenario} -> {status}{note}", flush=True)
            if ok:
                resp = entry.get("response")
                content = _extract_content_from_response(resp)
                raw_results[idx] = {
                    "request": req,
                    "response": resp,
                    "error": None,
                    "content": content,
                }
            else:
                raw_results[idx] = {
                    "request": req,
                    "response": None,
                    "error": err_msg or "unknown_error",
                    "content": "",
                }
    except Exception as e:
        last_error = str(e)
        raw_results = []

    items: List[Dict[str, Any]] = []
    all_ok = True
    if not raw_results or None in raw_results:
        raise SystemExit("Missing results from parallel_acompletions_iter")

    for idx, r in enumerate(raw_results):
        req = r.get("request") or {}
        err = r.get("error")
        raw_content = r.get("content")
        content = ""
        ok = False
        reason = None
        parsed = None
        scenario = scenarios[idx]
        if err:
            ok = False
            reason = str(err)
        else:
            if isinstance(raw_content, str):
                content = raw_content.strip()
                try:
                    parsed = json.loads(content)
                    ok = isinstance(parsed, (dict, list)) and len(json.dumps(parsed)) > 0
                except Exception as e:
                    if args.json_sanitize:
                        try:
                            parsed_candidate = clean_json_string(content, return_dict=True)
                        except Exception:
                            parsed_candidate = None
                        if isinstance(parsed_candidate, (dict, list)) and len(json.dumps(parsed_candidate)) > 0:
                            parsed = parsed_candidate
                            content = json.dumps(parsed_candidate)
                            ok = True
                        else:
                            ok = False
                            reason = "invalid_json:sanitize_failed"
                    else:
                        ok = False
                        reason = f"invalid_json:{e}"
            elif isinstance(raw_content, (dict, list)):
                parsed = raw_content
                content = json.dumps(raw_content)
                ok = True
            elif raw_content is None:
                ok = False
                reason = "empty_content"
            else:
                content = str(raw_content)
                try:
                    parsed = json.loads(content)
                    ok = isinstance(parsed, (dict, list)) and len(json.dumps(parsed)) > 0
                except Exception as e:
                    ok = False
                    reason = f"invalid_json:{e}"
        if ok and isinstance(parsed, dict):
            if scenario == "france_capital":
                ctry = str(parsed.get("country") or "").lower()
                cap = str(parsed.get("capital") or "").lower()
                if not (ctry == "france" and cap == "paris"):
                    ok = False
                    reason = f"mismatch:country={ctry},capital={cap}"
            if scenario in {"vlm_https_image", "vlm_file_image"} and vlm_model:
                desc = parsed.get("description")
                if not isinstance(desc, str) or not desc.strip():
                    ok = False
                    reason = "no_description"
            if scenario == "html_inline_classification":
                category = str(parsed.get("category") or "").strip().lower()
                if category != html_label:
                    ok = False
                    reason = f"mismatch:category={category}"
        req_artifacts = req.get("artifacts")
        if not req_artifacts and idx < len(requests):
            req_artifacts = requests[idx].get("artifacts")
        items.append({
            "index": idx,
            "scenario": scenario,
            "ok": ok,
            "reason": reason,
            "content_head": (content or "")[:160],
            "model_used": req.get("model"),
            "artifacts": req_artifacts,
        })
        all_ok = all_ok and ok

    elapsed = round(time.time() - start, 3)
    summary = {
        "ok": all_ok and last_error is None,
        "count": len(items),
        "items": items,
        "tenacious": bool(args.tenacious),
        "error": last_error,
        "elapsed_s": elapsed,
    }
    print(json.dumps(summary, ensure_ascii=False))
    print(f"SUMMARY chutes_batch_sanity ok={1 if summary['ok'] else 0} count={len(items)} elapsed_s={elapsed}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(aio.run(main(sys.argv[1:])))
    except RuntimeError:
        loop = aio.get_event_loop()
        raise SystemExit(loop.run_until_complete(main(sys.argv[1:])))



"""
python scripts/sanity/chutes_batch_sanity.py \
--execute --tenacious \
--backoff-base 0.5 \
--backoff-cap-s 30 \
--timeout-s 45 \
--wall-time-s 120
"""
