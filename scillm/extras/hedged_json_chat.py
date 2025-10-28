from __future__ import annotations
import asyncio, time
from typing import Any, Dict, List, Optional

import scillm

_WINNERS: Dict[str, Dict[str, Any]] = {}

def _get_winner(api_base: str, ttl_s: int) -> Optional[Dict[str, Any]]:
    w = _WINNERS.get(api_base)
    if not w:
        return None
    if (time.time() - w.get("ts", 0)) > ttl_s:
        return None
    return w

async def hedged_json_chat(
    *,
    model: str,
    messages: List[Dict[str, Any]],
    api_base: str,
    key: str,
    timeout: float = 30.0,
    temperature: float = 0.0,
    max_tokens: Optional[int] = None,
    variants: Optional[List[Dict[str,str]]] = None,
    ttl_s: int = 600,
    hedge_delay_ms: int = 250,
) -> Any:
    """
    Hedge 2–3 auth header variants once, cache the winner per api_base, then reuse.
    Returns an OpenAI-shaped dict (strict JSON via response_format=json_object).
    """
    # Reuse cached winner if fresh
    w = _get_winner(api_base, ttl_s)
    if w:
        return await scillm.acompletion(
            model=model,
            messages=messages,
            api_base=api_base,
            api_key=None,
            custom_llm_provider="openai_like",
            extra_headers=w["headers"],
            response_format={"type": "json_object"},
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )

    auth_variants = variants or [
        {"Authorization": f"Bearer {key}"},
        {"x-api-key": key},
        {"Authorization": key},
    ]

    async def _try(hdrs: Dict[str,str]):
        try:
            r = await scillm.acompletion(
                model=model,
                messages=messages,
                api_base=api_base,
                api_key=None,
                custom_llm_provider="openai_like",
                extra_headers=hdrs,
                response_format={"type": "json_object"},
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
            content = r.choices[0].message.get("content", "")
            if content:
                return True, r, hdrs
        except Exception:
            pass
        return False, None, hdrs

    # Launch first, schedule second/third with a small hedge delay
    tasks = [asyncio.create_task(_try(auth_variants[0]))]
    if len(auth_variants) > 1:
        await asyncio.sleep(hedge_delay_ms / 1000.0)
        tasks.append(asyncio.create_task(_try(auth_variants[1])))
    if len(auth_variants) > 2:
        await asyncio.sleep(hedge_delay_ms / 1000.0)
        tasks.append(asyncio.create_task(_try(auth_variants[2])))

    # Consume tasks as they complete; stop on first success
    pending = set(tasks)
    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for d in done:
            ok, resp, hdrs = await d
            if ok:
                # cancel remaining
                for p in pending:
                    try:
                        p.cancel()
                    except Exception:
                        pass
                _WINNERS[api_base] = {"headers": hdrs, "ts": time.time()}
                return resp
    raise RuntimeError("All hedged auth variants failed")

__all__ = ["hedged_json_chat"]

# New, clearer alias
async def hedged_json_completion(**kwargs):
    return await hedged_json_chat(**kwargs)
