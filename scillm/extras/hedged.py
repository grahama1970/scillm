from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional


async def hedged_acompletion(
    router,
    *,
    primary_model: str,
    messages: List[Dict[str, Any]],
    hedge_ms: int = 700,
    backup_model: Optional[str] = None,
    **kwargs,
):
    """
    Fire primary request, and if no result by `hedge_ms`, fire backup. Return the first success and cancel the loser.
    - router: a litellm.Router instance
    - primary_model: Router model/group to call first
    - backup_model: optional second model/group (if None, uses the next fallback inside Router)
    - kwargs: forwarded to router.acompletion
    """

    async def _call(model):
        return await router.acompletion(model=model, messages=messages, **kwargs)

    primary_task = asyncio.create_task(_call(primary_model))

    try:
        return await asyncio.wait_for(primary_task, timeout=hedge_ms / 1000.0)
    except asyncio.TimeoutError:
        pass

    # Fire hedge
    if backup_model is None:
        # best-effort: try the same call again and let Router fall back internally
        backup_model = primary_model

    backup_task = asyncio.create_task(_call(backup_model))

    done, pending = await asyncio.wait(
        {primary_task, backup_task}, return_when=asyncio.FIRST_COMPLETED
    )
    winner = next(iter(done))
    for t in pending:
        try:
            t.cancel()
        except Exception:
            pass
    return await winner


__all__ = ["hedged_acompletion"]

