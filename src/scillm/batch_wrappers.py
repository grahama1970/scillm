"""Convenience wrappers around parallel_acompletions.

Extracted from batch.py to keep file sizes under 800 lines.
"""

from __future__ import annotations

import os as _os
from typing import Any, AsyncIterator, Dict, List, Optional


def _extract_content_from_response(resp: Any) -> str:
    """Extract text content from an OpenAI ChatCompletion response."""
    try:
        if isinstance(resp, dict):
            msg = (resp.get("choices", [{}])[0].get("message", {}) or {})
            content = msg.get("content") or ""
            if not content and isinstance(msg.get("reasoning_content"), str):
                return msg["reasoning_content"]
            return content or ""
        msg = resp.choices[0].message
        content = msg.get("content") if hasattr(msg, "get") else getattr(msg, "content", "")
        if not content and hasattr(msg, "get") and isinstance(msg.get("reasoning_content"), str):
            return msg.get("reasoning_content")
        return content or ""
    except Exception:
        return ""


async def parallel_acompletions_simple(
    requests: List[Dict],
    *,
    api_base: str | None = None,
    api_key: str | None = None,
    custom_llm_provider: str = "openai_like",
    concurrency: int = 6,
    tenacious: bool = True,
    wall_time_s: float = 60.0,
    timeout: float = 30.0,
    backoff_base: float = 0.5,
    backoff_cap_s: float = 8.0,
) -> List[Dict[str, Any]]:
    """Minimal wrapper: returns an ordered list of {ok, content, error?, status?}."""
    from .batch import parallel_acompletions

    res = await parallel_acompletions(
        requests,
        api_base=api_base,
        api_key=api_key,
        custom_llm_provider=custom_llm_provider,
        concurrency=concurrency,
        tenacious=tenacious,
        wall_time_s=wall_time_s,
        timeout=timeout,
        backoff_base=backoff_base,
        backoff_cap_s=backoff_cap_s,
    )
    out: List[Dict[str, Any]] = []
    for r in res:
        if isinstance(r, dict) and r.get("error"):
            out.append({"ok": False, "content": "", "error": r.get("error"), "status": r.get("status")})
        else:
            content = _extract_content_from_response(r)
            out.append({"ok": bool(content), "content": content})
    return out


async def parallel_acompletions_simple_env(
    requests: List[Dict],
    *,
    concurrency: int = 6,
    tenacious: bool = True,
    wall_time_s: float = 60.0,
    timeout: float = 30.0,
    backoff_base: float = 0.5,
    backoff_cap_s: float = 8.0,
) -> List[Dict[str, Any]]:
    """Env-configured simple wrapper."""
    base = (_os.environ.get("SCILLM_API_BASE") or _os.environ.get("CHUTES_API_BASE") or "").strip()
    key = (_os.environ.get("SCILLM_PROXY_KEY") or _os.environ.get("CHUTES_API_KEY") or "").strip()
    model_default = _os.environ.get("CHUTES_MODEL_ID") or _os.environ.get("CHUTES_TEXT_MODEL")
    reqs: List[Dict] = []
    for r in requests:
        rr = dict(r or {})
        rr.setdefault("model", model_default)
        rr.setdefault("api_base", base)
        rr.setdefault("api_key", key)
        rr.setdefault("custom_llm_provider", "openai_like")
        reqs.append(rr)
    return await parallel_acompletions_simple(
        reqs,
        api_base=base,
        api_key=key,
        custom_llm_provider="openai_like",
        concurrency=concurrency,
        tenacious=tenacious,
        wall_time_s=wall_time_s,
        timeout=timeout,
        backoff_base=backoff_base,
        backoff_cap_s=backoff_cap_s,
    )


_DEFAULT_PROXY_BASE = "http://localhost:4001"
_DEFAULT_PROXY_KEY = "sk-dev-proxy-123"


async def parallel_acompletions_proxy(
    requests: List[Dict],
    *,
    concurrency: int = 6,
    tenacious: bool = True,
    wall_time_s: float = 900.0,
    timeout: float = 20.0,
    backoff_base: float = 0.5,
    backoff_cap_s: float = 30.0,
    default_max_tokens: int | None = None,
    default_temperature: float | None = None,
    response_format: dict | None = None,
    schema: Any | None = None,
    retry_invalid_json: int = 0,
    repair_invalid_json: Optional[bool] = None,
    # Source grounding verification
    source: "str | list[str] | None" = None,
    grounding_threshold: float = 0.7,
    grounding_retries: int = 2,
) -> AsyncIterator[Dict[str, Any]]:
    """Proxy-first batch wrapper — always routes through the scillm proxy."""
    from .batch import parallel_acompletions_iter

    base = _os.environ.get("SCILLM_API_BASE", _DEFAULT_PROXY_BASE)
    key = _os.environ.get("SCILLM_PROXY_KEY", _DEFAULT_PROXY_KEY)
    async for ev in parallel_acompletions_iter(
        requests,
        api_base=base,
        api_key=key,
        concurrency=concurrency,
        tenacious=tenacious,
        wall_time_s=wall_time_s,
        timeout=timeout,
        backoff_base=backoff_base,
        backoff_cap_s=backoff_cap_s,
        default_max_tokens=default_max_tokens,
        default_temperature=default_temperature,
        response_format=response_format,
        schema=schema,
        retry_invalid_json=retry_invalid_json,
        repair_invalid_json=repair_invalid_json,
        source=source,
        grounding_threshold=grounding_threshold,
        grounding_retries=grounding_retries,
    ):
        yield ev
