"""Batch resume middleware — skip LLM calls for already-completed items.

When a request includes scillm_metadata.batch_id + item_id, checks ArangoDB
for a successful prior completion. If found, returns cached response without
making an LLM call.

This enables reliable resume for long-running batch jobs:
- Generator passes batch_id + item_id in scillm_metadata
- If job fails at item #500, rerun picks up at #501 automatically
- Cached responses include x-scillm-batch-resume: hit header
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from loguru import logger

from scillm.proxy.middleware import BaseMiddleware

_MEMORY_URL = os.environ.get("MEMORY_URL", "http://127.0.0.1:8601")
_COLLECTION = "llm_call_log"

# Lazy-init httpx client
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=_MEMORY_URL,
            timeout=httpx.Timeout(5.0, connect=2.0),
        )
    return _client


async def _lookup_cached_response(batch_id: str, item_id: str) -> dict | None:
    """Query ArangoDB for a successful completion matching batch_id + item_id."""
    try:
        client = _get_client()
        # Query for successful completion with matching metadata
        # Memory service uses 'aql' key and returns 'documents'
        aql = f"""
            FOR doc IN {_COLLECTION}
            FILTER doc.status == "ok"
            FILTER doc.metadata.batch_id == "{batch_id}"
            FILTER doc.metadata.item_id == "{item_id}"
            SORT doc.ts DESC
            LIMIT 1
            RETURN doc
        """
        resp = await client.post("/query", json={"aql": aql})
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("documents", [])
            if results:
                return results[0]
    except Exception as exc:
        logger.debug("batch_resume: lookup failed: {}", exc)
    return None


def _reconstruct_response(cached: dict) -> dict:
    """Reconstruct an OpenAI-format response from cached log entry."""
    # Build minimal response that matches what the caller expects
    return {
        "id": f"batch-resume-{cached.get('_key', 'unknown')}",
        "object": "chat.completion",
        "created": 0,  # Not critical for batch use
        "model": cached.get("model_served", cached.get("model_requested", "unknown")),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": cached.get("response_content", ""),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": cached.get("prompt_tokens", 0),
            "completion_tokens": cached.get("completion_tokens", 0),
            "total_tokens": cached.get("total_tokens", 0),
        },
        # Mark as batch resume hit
        "_batch_resume": True,
        "_cached_from": cached.get("_key"),
    }


async def get_batch_failures(batch_id: str) -> list[dict]:
    """Query all failed items for a batch.

    Returns list of {item_id, error, ts} for items that failed.
    Used by callers to implement retry-failed-only logic.
    """
    try:
        client = _get_client()
        aql = f"""
            FOR doc IN {_COLLECTION}
            FILTER doc.metadata.batch_id == "{batch_id}"
            FILTER doc.status == "error"
            RETURN {{
                item_id: doc.metadata.item_id,
                error: doc.error,
                ts: doc.ts,
                model: doc.model_requested
            }}
        """
        resp = await client.post("/query", json={"aql": aql})
        if resp.status_code == 200:
            data = resp.json()
            return data.get("documents", [])
    except Exception as exc:
        logger.debug("batch_resume: get_batch_failures failed: {}", exc)
    return []


async def get_batch_status(batch_id: str) -> dict:
    """Get overall status of a batch: success/failure counts.

    Returns: {
        "batch_id": str,
        "total": int,
        "success": int,
        "failed": int,
        "failed_items": list[str]  # item_ids that failed
    }
    """
    try:
        client = _get_client()
        aql = f"""
            FOR doc IN {_COLLECTION}
            FILTER doc.metadata.batch_id == "{batch_id}"
            COLLECT status = doc.status INTO items
            RETURN {{
                status: status,
                count: LENGTH(items),
                item_ids: items[*].doc.metadata.item_id
            }}
        """
        resp = await client.post("/query", json={"aql": aql})
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("documents", [])
            summary = {"batch_id": batch_id, "total": 0, "success": 0, "failed": 0, "failed_items": []}
            for row in results:
                status = row.get("status")
                count = row.get("count", 0)
                summary["total"] += count
                if status == "ok":
                    summary["success"] = count
                elif status == "error":
                    summary["failed"] = count
                    summary["failed_items"] = row.get("item_ids", [])
            return summary
    except Exception as exc:
        logger.debug("batch_resume: get_batch_status failed: {}", exc)
    return {"batch_id": batch_id, "total": 0, "success": 0, "failed": 0, "failed_items": []}


async def check_batch_resume(metadata: dict) -> dict | None:
    """Check if a work item already completed successfully.

    This is the public API called by app.py before routing.

    Args:
        metadata: The scillm_metadata dict with batch_id and item_id

    Returns:
        Reconstructed OpenAI response if found, None otherwise
    """
    if not metadata:
        return None

    batch_id = metadata.get("batch_id")
    item_id = metadata.get("item_id")
    if not batch_id or not item_id:
        return None

    cached = await _lookup_cached_response(batch_id, item_id)
    if cached:
        logger.info(
            "batch_resume: HIT for batch={} item={} (cached_key={})",
            batch_id,
            item_id,
            cached.get("_key"),
        )
        return _reconstruct_response(cached)

    return None


class BatchResumeMiddleware(BaseMiddleware):
    """Check for prior successful completion before making LLM call.

    If scillm_metadata contains batch_id + item_id and a successful
    completion exists in llm_call_log, returns cached response immediately.
    """

    async def pre_call(self, request: dict) -> dict:
        # Check for scillm_metadata with batch_id + item_id
        metadata = request.get("scillm_metadata") or request.get("_scillm_metadata")
        if not metadata:
            return request

        batch_id = metadata.get("batch_id")
        item_id = metadata.get("item_id")
        if not batch_id or not item_id:
            return request

        # Look up cached response
        cached = await _lookup_cached_response(batch_id, item_id)
        if cached:
            logger.info(
                "batch_resume: HIT for batch={} item={} (cached_key={})",
                batch_id,
                item_id,
                cached.get("_key"),
            )
            # Stash the cached response to short-circuit the call
            request["_batch_resume_response"] = _reconstruct_response(cached)

        return request

    async def post_call(self, request: dict, response: Any) -> Any:
        # If we had a batch resume hit, the response was already set
        # Just add a header to indicate it was a resume
        if request.get("_batch_resume_response"):
            if isinstance(response, dict):
                response["_batch_resume"] = True
        return response
