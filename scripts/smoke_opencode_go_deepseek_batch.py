#!/usr/bin/env python3
"""Smoke: live OpenCode Go DeepSeek V4 batch via scillm using as_completed.

Env (optional):
- PROXY_BASE (default: http://127.0.0.1:4001)
- MASTER_KEY (default: sk-dev-proxy-123)
- OPENCODE_GO_DEEPSEEK_MODEL (default: prefer opencode-go/deepseek-v4-pro)
- SCILLM_BATCH_CONCURRENCY (default: 4)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

import httpx


QUESTIONS = [
    {"id": "capital_france", "prompt": "What is the capital of France? Answer concisely."},
    {"id": "two_plus_two", "prompt": "What is 2 + 2? Answer concisely."},
    {"id": "mustard_color", "prompt": "Why is mustard yellow'ish in color? Answer concisely."},
]


def _headers(master_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {master_key}",
        "X-Caller-Skill": "scillm-opencode-go-deepseek-batch-smoke",
    }


async def fetch_live_models(client: httpx.AsyncClient, base_url: str, headers: dict[str, str]) -> dict[str, Any]:
    response = await client.get(
        f"{base_url}/v1/scillm/opencode-go/models",
        headers=headers,
        params={"refresh": "true"},
        timeout=60.0,
    )
    response.raise_for_status()
    listing = response.json()
    if listing.get("source") != "cli":
        raise RuntimeError(f"OpenCode Go model discovery was not live CLI source: {json.dumps(listing)[:1000]}")
    return listing


def select_deepseek_v4_model(listing: dict[str, Any]) -> str:
    configured = [
        model["id"]
        for model in listing.get("models", [])
        if model.get("id", "").startswith("opencode-go/deepseek-v4-")
        and model.get("supported")
        and model.get("key_configured")
    ]
    override = os.getenv("OPENCODE_GO_DEEPSEEK_MODEL")
    if override:
        if override not in configured:
            raise RuntimeError(f"Requested OPENCODE_GO_DEEPSEEK_MODEL is unavailable: {override}")
        return override
    if "opencode-go/deepseek-v4-pro" in configured:
        return "opencode-go/deepseek-v4-pro"
    if configured:
        return configured[0]
    raise RuntimeError("No configured OpenCode Go DeepSeek V4 model found")


async def ask_one(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    base_url: str,
    headers: dict[str, str],
    model: str,
    batch_id: str,
    question: dict[str, str],
) -> dict[str, Any]:
    started = time.monotonic()
    async with semaphore:
        try:
            response = await client.post(
                f"{base_url}/v1/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": question["prompt"]}],
                    "temperature": 0,
                    "scillm_metadata": {"batch_id": batch_id, "item_id": question["id"]},
                },
                timeout=180.0,
            )
            response.raise_for_status()
            data = response.json()
            return {
                "id": question["id"],
                "ok": True,
                "answer": data["choices"][0]["message"]["content"],
                "served_model": data.get("model"),
                "metadata": data.get("scillm_metadata"),
                "batch_resumed": response.headers.get("x-batch-resumed") == "true",
                "elapsed_s": round(time.monotonic() - started, 2),
            }
        except Exception as exc:
            return {
                "id": question["id"],
                "ok": False,
                "error": str(exc),
                "elapsed_s": round(time.monotonic() - started, 2),
            }


async def run_batch() -> dict[str, Any]:
    base_url = os.getenv("PROXY_BASE", "http://127.0.0.1:4001").rstrip("/")
    master_key = os.getenv("MASTER_KEY", os.getenv("LITELLM_MASTER_KEY", "sk-dev-proxy-123"))
    concurrency = int(os.getenv("SCILLM_BATCH_CONCURRENCY", "4"))
    batch_id = f"opencode-go-deepseek-v4-smoke-{int(time.time())}"
    headers = _headers(master_key)

    async with httpx.AsyncClient() as client:
        listing = await fetch_live_models(client, base_url, headers)
        selected_model = select_deepseek_v4_model(listing)
        semaphore = asyncio.Semaphore(concurrency)
        tasks = [
            asyncio.create_task(ask_one(client, semaphore, base_url, headers, selected_model, batch_id, question))
            for question in QUESTIONS
        ]

        results: list[dict[str, Any]] = []
        completion_order: list[str] = []
        for task in asyncio.as_completed(tasks):
            result = await task
            completion_order.append(result["id"])
            results.append(result)

    return {
        "ok": all(result.get("ok") for result in results),
        "proxy_base": base_url,
        "selected_model": selected_model,
        "model_listing_source": listing.get("source"),
        "model_count": len(listing.get("models", [])),
        "listing_errors": listing.get("errors", []),
        "batch_id": batch_id,
        "concurrency": concurrency,
        "completion_order": completion_order,
        "results": results,
    }


def main() -> int:
    try:
        report = asyncio.run(run_batch())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
