from __future__ import annotations

# A single import point for the paved path used by project agents.
# This intentionally re-exports the stable helpers that:
# - Use openai_like + Bearer for Chutes
# - Return OpenAI-shaped responses with JSON mode
# - Keep retries deterministic by default (caller can opt into tenacious)

from scillm.extras.chutes_simple import (
    chutes_chat_json,
    chutes_router_json,
    chutes_healthcheck,
)
import json as _json
import urllib.request as _urlreq
import urllib.error as _urlerr
from typing import Optional as _Optional

__all__ = [
    "chutes_chat_json",
    "chutes_router_json",
    "chutes_healthcheck",
    "preflight_text",
    # New helpers to reduce cognitive load for project agents
    "list_models_openai_like",
    "preflight_text_tenacious",
    "sanity_preflight",
    "parallel_preflight_text",
]


def preflight_text(*, api_base: str, api_key: str, model: str, timeout: float = 20.0) -> bool:
    """Minimal, paved-path preflight for text JSON mode.

    - One POST to {api_base}/chat/completions with Authorization: Bearer
    - response_format={"type":"json_object"}, temperature=0, small max_tokens
    - No alternates, no client-side retries, no hidden header hedging

    Returns True when HTTP 200 and choices[0].message.content parses as JSON (string containing JSON or a JSON value).
    Otherwise returns False.
    """
    base = (api_base or "").rstrip("/")
    if not base or not api_key or not model:
        return False
    url = f"{base}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": 'Return only {"ok":true} as JSON.'}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 16,
    }
    data = _json.dumps(payload).encode("utf-8")
    req = _urlreq.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with _urlreq.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return False
            body = r.read().decode("utf-8", "replace")
            try:
                j = _json.loads(body)
                content = (j.get("choices") or [{}])[0].get("message", {}).get("content", "")
                if isinstance(content, str) and content:
                    try:
                        _json.loads(content)
                        return True
                    except Exception:
                        return False
                return False
            except Exception:
                return False
    except _urlerr.HTTPError:
        return False
    except Exception:
        return False


def list_models_openai_like(*, api_base: str, api_key: str, timeout: float = 15.0) -> list[str]:
    """GET {api_base}/models trying Bearer first, then x-api-key on 401.

    Returns a list of model ids or an empty list on failure.
    """
    base = (api_base or "").rstrip("/")
    if not base or not api_key:
        return []
    url = f"{base}/models"
    def _req(headers: dict) -> list[str]:
        try:
            req = _urlreq.Request(url, headers=headers, method="GET")
            with _urlreq.urlopen(req, timeout=timeout) as r:
                if r.status != 200:
                    return []
                body = r.read().decode("utf-8", "replace")
                j = _json.loads(body)
                data = j.get("data") or []
                return [m.get("id") for m in data if isinstance(m, dict) and m.get("id")]
        except _urlerr.HTTPError as he:  # fallthrough to caller
            raise he
        except Exception:
            return []
    # Bearer attempt
    try:
        ids = _req({"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
        if ids:
            return ids
    except _urlerr.HTTPError as he:
        if getattr(he, "code", None) != 401:
            return []
    # x-api-key fallback
    return _req({"x-api-key": str(api_key), "Accept": "application/json"})


def preflight_text_tenacious(
    *, api_base: str, api_key: str, model: str,
    wall_time_s: int = 30, timeout: float = 20.0,
    transient_retries: int = 1,
) -> tuple[bool, dict | None]:
    """Strict JSON preflight using the paved path with short tenacious retries.

    Returns (ok, details). details includes exc_type/message/status when failing.
    """
    if not api_base or not api_key or not model:
        return False, {"exc_type": "ValueError", "message": "missing api_base/api_key/model"}
    # Inject env for the paved path call, then restore.
    import os as _os
    _prev = {k: _os.environ.get(k) for k in ("CHUTES_API_BASE","CHUTES_API_KEY","CHUTES_TEXT_MODEL")}
    try:
        _os.environ["CHUTES_API_BASE"] = api_base
        _os.environ["CHUTES_API_KEY"] = api_key
        _os.environ["CHUTES_TEXT_MODEL"] = model
        try:
            resp = chutes_chat_json(
                messages=[{"role":"user","content": 'Return only {"ok":true} as JSON.'}],
                model=model,
                max_tokens=16,
                temperature=0.0,
                timeout=timeout,
                tenacious=True,
                max_wall_time_s=wall_time_s,
                backoff_cap_s=5,
                backoff_base=0.5,
                transient_retries=max(0, int(transient_retries)),
            )
            # Parse content as JSON (string or object)
            try:
                content = resp.choices[0].message.get("content")  # type: ignore[attr-defined]
            except Exception:
                content = None
            if isinstance(content, str):
                try:
                    _json.loads(content)
                    return True, None
                except Exception:
                    return False, {"exc_type": "ValueError", "message": "non-json content"}
            elif content is not None:
                return True, None
            return False, {"exc_type": "Empty", "message": "no content"}
        except Exception as exc:
            status = getattr(exc, "status", None) or getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
            return False, {"exc_type": type(exc).__name__, "message": str(exc), "status": status}
    finally:
        for k, v in _prev.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v


def sanity_preflight(
    *, api_base: str, api_key: str, model: str, require_listed: bool = True,
    wall_time_s: int = 30, timeout: float = 20.0, parallel: int = 2,
    policy: str = "auto",
) -> tuple[bool, str | None, dict | None]:
    """One-call paved preflight with model listing and robust probe policy.

    - Checks model presence via list_models_openai_like (Bearer then x-api-key)
    - Policies (default 'auto'):
        * strict: strict-JSON probe only (preflight_text_tenacious)
        * normal: non-JSON probe only (parallel_preflight_text)
        * auto: run strict and normal in parallel; accept first success
    - Returns (ok, model, details)
    """
    models = list_models_openai_like(api_base=api_base, api_key=api_key, timeout=timeout)
    if require_listed and models and model not in models:
        return False, model, {"reason": "model_not_listed", "available_example": (models[0] if models else None)}

    policy = (policy or "auto").strip().lower()
    if policy not in {"strict", "normal", "auto"}:
        policy = "auto"

    if policy == "strict":
        ok, details = preflight_text_tenacious(api_base=api_base, api_key=api_key, model=model, wall_time_s=wall_time_s, timeout=timeout)
        return ok, model, details
    if policy == "normal":
        ok, details = parallel_preflight_text(api_base=api_base, api_key=api_key, model=model, parallel=max(1, parallel), wall_time_s=wall_time_s, timeout=timeout)
        return ok, model, details

    # auto: race strict vs normal and accept first success
    import asyncio as _asyncio

    async def _race():
        async def _run_strict():
            # run in thread to reuse sync helper
            loop = _asyncio.get_event_loop()
            return await loop.run_in_executor(None, lambda: preflight_text_tenacious(api_base=api_base, api_key=api_key, model=model, wall_time_s=wall_time_s, timeout=timeout))

        async def _run_normal():
            return await _asyncio.get_event_loop().run_in_executor(None, lambda: parallel_preflight_text(api_base=api_base, api_key=api_key, model=model, parallel=max(1, parallel), wall_time_s=wall_time_s, timeout=timeout))

        t1 = _asyncio.create_task(_run_strict())
        t2 = _asyncio.create_task(_run_normal())
        pending = {t1, t2}
        last_details = None
        try:
            while pending:
                done, pending = await _asyncio.wait(pending, return_when=_asyncio.FIRST_COMPLETED)
                for d in done:
                    ok, details = await d
                    if ok:
                        for p in pending:
                            p.cancel()
                        return True, details
                    last_details = details
            return False, last_details
        finally:
            for p in pending:
                p.cancel()

    try:
        ok, details = _asyncio.run(_race())
    except RuntimeError:
        loop = _asyncio.get_event_loop()
        ok, details = loop.run_until_complete(_race())
    return ok, model, details


def parallel_preflight_text(
    *, api_base: str, api_key: str, model: str,
    parallel: int = 2, wall_time_s: int = 30, timeout: float = 20.0,
) -> tuple[bool, dict | None]:
    """Launch multiple strict JSON probes concurrently; succeed on first pass.

    Uses scillm.acompletion directly (openai_like + Bearer canonicalization).
    Returns (ok, details) with failure details from the last completed attempt when all fail.
    """
    if parallel <= 1:
        return preflight_text_tenacious(api_base=api_base, api_key=api_key, model=model, wall_time_s=wall_time_s, timeout=timeout)
    import asyncio as _asyncio
    from scillm import acompletion as _acompletion  # type: ignore
    prompt = [{"role": "user", "content": 'Return only {"ok":true} as JSON.'}]

    async def _one() -> tuple[bool, dict | None]:
        start = _asyncio.get_event_loop().time()
        # simple short-loop retries inside each task to mimic tenacity
        attempt = 0
        last: dict | None = None
        while True:
            attempt += 1
            try:
                resp = await _acompletion(
                    model=model,
                    api_base=api_base,
                    api_key=api_key,
                    custom_llm_provider="openai_like",
                    messages=prompt,
                    response_format={"type": "json_object"},
                    temperature=0,
                    max_tokens=16,
                    timeout=timeout,
                    empty_retries=0,
                    retry_on_empty=False,
                )
                content = resp.choices[0].message.get("content") if hasattr(resp, "choices") else ((resp.get("choices", [{}])[0]).get("message", {}).get("content", ""))  # type: ignore[index]
                if isinstance(content, str):
                    try:
                        _json.loads(content)
                        return True, None
                    except Exception:
                        last = {"exc_type": "ValueError", "message": "non-json content"}
                else:
                    return True, None
            except Exception as exc:
                status = getattr(exc, "status", None) or getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
                last = {"exc_type": type(exc).__name__, "message": str(exc), "status": status}
            # time check
            if (_asyncio.get_event_loop().time() - start) >= wall_time_s:
                return False, last
            # small backoff
            await _asyncio.sleep(0.5)

    async def _run_many() -> tuple[bool, dict | None]:
        tasks = [_asyncio.create_task(_one()) for _ in range(int(parallel))]
        done: set = set()
        last_detail: dict | None = None
        while tasks:
            done, tasks = await _asyncio.wait(tasks, return_when=_asyncio.FIRST_COMPLETED)
            for t in done:
                ok, detail = await t
                if ok:
                    # cancel remaining
                    for p in tasks:
                        p.cancel()
                    return True, None
                last_detail = detail or last_detail
            # if no tasks left, break
            if not tasks:
                break
        return False, last_detail

    # run
    try:
        return _asyncio.run(_run_many())
    except RuntimeError:
        # if already in loop, create nested
        loop = _asyncio.get_event_loop()
        return loop.run_until_complete(_run_many())
