# litellm/router_utils/parallel_acompletion.py
"""
Parallel completion helpers for LiteLLM Router.

Goals
-----
- Provide a small, typed request container (RouterParallelRequest) for batch fan-out.
- Support common generation knobs (temperature, max_tokens, top_p, stream).
- Aggregate streaming responses into a normalized Chat-Completions-like dict so
  callers/tests don't have to handle provider-specific streaming shapes.
- Return a stable result container (ParallelResult) with (index, request, response, exception).
- Preserve input order deterministically when requested.

Back-compat
-----------
- `run_parallel_requests(...)` is kept for existing callers and now wraps the
  unified path, returning List[Tuple[index, response, exception]] as before.
- New code should use `gather_parallel_acompletions(...)` which returns
  List[ParallelResult].

Notes
-----
- We do NOT parse provider-specific usage/cost fields here; callers can inspect
  `response` as returned by Router.acompletion (non-stream) or the normalized
  aggregated dict (stream).
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union


# ----------------------------- Request / Result -------------------------------

@dataclass
class RouterParallelRequest:
    """
    Typed request for a single parallel Router.acompletion call.

    - `model`: Router model group or concrete model alias known to Router.
    - `messages`: OpenAI-style messages list.
    - Common knobs: optional temperature, max_tokens, top_p, stream.
    - `kwargs`: provider/vendor-specific extras (will be merged; does not override
                explicitly set common knobs unless provided directly in kwargs).
    """
    model: str
    messages: List[Dict[str, Any]]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    stream: Optional[bool] = None
    kwargs: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ParallelResult:
    """
    Result of a single parallel request.

    - index: original position in the input list
    - request: the RouterParallelRequest (normalized)
    - response: provider response (non-stream -> raw; stream -> aggregated dict) or None on error
    - exception: captured Exception if the request failed, else None
    """
    index: int
    request: RouterParallelRequest
    response: Any | None
    exception: Exception | None
    timing_ms: Optional[int] = None


# ----------------------------- Internal helpers -------------------------------

def _normalize_request(
    req: Union[RouterParallelRequest, Dict[str, Any], Any], idx: int
) -> RouterParallelRequest:
    """
    Accept RouterParallelRequest, dict, dataclass, or any object with
    `model` and `messages` attributes. Falls back to structural copy to avoid
    brittle isinstance checks across modules.
    """
    if isinstance(req, RouterParallelRequest):
        return req

    data: Dict[str, Any]

    if isinstance(req, dict):
        data = dict(req)
    elif dataclasses.is_dataclass(req):
        try:
            data = dataclasses.asdict(req)
        except Exception:
            data = {
                "model": getattr(req, "model", None),
                "messages": getattr(req, "messages", None),
                "temperature": getattr(req, "temperature", None),
                "max_tokens": getattr(req, "max_tokens", None),
                "top_p": getattr(req, "top_p", None),
                "stream": getattr(req, "stream", None),
                "kwargs": getattr(req, "kwargs", None),
            }
    elif hasattr(req, "model") and hasattr(req, "messages"):
        data = {
            "model": getattr(req, "model"),
            "messages": getattr(req, "messages"),
        }
        for key in (
            "temperature",
            "max_tokens",
            "top_p",
            "stream",
            "kwargs",
            "metadata",
            "tools",
            "tool_choice",
            "response_format",
            "seed",
            "timeout",
        ):
            if hasattr(req, key):
                data.setdefault(key, getattr(req, key))
    else:
        if inspect.isclass(req):
            raise TypeError(
                f"request at index {idx} is a class, not an instance: {req!r}; construct it first"
            )
        raise TypeError(
            f"request at index {idx} must be RouterParallelRequest-like or dict, got instance of {type(req)}"
        )

    model = data.get("model")
    messages = data.get("messages")
    if model is None or not isinstance(messages, list):
        raise TypeError(
            f"request at index {idx} missing required fields model/messages after normalization: keys={list(data.keys())}"
        )

    kwargs = data.get("kwargs") or {}
    if kwargs is None:
        kwargs = {}

    return RouterParallelRequest(
        model=str(model),
        messages=list(messages),
        temperature=data.get("temperature"),
        max_tokens=data.get("max_tokens"),
        top_p=data.get("top_p"),
        stream=data.get("stream"),
        kwargs=dict(kwargs),
    )


def _normalize_requests(
    requests: Sequence[Union[RouterParallelRequest, Dict[str, Any], Any]]
) -> List[RouterParallelRequest]:
    return [_normalize_request(r, i) for i, r in enumerate(requests)]


def _merge_kwargs(req: RouterParallelRequest) -> Dict[str, Any]:
    """
    Merge typed fields into kwargs without letting kwargs overwrite explicitly set
    typed fields (unless caller passed them only via kwargs).
    """
    out = dict(req.kwargs or {})
    if req.temperature is not None:
        out.setdefault("temperature", req.temperature)
    if req.max_tokens is not None:
        out.setdefault("max_tokens", req.max_tokens)
    if req.top_p is not None:
        out.setdefault("top_p", req.top_p)
    if req.stream is not None:
        out.setdefault("stream", req.stream)
    return out


async def _aggregate_stream(
    call_target,
    req: RouterParallelRequest,
    call_kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Consume a streaming response and aggregate into a minimal Chat-Completions-like dict:
      {"choices": [{"message": {"content": "<assembled text>"}}]}

    Supports both object and dict chunk shapes.
    """
    # Start streaming call
    stream = await call_target(model=req.model, messages=req.messages, **call_kwargs)

    chunks: List[str] = []

    # Some providers return an async iterator directly; others return an object with __aiter__
    async for ev in stream:
        text: Optional[str] = None

        # Object-shaped delta
        try:
            # OpenAI SDK-like: ev.choices[0].delta.content or message.content
            choice0 = ev.choices[0]  # type: ignore[attr-defined]
            delta = getattr(choice0, "delta", None)
            if delta is not None:
                text = getattr(delta, "content", None)
            if text is None:
                msg = getattr(choice0, "message", None)
                text = getattr(msg, "content", None)
        except Exception:
            pass

        # Dict-shaped delta
        if text is None and isinstance(ev, dict):
            try:
                choices = ev.get("choices") or [{}]
                delta = (choices[0].get("delta") or {})
                text = delta.get("content")
                if text is None:
                    msg = (choices[0].get("message") or {})
                    text = msg.get("content")
            except Exception:
                text = None

        if isinstance(text, str):
            chunks.append(text)

    assembled = "".join(chunks)
    return {"choices": [{"message": {"content": assembled}}]}


async def _acompletion_with_stream_aware_aggregation(router, req: RouterParallelRequest) -> Any:
    """
    Call Router.acompletion for a single request with optional stream aggregation.
    Returns:
      - Non-stream: underlying response object/dict
      - Stream: normalized dict with choices[0].message.content aggregated
    """
    call_kwargs = _merge_kwargs(req)

    call_target = router.__dict__.get("acompletion")
    if call_target is None:
        call_target = router.acompletion

    if call_kwargs.get("stream") is True:
        return await _aggregate_stream(call_target, req, call_kwargs)
    return await call_target(model=req.model, messages=req.messages, **call_kwargs)


def _to_openai_with_meta(resp: Any, timing_ms: Optional[int], req: RouterParallelRequest | None = None) -> Dict[str, Any]:
    """
    Normalize Router.acompletion responses to an OpenAI-shaped dict and attach
    SciLLM router meta (if present) under 'scillm_router'.

    - ModelResponse → {'choices':[{'message':{'content': str}}]}
    - dict (already aggregated stream or provider dict) → copy and attach meta
    """
    meta = None
    content = None
    # Try ModelResponse path first
    try:
        if hasattr(resp, "additional_kwargs"):
            meta = getattr(resp, "additional_kwargs", {}).get("router")
        # choices[0].message.content accessor
        try:
            content = resp.choices[0].message.content  # type: ignore[attr-defined]
        except Exception:
            content = None
    except Exception:
        meta = None

    # Already dict-shaped
    if isinstance(resp, dict):
        out = dict(resp)
        # ensure meta container exists
        out.setdefault("scillm_router", {})
        # attach upstream meta if present
        if meta is not None:
            try:
                out["scillm_router"].update(meta)
            except Exception:
                out["scillm_router"] = meta
        # minimal classification when upstream meta is absent
        try:
            content = ((out.get("choices") or [{}])[0].get("message") or {}).get("content")
            err = "ok"
            jv = None
            if content is None:
                err = "empty_content"
            elif isinstance(content, str):
                if content.strip() == "":
                    err = "empty_content"
                else:
                    try:
                        import json as _json
                        _json.loads(content)
                        jv = True
                    except Exception:
                        jv = False
                        err = "invalid_json"
            # Only stamp if upstream meta didn’t set error_type
            out["scillm_router"].setdefault("error_type", err)
            if jv is not None:
                out["scillm_router"].setdefault("json_valid", jv)
            # best-effort provider
            if req is not None:
                prov = None
                try:
                    prov = (req.kwargs or {}).get("custom_llm_provider")
                except Exception:
                    prov = None
                if prov:
                    out["scillm_router"].setdefault("provider", prov)
            # schema hint
            try:
                rf = (req.kwargs or {}).get("response_format") if req else None
                rm = (req.kwargs or {}).get("response_mode") if req else None
                if isinstance(rf, dict) and rf.get("type") == "json_schema":
                    out["scillm_router"].setdefault("schema_mode", "schema_first")
                elif rm == "schema_first":
                    out["scillm_router"].setdefault("schema_mode", "schema_first")
            except Exception:
                pass
        except Exception:
            pass
        if timing_ms is not None:
            out["scillm_router"].setdefault("timing_ms", timing_ms)
        return out

    # Fallback to minimal OpenAI dict shape
    if not isinstance(content, str):
        # best-effort string
        try:
            content = str(content) if content is not None else ""
        except Exception:
            content = ""
    out = {"choices": [{"message": {"content": content}}]}
    # ensure meta container
    out.setdefault("scillm_router", {})
    if meta is not None:
        out["scillm_router"].update(meta)
    # minimal classification when upstream meta was absent
    try:
        err = "ok"
        jv = None
        if content == "":
            err = "empty_content"
        else:
            try:
                import json as _json
                _json.loads(content)
                jv = True
            except Exception:
                jv = False
                err = "invalid_json"
        out["scillm_router"].setdefault("error_type", err)
        if jv is not None:
            out["scillm_router"].setdefault("json_valid", jv)
        # schema hint from request
        if req is not None:
            rm = (req.kwargs or {}).get("response_mode")
            rf = (req.kwargs or {}).get("response_format")
            if rm == "schema_first" or (isinstance(rf, dict) and rf.get("type") == "json_schema"):
                out["scillm_router"].setdefault("schema_mode", "schema_first")
            prov = (req.kwargs or {}).get("custom_llm_provider")
            if prov:
                out["scillm_router"].setdefault("provider", prov)
    except Exception:
        pass
    if timing_ms is not None:
        out["scillm_router"].setdefault("timing_ms", timing_ms)
    return out


# ------------------------------ Public helpers --------------------------------

async def gather_parallel_acompletions(
    router,
    requests: Sequence[Union[RouterParallelRequest, Dict[str, Any]]],
    *,
    concurrency: Optional[int] = None,
    preserve_order: bool = True,
) -> List[ParallelResult]:
    """
    Run multiple Router.acompletion calls concurrently and return rich results.

    Args:
        router: LiteLLM Router instance.
        requests: List of RouterParallelRequest or dicts (model/messages/knobs).
        concurrency: Optional max in-flight; if None or <=0, unbounded.
        preserve_order: When True, sort results by original index.

    Returns:
        List[ParallelResult] with (index, request, response, exception).
        - For streaming requests (stream=True), `response` is an aggregated dict.
        - For non-streaming, `response` is the provider's response as returned by Router.

    Contract:
        - Exactly one ParallelResult per request, in input order if preserve_order=True.
        - Never returns None slots; failures appear in `.exception`.
    """
    norm_reqs = _normalize_requests(requests)
    sem = asyncio.Semaphore(concurrency) if (concurrency and concurrency > 0) else None

    async def _do_one(i: int, req: RouterParallelRequest) -> ParallelResult:
        try:
            import time as _t
            _t0 = _t.perf_counter()
            if sem:
                async with sem:
                    resp = await _acompletion_with_stream_aware_aggregation(router, req)
            else:
                resp = await _acompletion_with_stream_aware_aggregation(router, req)
            _dt = int(((_t.perf_counter() - _t0) * 1000))
            # Always return OpenAI-shaped dict with 'scillm_router' meta when available
            normalized = _to_openai_with_meta(resp, _dt, req)
            return ParallelResult(index=i, request=req, response=normalized, exception=None, timing_ms=_dt)
        except Exception as e:
            # Emit helpful debug when requested
            import os as _os, sys as _sys, traceback as _tb
            if _os.getenv("SCILLM_DEBUG_PARALLEL") == "1":
                try:
                    print(f"[scillm][parallel] exception idx={i} model={getattr(req,'model',None)} provider={(getattr(req,'kwargs',{}) or {}).get('custom_llm_provider')} err={e}", file=_sys.stderr)
                    _tb.print_exc()
                except Exception:
                    pass
            # Return a normalized error-shaped dict so callers always see scillm_router
            err_out = {
                "choices": [{"message": {"content": None}}],
                "scillm_router": {
                    "error_type": "provider_error",
                    "schema_mode": (getattr(req, 'kwargs', {}) or {}).get('response_mode'),
                    "provider": (getattr(req, 'kwargs', {}) or {}).get('custom_llm_provider'),
                },
            }
            return ParallelResult(index=i, request=req, response=err_out, exception=e, timing_ms=None)

    tasks = [asyncio.create_task(_do_one(i, r)) for i, r in enumerate(norm_reqs)]
    results = await asyncio.gather(*tasks, return_exceptions=False)

    if preserve_order:
        results.sort(key=lambda r: r.index)

    return results


# --------------------------- Back-compat adapter ------------------------------

async def run_parallel_requests(
    router,
    requests: Sequence[Union[RouterParallelRequest, Dict[str, Any]]],
    preserve_order: bool = True,
    return_exceptions: bool = True,
    concurrency: Optional[int] = None,
) -> List[Tuple[int, Any, Optional[Exception]]]:
    """
    DEPRECATED adapter for legacy callers.

    Returns a list of tuples: (index, response, exception)
    - If return_exceptions=False, raises the first exception encountered.
    - If return_exceptions=True, captures exceptions per item.
    """
    prs = await gather_parallel_acompletions(
        router,
        requests,
        concurrency=concurrency,
        preserve_order=preserve_order,
    )

    if not return_exceptions:
        # Fail fast if any exception occurred
        for r in prs:
            if r.exception is not None:
                raise r.exception
        return [(r.index, r.response, None) for r in prs]

    # Tolerant path: surface per-item exceptions
    return [(r.index, r.response, r.exception) for r in prs]
