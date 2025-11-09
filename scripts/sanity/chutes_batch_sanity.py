#!/usr/bin/env python3
from __future__ import annotations

import asyncio as aio
import json
import os
from pathlib import Path
from typing import Any, Dict, List

from dotenv import find_dotenv, load_dotenv

from scillm import parallel_acompletions


async def main() -> int:
    # Load repo-level .env so the script works out-of-the-box for local devs.
    load_dotenv(find_dotenv(), override=False)
    # Allow operators to dial back runtime for debugging without changing defaults.
    def _float_env(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, "").strip() or default)
        except Exception:
            return default

    def _int_env(name: str, default: int) -> int:
        try:
            return int(float(os.getenv(name, "").strip() or default))
        except Exception:
            return default

    wall_time_s = _float_env("SCILLM_SANITY_WALL_TIME_S", 1800.0)
    timeout_s = _float_env("SCILLM_SANITY_TIMEOUT_S", 30.0)
    concurrency = _int_env("SCILLM_SANITY_CONCURRENCY", 3)

    # Ensure local image files can be tested without hosting (explicit demo only)
    os.environ.setdefault("SCILLM_AUTO_IMAGE_DATAURL", "1")
    base = os.environ["CHUTES_API_BASE"].rstrip("/")
    key = os.environ["CHUTES_API_KEY"]
    text_model = os.environ.get("CHUTES_TEXT_MODEL") or os.environ.get("CHUTES_MODEL_ID")
    vlm_model = os.environ.get("CHUTES_VLM_MODEL")
    if not text_model or not vlm_model:
        raise SystemExit("Missing CHUTES_TEXT_MODEL (or CHUTES_MODEL_ID) and/or CHUTES_VLM_MODEL")

    # Fixed model_list (no conditionals)
    model_list: List[Dict[str, Any]] = [
        {"model_name": "chutes/text",
         "litellm_params": {"custom_llm_provider": "openai_like", "model": text_model, "api_base": base, "api_key": key}},
        {"model_name": "chutes/vlm",
         "litellm_params": {"custom_llm_provider": "openai_like", "model": vlm_model, "api_base": base, "api_key": key}},
    ]

    here = Path(__file__).resolve()
    repo_root = here.parents[2]
    sanity_assets = here.parent / "assets"
    html_fixture = sanity_assets / "inline_classification.html"
    image_fixture = repo_root / "docs" / "assets" / "screenshots" / "220px-Giant_Panda_Tai_Shan.JPG"
    html_label = "luminous-harvest"

    if not html_fixture.exists():
        raise SystemExit(f"Missing HTML fixture: {html_fixture}")
    if not image_fixture.exists():
        raise SystemExit(f"Missing image fixture: {image_fixture}")

    requests: List[Dict[str, Any]] = []
    scenarios: List[str] = []

    def add_request(name: str, payload: Dict[str, Any]) -> None:
        requests.append(payload)
        scenarios.append(name)
    # 1) JSON probe
    add_request(
        "json_probe",
        {
            "messages": [
                {"role": "system", "content": "Only respond in well formatted JSON"},
                {"role": "user", "content": "Return only {\"ok\":true} as JSON."},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 16,
            "temperature": 0,
        }
    )
    # 2) France/Paris
    add_request(
        "france_capital",
        {
            "messages": [
                {"role": "system", "content": "Only respond in well formatted JSON"},
                {"role": "user", "content": "What is the capital of France? Respond with {country:<string>, capital:<string>} strictly as JSON."},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 32,
            "temperature": 0,
        }
    )
    # 3) VLM HTTPS image (simple: url key; API expands to image_url)
    add_request(
        "vlm_https_image",
        {
            "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/0/0f/Grosser_Panda.JPG/2560px-Grosser_Panda.JPG",
            "messages": [
                {"role": "system", "content": "Only respond in well formatted JSON"},
                {"role": "user",   "content": "Describe this image as {\"description\":<string>} strictly as JSON."},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 128,
            "temperature": 0.2,
        }
    )
    # 4) Local file path (simple: file_path; API expands; enable data URL with SCILLM_AUTO_IMAGE_DATAURL=1)
    add_request(
        "vlm_file_image",
        {
            "file_path": str(image_fixture),
            "messages": [
                {"role": "system", "content": "Only respond in well formatted JSON"},
                {"role": "user",   "content": "Describe this image as {\"description\":<string>} strictly as JSON."},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 128,
            "temperature": 0.2,
        }
    )
    # 5) Inline HTML classification (verifies html->text expansion)
    add_request(
        "html_inline_classification",
        {
            "file_path": str(html_fixture),
            "messages": [
                {"role": "system", "content": "Only respond in well formatted JSON"},
                {"role": "user", "content": "Read the provided HTML document (appended separately) and return {\"category\":<string>} using the exact 'Classification Label' value."},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 16,
            "temperature": 0,
        }
    )

    res = await parallel_acompletions(
        requests,
        model_list=model_list,
        concurrency=concurrency,
        wall_time_s=wall_time_s,
        timeout=timeout_s,
    )

    items: List[Dict[str, Any]] = []
    all_ok = True
    if len(res) != len(scenarios):
        raise SystemExit(f"Mismatch between request and response counts: {len(res)} vs {len(scenarios)}")

    for idx, r in enumerate(res):
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
            if isinstance(raw_content, str):
                content = raw_content.strip()
                try:
                    parsed = json.loads(content)
                    ok = isinstance(parsed, (dict, list)) and (len(json.dumps(parsed)) > 0)
                except Exception as e:
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
                    ok = isinstance(parsed, (dict, list)) and (len(json.dumps(parsed)) > 0)
                except Exception as e:
                    ok = False
                    reason = f"invalid_json:{e}"
        scenario = scenarios[idx]
        # Semantic checks
        if ok and isinstance(parsed, dict):
            if scenario == "france_capital":
                ctry = str(parsed.get("country") or "").lower()
                cap = str(parsed.get("capital") or "").lower()
                if not (ctry == "france" and cap == "paris"):
                    ok = False
                    reason = f"mismatch:country={ctry},capital={cap}"
            if scenario in {"vlm_https_image", "vlm_file_image"} and vlm_model:  # images
                desc = parsed.get("description")
                if not isinstance(desc, str) or not desc.strip():
                    ok = False
                    reason = "no_description"
            if scenario == "html_inline_classification":
                category = str(parsed.get("category") or "").strip().lower()
                if category != html_label:
                    ok = False
                    reason = f"mismatch:category={category}"
        items.append({
            "index": idx,
            "scenario": scenario,
            "ok": ok,
            "reason": reason,
            "content_head": (content or "")[:160],
            "model_used": req.get("model"),
        })
        all_ok = all_ok and ok

    summary = {"ok": all_ok, "count": len(items), "items": items}
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if all_ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(aio.run(main()))
    except RuntimeError:
        loop = aio.get_event_loop()
        raise SystemExit(loop.run_until_complete(main()))
