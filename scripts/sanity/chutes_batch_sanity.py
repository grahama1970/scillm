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

from scillm import parallel_acompletions
from scillm.extras.json_utils import clean_json_string


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Chutes batch sanity runner with artifact extraction & tenacity.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Plan only: show request artifacts & payload summaries.")
    mode.add_argument("--execute", action="store_true", help="Perform network calls (required for real run).")
    p.add_argument("--tenacious", action="store_true", help="Enable Tenacity retry wrapping for network call.")
    p.add_argument("--max-attempts", type=int, default=3, help="Retry attempts when --tenacious.")
    p.add_argument("--backoff-base", type=float, default=0.5, help="Exponential backoff base seconds.")
    p.add_argument("--backoff-max", type=float, default=8.0, help="Maximum backoff interval seconds.")
    p.add_argument("--wall-time-s", type=float, default=None, help="Override wall time seconds (default/env SCILLM_SANITY_WALL_TIME_S).")
    p.add_argument("--timeout-s", type=float, default=None, help="Override per request timeout seconds (env SCILLM_SANITY_TIMEOUT_S).")
    p.add_argument("--concurrency", type=int, default=None, help="Override parallel request concurrency (env SCILLM_SANITY_CONCURRENCY).")
    p.add_argument("--batch-config", type=str, help="(Reserved) External batch config file path.", default=None)
    return p.parse_args(argv)


async def main(argv: List[str] | None = None) -> int:
    load_dotenv(find_dotenv(), override=False)
    args = parse_args(argv or [])

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

    wall_time_s = args.wall_time_s if args.wall_time_s is not None else _env_float("SCILLM_SANITY_WALL_TIME_S", 1800.0)
    timeout_s = args.timeout_s if args.timeout_s is not None else _env_float("SCILLM_SANITY_TIMEOUT_S", 30.0)
    concurrency = args.concurrency if args.concurrency is not None else _env_int("SCILLM_SANITY_CONCURRENCY", 3)

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
                "max_attempts": args.max_attempts if args.tenacious else 1,
                "backoff_base": args.backoff_base if args.tenacious else None,
                "backoff_max": args.backoff_max if args.tenacious else None,
                "wall_time_s": wall_time_s,
            },
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
    retry_reasons: List[str] = []
    try:
        res = await parallel_acompletions(
            requests,
            model_list=model_list,
            concurrency=concurrency,
            wall_time_s=wall_time_s,
            timeout=timeout_s,
            tenacious=args.tenacious,
            backoff_base=args.backoff_base,
            backoff_cap_s=args.backoff_max,
        )
        last_error = None
    except Exception as e:
        res = []
        last_error = str(e)

    items: List[Dict[str, Any]] = []
    all_ok = True
    if res and len(res) != len(scenarios):
        raise SystemExit(f"Mismatch between request and response counts: {len(res)} vs {len(scenarios)}")

    for idx, r in enumerate(res or []):
        req = r.get("request") or {}
        err = r.get("error")
        raw_content = r.get("content")
        content = ""
        ok = False
        reason = None
        parsed = None
        if err:
            ok = False
            reason = str(err)
        else:
            cleaned_dict = clean_json_string(raw_content or "", return_dict=True)
            if isinstance(cleaned_dict, (dict, list)) and len(json.dumps(cleaned_dict)) > 0:
                parsed = cleaned_dict
                content = json.dumps(cleaned_dict)
                ok = True
            else:
                cleaned_str = clean_json_string(raw_content or "")
                content = cleaned_str if isinstance(cleaned_str, str) else json.dumps(cleaned_str)
                ok = False
                reason = "invalid_json"
        scenario = scenarios[idx]
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
        "attempts_used": 1,
        "retry_reasons": [],
        "retry_config": {
            "max_attempts": args.max_attempts if args.tenacious else 1,
            "backoff_base": args.backoff_base if args.tenacious else None,
            "backoff_max": args.backoff_max if args.tenacious else None,
            "wall_time_s": wall_time_s,
        },
        "error": last_error,
        "elapsed_s": elapsed,
    }
    print(json.dumps(summary, ensure_ascii=False))
    print(
        "SUMMARY "
        f"tenacious={summary['tenacious']} "
        f"attempts_used={summary['attempts_used']} "
        f"retries={len(retry_reasons)} "
        f"elapsed_s={elapsed} "
        f"ok={summary['ok']}"
    )
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(aio.run(main(sys.argv[1:])))
    except RuntimeError:
        loop = aio.get_event_loop()
        raise SystemExit(loop.run_until_complete(main(sys.argv[1:])))
