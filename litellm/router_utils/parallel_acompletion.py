"""
<<<<<<< HEAD
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
=======
Utilities for running multiple Router.acompletion calls concurrently.

Two surfaces:
- gather_parallel_acompletions(): returns a list of results
- iter_parallel_acompletions(): async iterator yielding results as they finish (completion order)

Flag gated in router.py via ENABLE_PARALLEL_ACOMPLETIONS.
>>>>>>> abf222a5df (feat(router): add experimental parallel_acompletions utilities)
"""

from __future__ import annotations

import asyncio
<<<<<<< HEAD
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
=======
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Dict, List, Optional

# Type shape for a router request
@dataclass
class RouterParallelRequest:
    model: str
    messages: List[Dict[str, Any]]
    kwargs: Optional[Dict[str, Any]] = None  # extra params for acompletion


@dataclass
class RouterParallelResult:
    index: int
    request: RouterParallelRequest
    response: Optional[Any] = None
    exception: Optional[BaseException] = None


async def _run_one(
    router: Any,
    sem: asyncio.Semaphore,
    idx: int,
    req: RouterParallelRequest,
    return_exceptions: bool,
) -> RouterParallelResult:
    async with sem:
        try:
            resp = await router.acompletion(
                model=req.model,
                messages=req.messages,
                **(req.kwargs or {}),
            )
            return RouterParallelResult(index=idx, request=req, response=resp)
        except BaseException as e:
            if not return_exceptions:
                raise
            return RouterParallelResult(index=idx, request=req, exception=e)


async def gather_parallel_acompletions(
    router: Any,
>>>>>>> abf222a5df (feat(router): add experimental parallel_acompletions utilities)
    requests: List[RouterParallelRequest],
    *,
    concurrency: int = 8,
    return_exceptions: bool = True,
    preserve_order: bool = False,
) -> List[RouterParallelResult]:
    """
<<<<<<< HEAD
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
=======
    Launch all acompletion calls with a bounded concurrency semaphore.

    Args:
        router: litellm.Router instance
        requests: list of RouterParallelRequest
        concurrency: max in-flight calls
        return_exceptions: if False, first exception aborts everything
        preserve_order: if True, returned list order matches input order
    """
    if concurrency <= 0:
        raise ValueError("concurrency must be >= 1")
    sem = asyncio.Semaphore(concurrency)
    tasks: List[Awaitable[RouterParallelResult]] = [
        _run_one(router, sem, i, r, return_exceptions) for i, r in enumerate(requests)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=False)  # internal handling
    if preserve_order:
        results.sort(key=lambda r: r.index)
    return results


async def _iter_worker(
    router: Any,
    requests: List[RouterParallelRequest],
    concurrency: int,
    return_exceptions: bool,
    queue: "asyncio.Queue[RouterParallelResult]",
):
    sem = asyncio.Semaphore(concurrency)

    async def runner(i: int, r: RouterParallelRequest):
        try:
            await queue.put(
                await _run_one(router, sem, i, r, return_exceptions)
            )
        finally:
            pass

    await asyncio.gather(*(runner(i, r) for i, r in enumerate(requests)))
    await queue.put(None)  # sentinel


async def iter_parallel_acompletions(
    router: Any,
    requests: List[RouterParallelRequest],
    *,
    concurrency: int = 8,
    return_exceptions: bool = True,
) -> AsyncIterator[RouterParallelResult]:
    """
    Async iterator yielding results as soon as they complete (not input order).
    """
    if concurrency <= 0:
        raise ValueError("concurrency must be >= 1")
    queue: "asyncio.Queue[Optional[RouterParallelResult]]" = asyncio.Queue()
    asyncio.create_task(
        _iter_worker(router, requests, concurrency, return_exceptions, queue)
    )
    while True:
        item = await queue.get()
        if item is None:
            break
        yield item

>>>>>>> abf222a5df (feat(router): add experimental parallel_acompletions utilities)
"""
Utilities for running multiple Router.acompletion calls concurrently.

Two surfaces:
- gather_parallel_acompletions(): returns a list of results
- iter_parallel_acompletions(): async iterator yielding results as they finish (completion order)

Flag gated in router.py via ENABLE_PARALLEL_ACOMPLETIONS.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Dict, List, Optional

# Type shape for a router request
@dataclass
class RouterParallelRequest:
    model: str
    messages: List[Dict[str, Any]]
    kwargs: Optional[Dict[str, Any]] = None  # extra params for acompletion


@dataclass
class RouterParallelResult:
    index: int
    request: RouterParallelRequest
    response: Optional[Any] = None
    exception: Optional[BaseException] = None


async def _run_one(
    router: Any,
    sem: asyncio.Semaphore,
    idx: int,
    req: RouterParallelRequest,
    return_exceptions: bool,
) -> RouterParallelResult:
    async with sem:
        try:
            resp = await router.acompletion(
                model=req.model,
                messages=req.messages,
                **(req.kwargs or {}),
            )
            return RouterParallelResult(index=idx, request=req, response=resp)
        except BaseException as e:
            if not return_exceptions:
                raise
            return RouterParallelResult(index=idx, request=req, exception=e)


async def gather_parallel_acompletions(
    router: Any,
    requests: List[RouterParallelRequest],
    *,
    concurrency: int = 8,
    return_exceptions: bool = True,
    preserve_order: bool = False,
) -> List[RouterParallelResult]:
    """
    Launch all acompletion calls with a bounded concurrency semaphore.

    Args:
        router: litellm.Router instance
        requests: list of RouterParallelRequest
        concurrency: max in-flight calls
        return_exceptions: if False, first exception aborts everything
        preserve_order: if True, returned list order matches input order
    """
    if concurrency <= 0:
        raise ValueError("concurrency must be >= 1")
    sem = asyncio.Semaphore(concurrency)
    tasks: List[Awaitable[RouterParallelResult]] = [
        _run_one(router, sem, i, r, return_exceptions) for i, r in enumerate(requests)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=False)  # internal handling
    if preserve_order:
        results.sort(key=lambda r: r.index)
    return results


async def _iter_worker(
    router: Any,
    requests: List[RouterParallelRequest],
    concurrency: int,
    return_exceptions: bool,
    queue: "asyncio.Queue[RouterParallelResult]",
):
    sem = asyncio.Semaphore(concurrency)

    async def runner(i: int, r: RouterParallelRequest):
        try:
            await queue.put(
                await _run_one(router, sem, i, r, return_exceptions)
            )
        finally:
            pass

    await asyncio.gather(*(runner(i, r) for i, r in enumerate(requests)))
    await queue.put(None)  # sentinel


async def iter_parallel_acompletions(
    router: Any,
    requests: List[RouterParallelRequest],
    *,
    concurrency: int = 8,
    return_exceptions: bool = True,
) -> AsyncIterator[RouterParallelResult]:
    """
    Async iterator yielding results as soon as they complete (not input order).
    """
    if concurrency <= 0:
        raise ValueError("concurrency must be >= 1")
    queue: "asyncio.Queue[Optional[RouterParallelResult]]" = asyncio.Queue()
    asyncio.create_task(
        _iter_worker(router, requests, concurrency, return_exceptions, queue)
    )
    while True:
        item = await queue.get()
        if item is None:
            break
        yield item
