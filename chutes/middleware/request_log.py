"""Query helpers for LLM call logs stored in ArangoDB.

Reads from the `llm_call_log` collection (written by ArangoLogMiddleware).
Provides aggregation functions for cost summaries and usage analytics.

No middleware here — just query helpers for /v1/scillm/logs endpoint.
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from loguru import logger

_MEMORY_URL = os.environ.get("MEMORY_URL", "http://127.0.0.1:8601")
_COLLECTION = "llm_call_log"

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=_MEMORY_URL,
            timeout=httpx.Timeout(10.0, connect=2.0),
        )
    return _client


async def get_logs(date: str, limit: int = 1000, offset: int = 0) -> list[dict]:
    """Fetch log records for a given date (YYYY-MM-DD) from ArangoDB."""
    try:
        client = _get_client()
        resp = await client.post(
            "/query",
            json={
                "query": f"""
                    FOR doc IN {_COLLECTION}
                    FILTER doc.date == @date
                    SORT doc.ts DESC
                    LIMIT @offset, @limit
                    RETURN doc
                """,
                "bindVars": {"date": date, "offset": offset, "limit": limit},
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("result", [])
    except Exception as exc:
        logger.debug("request_log: ArangoDB query failed: {}", exc)
    return []


async def get_cost_summary(date: str) -> dict:
    """Aggregate cost by model for a given date using ArangoDB aggregation."""
    try:
        client = _get_client()
        resp = await client.post(
            "/query",
            json={
                "query": f"""
                    FOR doc IN {_COLLECTION}
                    FILTER doc.date == @date
                    COLLECT model = (doc.model_served || doc.model_requested || "unknown")
                    AGGREGATE
                        requests = COUNT(1),
                        prompt_tokens = SUM(doc.prompt_tokens || 0),
                        completion_tokens = SUM(doc.completion_tokens || 0),
                        total_tokens = SUM(doc.total_tokens || 0),
                        cost_usd = SUM(doc.cost_usd || 0),
                        errors = SUM(doc.status == "error" ? 1 : 0),
                        cache_hits = SUM(doc.cache_hit ? 1 : 0)
                    RETURN {{
                        model: model,
                        requests: requests,
                        prompt_tokens: prompt_tokens,
                        completion_tokens: completion_tokens,
                        total_tokens: total_tokens,
                        cost_usd: cost_usd,
                        errors: errors,
                        cache_hits: cache_hits
                    }}
                """,
                "bindVars": {"date": date},
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("result", [])

            by_model = {}
            total_cost = 0.0
            total_requests = 0
            for row in results:
                model = row.get("model", "unknown")
                by_model[model] = {
                    "requests": row.get("requests", 0),
                    "prompt_tokens": row.get("prompt_tokens", 0),
                    "completion_tokens": row.get("completion_tokens", 0),
                    "total_tokens": row.get("total_tokens", 0),
                    "cost_usd": row.get("cost_usd", 0.0),
                    "errors": row.get("errors", 0),
                    "cache_hits": row.get("cache_hits", 0),
                }
                total_cost += row.get("cost_usd", 0.0)
                total_requests += row.get("requests", 0)

            return {
                "date": date,
                "total_requests": total_requests,
                "total_cost_usd": round(total_cost, 6),
                "by_model": by_model,
            }
    except Exception as exc:
        logger.debug("request_log: ArangoDB aggregation failed: {}", exc)

    return {
        "date": date,
        "total_requests": 0,
        "total_cost_usd": 0.0,
        "by_model": {},
    }
