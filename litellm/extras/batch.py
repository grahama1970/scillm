from __future__ import annotations

import asyncio
from typing import Any, Dict, Iterable, List, Sequence, Tuple


async def acompletion_as_completed(
    router: Any,
    requests: Sequence[Dict[str, Any]],
    *,
    concurrency: int = 5,
    **shared_kwargs: Any,
) -> Iterable[Tuple[int, Any]]:
    """
    Yield (index, response) as each completion finishes.

    requests: list of kwargs for router.acompletion (e.g., {"model":..., "messages":[...]})
    shared_kwargs: merged into each request
    """
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def run_one(idx: int, req: Dict[str, Any]):
        async with sem:
            return idx, await router.acompletion(**{**shared_kwargs, **req})

    tasks = [asyncio.create_task(run_one(i, req)) for i, req in enumerate(requests)]
    for fut in asyncio.as_completed(tasks):
        yield await fut

