"""Structured request logging middleware — writes every completion to Redis.

Appends a compact JSON record per request to a Redis list keyed by date:
    scillm:log:2026-03-13  →  [record, record, ...]

Each record captures: request_id, model, tokens, cost, latency, cache_hit,
status (ok/error), and timestamp.  No message content is stored.

Query logs via:
    LRANGE scillm:log:2026-03-13 0 -1

Or use `make costs` to aggregate by provider/model/day.

Falls back to JSONL file if Redis is unavailable.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from scillm.proxy.middleware import BaseMiddleware

# Redis connection reused from cache_init (same REDIS_HOST env vars)
_redis = None
_fallback_path: Path | None = None
_LOG_TTL_DAYS = int(os.environ.get("SCILLM_LOG_TTL_DAYS", "90"))


async def _get_redis():
    """Lazy-init Redis connection (shares config with cache_init)."""
    global _redis
    if _redis is not None:
        return _redis

    redis_url = os.environ.get("REDIS_URL", "").strip()
    redis_host = os.environ.get("REDIS_HOST", "").strip()

    if not redis_url and not redis_host:
        return None

    try:
        import redis.asyncio as aioredis

        if redis_url:
            _redis = aioredis.from_url(redis_url, socket_timeout=3, decode_responses=True)
        else:
            port = int(os.environ.get("REDIS_PORT", "6379"))
            db = int(os.environ.get("REDIS_DB", "0"))
            password = os.environ.get("REDIS_PASSWORD", "").strip() or None
            _redis = aioredis.Redis(
                host=redis_host, port=port, db=db,
                password=password, socket_timeout=3, decode_responses=True,
            )
        await _redis.ping()
        return _redis
    except Exception as exc:
        logger.debug("request_log: Redis not reachable ({}), using JSONL fallback", exc)
        _redis = None
        return None


def _get_fallback_path() -> Path:
    """JSONL fallback when Redis is unavailable."""
    global _fallback_path
    if _fallback_path is None:
        log_dir = Path(os.environ.get("SCILLM_LOG_DIR", "local/logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        _fallback_path = log_dir / "request_log.jsonl"
    return _fallback_path


def _build_record(
    request: dict,
    response: Any,
    *,
    error: str | None = None,
) -> dict:
    """Build a compact log record from request + response."""
    now = datetime.now(timezone.utc)

    usage = {}
    cost_usd = None
    model_served = request.get("model", "")

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

    return {
        "ts": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "model_requested": request.get("model", ""),
        "model_served": model_served,
        "stream": bool(request.get("stream")),
        "cache_hit": bool(request.get("_cache_hit")),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "cost_usd": cost_usd,
        "status": "error" if error else "ok",
        "error": error,
    }


class RequestLogMiddleware(BaseMiddleware):
    """Log every request to Redis (or JSONL fallback). Observation-only — never modifies request or response."""

    async def post_call(self, request: dict, response: Any) -> Any:
        try:
            record = _build_record(request, response)
            await _write_record(record)
        except Exception as exc:
            logger.debug("request_log: failed to write: {}", exc)
        return response

    async def on_error(self, request: dict, error: Exception) -> None:
        try:
            record = _build_record(request, {}, error=type(error).__name__)
            await _write_record(record)
        except Exception as exc:
            logger.debug("request_log: failed to write error record: {}", exc)


async def _write_record(record: dict) -> None:
    """Write record to Redis list or JSONL fallback."""
    date_key = f"scillm:log:{record['date']}"
    line = json.dumps(record, separators=(",", ":"))

    r = await _get_redis()
    if r is not None:
        try:
            pipe = r.pipeline()
            pipe.rpush(date_key, line)
            # Set TTL on first write (idempotent — won't shorten existing TTL)
            pipe.expire(date_key, _LOG_TTL_DAYS * 86400, nx=True)
            await pipe.execute()
            return
        except Exception as exc:
            logger.debug("request_log: Redis write failed ({}), falling back to JSONL", exc)

    # JSONL fallback
    path = _get_fallback_path()
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------------------
# Query helpers (used by `make costs` and /v1/scillm/logs)
# ---------------------------------------------------------------------------


async def get_logs(date: str, limit: int = 1000, offset: int = 0) -> list[dict]:
    """Fetch log records for a given date (YYYY-MM-DD)."""
    r = await _get_redis()
    if r is not None:
        try:
            raw = await r.lrange(f"scillm:log:{date}", offset, offset + limit - 1)
            return [json.loads(line) for line in raw]
        except Exception:
            pass

    # JSONL fallback
    path = _get_fallback_path()
    if not path.exists():
        return []
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
                if rec.get("date") == date:
                    records.append(rec)
            except Exception:
                continue
    return records[offset : offset + limit]


async def get_cost_summary(date: str) -> dict:
    """Aggregate cost by model for a given date."""
    records = await get_logs(date, limit=100_000)
    by_model: dict[str, dict] = {}
    for rec in records:
        model = rec.get("model_served") or rec.get("model_requested") or "unknown"
        entry = by_model.setdefault(model, {
            "requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "total_tokens": 0, "cost_usd": 0.0, "errors": 0, "cache_hits": 0,
        })
        entry["requests"] += 1
        entry["prompt_tokens"] += rec.get("prompt_tokens", 0)
        entry["completion_tokens"] += rec.get("completion_tokens", 0)
        entry["total_tokens"] += rec.get("total_tokens", 0)
        if rec.get("cost_usd") is not None:
            entry["cost_usd"] += rec["cost_usd"]
        if rec.get("status") == "error":
            entry["errors"] += 1
        if rec.get("cache_hit"):
            entry["cache_hits"] += 1

    total_cost = sum(e["cost_usd"] for e in by_model.values())
    total_requests = sum(e["requests"] for e in by_model.values())

    return {
        "date": date,
        "total_requests": total_requests,
        "total_cost_usd": round(total_cost, 6),
        "by_model": by_model,
    }
