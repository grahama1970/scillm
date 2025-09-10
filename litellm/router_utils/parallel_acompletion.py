"""
Experimental concurrent fan-out helpers for Router.acompletion.

Exports:
- iter_parallel_acompletions(): async iterator yielding each result as soon as it finishes
- gather_parallel_acompletions(): convenience wrapper returning a list of all results
  (optionally preserving original request order)

Non-goals (initial version):
- Streaming multiplex (stream=True rejected)
- Provider multi-prompt batch merging (each request is an ordinary acompletion)
- Aggregated usage/cost rollups
- Progress callbacks

Intended use cases:
- Multi-document / multi-query RAG summarization
- Bulk evaluation / scoring tasks
- Query expansion / parallel prompting
"""

from __future__ import annotations

import asyncio
import time
import uuid
import typing
from typing import Any, AsyncIterator, Dict, List, Optional

from litellm._logging import verbose_router_logger

if typing.TYPE_CHECKING:  # pragma: no cover
    from litellm.router import Router


class RouterParallelRequest(typing.TypedDict, total=False):
    id: str
    model: str
    messages: list
    metadata: dict  # arbitrary caller metadata (e.g., original_index)
    # plus any additional completion kwargs (temperature, tools, etc.)


class RouterParallelResult(typing.TypedDict, total=False):
    id: str
    request: Dict[str, Any]        # minimally: model + messages (+ optional metadata)
    response: Any                  # ModelResponse / provider dict / None on error
    error: Optional[str]           # stringified exception if captured
    started_at: float
    completed_at: float
    duration: float


_RESERVED_KEYS = {"id", "model", "messages", "metadata"}


def _assign_request_id(r: RouterParallelRequest) -> None:
    if not r.get("id"):
        # short stable-ish ID for logs
        r["id"] = uuid.uuid4().hex[:8]


async def _run_single(
    router_obj: "Router",
    req: RouterParallelRequest,
    sem: asyncio.Semaphore,
    return_exceptions: bool,
) -> RouterParallelResult:
    """
    Execute one Router.acompletion under concurrency control, returning a structured result.
    """
    _assign_request_id(req)
    rid = req["id"]
    model = req["model"]
    messages = req["messages"]

    if req.get("stream"):
        raise ValueError("stream=True not supported in parallel_acompletions (initial version).")

    started = time.time()

    async with sem:
        try:
            passthrough = {
                k: v
                for k, v in req.items()
                if k not in _RESERVED_KEYS
            }
            if "metadata" in req:
                passthrough["metadata"] = req["metadata"]

            verbose_router_logger.debug(f"[parallel_acompletions] start rid={rid} model={model}")
            resp = await router_obj.acompletion(model=model, messages=messages, **passthrough)
            completed = time.time()
            result: RouterParallelResult = {
                "id": rid,
                "request": {"model": model, "messages": messages, **({"metadata": req['metadata']} if "metadata" in req else {})},
                "response": resp,
                "error": None,
                "started_at": started,
                "completed_at": completed,
                "duration": completed - started,
            }
            verbose_router_logger.debug(
                f"[parallel_acompletions] success rid={rid} duration={result['duration']:.4f}s"
            )
            return result
        except Exception as e:
            completed = time.time()
            if not return_exceptions:
                # propagate; caller will cancel outstanding tasks
                raise
            verbose_router_logger.debug(f"[parallel_acompletions] error rid={rid} err={e}")
            return {
                "id": rid,
                "request": {"model": model, "messages": messages, **({"metadata": req['metadata']} if "metadata" in req else {})},
                "response": None,
                "error": str(e),
                "started_at": started,
                "completed_at": completed,
                "duration": completed - started,
            }


async def iter_parallel_acompletions(
    router_obj: "Router",
    requests: List[RouterParallelRequest],
    *,
    concurrency: int = 8,
    return_exceptions: bool = True,
) -> AsyncIterator[RouterParallelResult]:
    """
    Yield each completion result as it finishes (unordered by default).

    Args:
        router_obj: Router instance
        requests: list of request dicts (model, messages, optional id/metadata + kwargs)
        concurrency: max in-flight tasks at orchestrator layer
        return_exceptions: if True, capture exceptions into result['error']; otherwise first error raises

    Yields:
        RouterParallelResult items in completion order.
    """
    if concurrency <= 0:
        raise ValueError("concurrency must be > 0")

    sem = asyncio.Semaphore(concurrency)
    tasks: List[asyncio.Task] = []
    for r in requests:
        tasks.append(asyncio.create_task(_run_single(router_obj, r, sem, return_exceptions)))

    try:
        for coro in asyncio.as_completed(tasks):
            yield await coro
    except Exception:
        # Cancel unfinished tasks if fail-fast mode triggered
        for t in tasks:
            if not t.done():
                t.cancel()
        raise


async def gather_parallel_acompletions(
    router_obj: "Router",
    requests: List[RouterParallelRequest],
    *,
    concurrency: int = 8,
    return_exceptions: bool = True,
    preserve_order: bool = False,
) -> List[RouterParallelResult]:
    """
    Collect all results into a list.

    Args mirror iter_parallel_acompletions plus:
        preserve_order: if True, results list aligns with original `requests` ordering;
                        otherwise it's completion order.
    """
    results: List[RouterParallelResult] = []
    async for r in iter_parallel_acompletions(
        router_obj,
        requests,
        concurrency=concurrency,
        return_exceptions=return_exceptions,
    ):
        results.append(r)

    if preserve_order:
        # Build mapping by id (guaranteed assigned) to reorder according to input list
        by_id = {r["id"]: r for r in results}
        ordered: List[RouterParallelResult] = []
        for req in requests:
            _assign_request_id(req)  # ensure we know its final id
            ordered.append(by_id[req["id"]])
        return ordered

    return results
