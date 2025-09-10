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
            await queue.put(await _run_one(router, sem, i, r, return_exceptions))
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
