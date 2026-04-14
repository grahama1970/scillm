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


async def get_log_by_key(key: str) -> dict | None:
    """Fetch a single log record by _key."""
    try:
        client = _get_client()
        resp = await client.post(
            "/query",
            json={
                "query": f"FOR doc IN {_COLLECTION} FILTER doc._key == @key RETURN doc",
                "bindVars": {"key": key},
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("result", [])
            return results[0] if results else None
    except Exception as exc:
        logger.debug("request_log: get_log_by_key failed: {}", exc)
    return None


async def get_recent_logs_by_caller(caller: str, limit: int = 5) -> list[dict]:
    """Fetch recent logs for a specific caller."""
    try:
        client = _get_client()
        resp = await client.post(
            "/query",
            json={
                "query": f"""
                    FOR doc IN {_COLLECTION}
                    FILTER doc.caller == @caller
                    SORT doc.ts DESC
                    LIMIT @limit
                    RETURN doc
                """,
                "bindVars": {"caller": caller, "limit": limit},
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("result", [])
    except Exception as exc:
        logger.debug("request_log: get_recent_logs_by_caller failed: {}", exc)
    return []


# Best practices excerpt for common error scenarios
BEST_PRACTICES = """
## scillm Best Practices

### Batch Processing (50+ requests)
Use chunked processing with CHUNK_SIZE=4:
```python
for chunk in chunks(prompts, 4):
    results = await asyncio.gather(*[call_proxy(p) for p in chunk])
```
Firing 50+ requests at once causes queue timeout (300s limit).

### Headers
Always include X-Caller-Skill header for tracking:
```python
headers={"X-Caller-Skill": "your-skill-name"}
```

### Timeouts
Use generous timeouts for LLM calls (30-120s). The proxy handles retries internally.

### JSON Responses
Add `response_format: {"type": "json_object"}` for structured output. The proxy auto-validates and retries.

### Model Selection
- `text` — general text (Chutes → Gemini → DeepSeek cascade)
- `vlm` — images/PDFs (auto-detected from image_url content)
- `text-gemini` — direct Gemini (1M context)
- `local-text` — Ollama (always-on, no cost)

### Error Handling
Check response for `choices[0].message.content`. On error, check `error` field in response body.
"""


async def analyze_call(log: dict) -> dict:
    """Analyze a call using LLM and return structured diagnosis with best practices."""
    prompt = f"""You are an LLM proxy debugger. Analyze this API call and provide a concise diagnosis.

## Call Details
- **Status:** {log.get('status', 'unknown')}
- **Error:** {log.get('error') or 'None'}
- **Model Requested:** {log.get('model_requested', 'unknown')}
- **Model Served:** {log.get('model_served') or 'unknown'}
- **Provider:** {log.get('provider') or 'unknown'}
- **Caller:** {log.get('caller') or 'unknown'}
- **Duration:** {log.get('duration_ms', 'unknown')}ms
- **Tokens:** prompt={log.get('prompt_tokens', 0)}, completion={log.get('completion_tokens', 0)}, total={log.get('total_tokens', 0)}
- **Cost:** ${log.get('cost_usd', 0):.4f}

## Input Prompt
```
{log.get('request_prompt') or '(not captured)'}
```

## Response
```
{log.get('response_content') or '(not captured)'}
```

{BEST_PRACTICES}

Based on the call details and best practices above, provide:
1. **Diagnosis:** What happened? (1-2 sentences)
2. **Root Cause:** Why did this happen?
3. **Fix:** Specific code change or action to resolve this
4. **Relevant Best Practice:** Which best practice applies here?

Keep it concise — under 200 words total. Format as markdown."""

    try:
        # Use internal proxy call
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "http://127.0.0.1:4001/v1/chat/completions",
                headers={
                    "Authorization": "Bearer sk-dev-proxy-123",
                    "X-Caller-Skill": "scillm.debug-endpoint",
                },
                json={
                    "model": "text-gemini",
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                analysis = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return {
                    "success": True,
                    "analysis": analysis,
                    "call_id": log.get("_key"),
                    "caller": log.get("caller"),
                    "status": log.get("status"),
                    "model": f"{log.get('model_requested')} → {log.get('model_served') or 'unknown'}",
                    "timestamp": log.get("ts"),
                    "best_practices_included": True,
                }
            else:
                return {
                    "success": False,
                    "error": f"Analysis call failed: {resp.status_code}",
                    "call_id": log.get("_key"),
                }
    except Exception as exc:
        return {
            "success": False,
            "error": f"Analysis error: {exc}",
            "call_id": log.get("_key"),
        }
