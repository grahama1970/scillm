from __future__ import annotations

from typing import Any, Dict, List, Optional

from litellm import Router


async def _shutdown_router(router: Router) -> None:
    try:
        aclose = getattr(router, "aclose", None)
        if callable(aclose):
            await aclose()  # type: ignore
            return
        close = getattr(router, "close", None)
        if callable(close):
            close()  # type: ignore
    except Exception:
        pass


async def arouter_call(
    *,
    model: str,
    messages: List[Dict[str, Any]],
    stream: bool = False,
    num_retries: Optional[int] = None,
    default_max_parallel_requests: Optional[int] = None,
    **kwargs: Any,
):
    """Minimal Router-based call used by the mini-agent.

    - Keeps surface small: no CLI, no batch helpers.
    - Accepts any kwargs supported by Router.acompletion (tools, tool_choice, temperature, etc.).
    - Returns the raw Router.acompletion response so tool_calls are preserved.
    """
    router = Router(
        model_list=[{"model_name": model, "litellm_params": {"model": model}}],
        num_retries=num_retries,
        default_max_parallel_requests=default_max_parallel_requests,
    )
    try:
        # Streaming not used by the current agent loop; included for parity.
        resp = await router.acompletion(model=model, messages=messages, stream=stream, **kwargs)
        return resp
    finally:
        await _shutdown_router(router)

