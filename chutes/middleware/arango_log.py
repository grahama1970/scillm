"""ArangoDB call log middleware — writes every LLM completion to memory daemon.

Creates documents in the `llm_call_log` collection via /upsert, enabling:
- /learn-timeout to estimate p95 latency per model/provider
- /orchestrate to diagnose failures and self-correct
- /analytics to track cost and token trends
- Debugging failed batches via raw request/response content

Also writes to JSONL backup on disk — independent of database, survives wipes.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from scillm.proxy.middleware import BaseMiddleware

_MEMORY_URL = os.environ.get("MEMORY_URL", "http://127.0.0.1:8601")
_COLLECTION = "llm_call_log"

# JSONL backup path — independent of database, survives DB wipes
_JSONL_BACKUP_DIR = Path(os.environ.get(
    "SCILLM_LOG_BACKUP_DIR",
    "/mnt/storage12tb/scillm-logs"
))

# Lazy-init httpx client (reuses connection pool)
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=_MEMORY_URL,
            timeout=httpx.Timeout(5.0, connect=2.0),
        )
    return _client


import uuid as _uuid

def _make_key(ts_iso: str, model: str) -> str:
    """Unique _key per request — includes UUID suffix to avoid batch collisions."""
    raw = f"{ts_iso}:{model}:{_uuid.uuid4().hex[:8]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _write_jsonl_backup(doc: dict, now: datetime) -> None:
    """Append doc to daily JSONL file — independent backup, survives DB wipes.

    Path: /mnt/storage12tb/scillm-logs/2026-04/calls-2026-04-18.jsonl
    """
    try:
        month_dir = _JSONL_BACKUP_DIR / now.strftime("%Y-%m")
        month_dir.mkdir(parents=True, exist_ok=True)

        log_file = month_dir / f"calls-{now.strftime('%Y-%m-%d')}.jsonl"

        # Append-only write — one JSON object per line
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(doc, default=str, ensure_ascii=False) + "\n")
    except Exception as exc:
        # Never crash the request for backup failure — just log
        logger.warning("arango_log: JSONL backup failed: {}", exc)


class ArangoLogMiddleware(BaseMiddleware):
    """Log every LLM call to ArangoDB for /learn-timeout and /orchestrate."""

    async def pre_call(self, request: dict) -> dict:
        request["_arango_log_start"] = time.monotonic()
        return request

    async def post_call(self, request: dict, response: Any) -> Any:
        # Skip cache hits — they're not real provider calls, would skew timeout estimates
        if request.get("_cache_hit"):
            return response
        try:
            await self._write(request, response, error=None)
        except Exception as exc:
            logger.debug("arango_log: post_call write failed: {}", exc)
        return response

    async def on_error(self, request: dict, error: Exception) -> None:
        try:
            # Include both error type and message for debugging
            error_str = f"{type(error).__name__}: {str(error)[:500]}"
            await self._write(request, {}, error=error_str)
        except Exception as exc:
            logger.debug("arango_log: on_error write failed: {}", exc)

    async def _write(
        self, request: dict, response: Any, *, error: str | None
    ) -> None:
        now = datetime.now(timezone.utc)
        start = request.get("_arango_log_start")
        duration_ms = (
            round((time.monotonic() - start) * 1000)
            if start is not None
            else None
        )

        usage = {}
        model_served = request.get("model", "")
        cost_usd = None
        response_content = None

        if isinstance(response, dict) and not response.get("stream"):
            usage = response.get("usage", {})
            model_served = response.get("model", model_served)
            cost_headers = response.get("_cost_headers", {})
            raw_cost = cost_headers.get("x-cost-usd", "")
            if raw_cost and raw_cost != "unknown":
                try:
                    cost_usd = float(raw_cost)
                except ValueError:
                    pass
            # Extract raw response content for debugging
            choices = response.get("choices", [])
            if choices and isinstance(choices[0], dict):
                msg = choices[0].get("message", {})
                if isinstance(msg, dict):
                    response_content = msg.get("content")

        # Extract request messages (last message only to limit size)
        messages = request.get("messages", [])
        last_user_message = None
        if messages:
            for m in reversed(messages):
                if m.get("role") == "user":
                    content = m.get("content", "")
                    # Truncate very long messages
                    if isinstance(content, str) and len(content) > 4000:
                        content = content[:4000] + "...[truncated]"
                    last_user_message = content
                    break

        # Extract caller identity — prefer x-caller-skill, fall back to other headers
        headers = request.get("_headers", {})
        caller_skill = request.get("_caller_skill", "") or headers.get("x-caller-skill", "")

        # Build caller_info dict for debugging when x-caller-skill missing
        caller_info = {}
        if not caller_skill:
            # Capture any identifying info when skill header missing
            user_agent = headers.get("user-agent", "")
            if user_agent:
                caller_info["user_agent"] = user_agent[:200]  # Truncate long UAs
            # Check for custom headers that might identify the caller
            for h in ["x-request-id", "x-client-id", "x-correlation-id"]:
                if headers.get(h):
                    caller_info[h] = headers[h][:100]

        doc = {
            "_key": _make_key(now.isoformat(), model_served),
            "ts": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"),
            "task_type": "llm_api_call",
            "model_requested": request.get("model", ""),
            "model_served": model_served,
            "provider": _infer_provider(model_served),
            "duration_ms": duration_ms,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "cost_usd": cost_usd,
            "stream": bool(request.get("stream")),
            # cache_hit field removed — cache hits are not logged (they skip _write)
            "status": "error" if error else "ok",
            "error": error,
            "caller": caller_skill,
            "caller_info": caller_info if caller_info else None,  # Only include if we have fallback info
            # scillm_metadata for work item correlation (skill passes batch_id, doc_key, etc.)
            # Stashed as _scillm_metadata by app.py to survive routing
            "metadata": request.get("_scillm_metadata", {}),
            # Raw content for debugging (can prune later)
            "request_prompt": last_user_message,
            "response_content": response_content,
        }

        # JSONL backup FIRST — survives DB wipes, append-only, can't be corrupted by agents
        _write_jsonl_backup(doc, now)

        # Then write to ArangoDB (may fail if DB is down/wiped)
        client = _get_client()
        resp = await client.post(
            "/upsert",
            json={"collection": _COLLECTION, "documents": [doc]},
        )
        if resp.status_code >= 400:
            logger.debug(
                "arango_log: /upsert returned {}: {}",
                resp.status_code,
                resp.text[:200],
            )


def _infer_provider(model: str) -> str:
    """Infer provider name from model string."""
    m = model.lower()
    if "deepseek" in m:
        return "chutes" if "v3" in m or "r1" in m else "deepseek"
    if "gemini" in m or "flash" in m:
        return "google"
    if "qwen" in m:
        return "chutes"
    if "glm" in m:
        return "zhipu"
    if "kimi" in m or "moonshot" in m:
        return "moonshot"
    if "claude" in m:
        return "anthropic"
    if "gpt" in m or "codex" in m:
        return "openai"
    return "unknown"
