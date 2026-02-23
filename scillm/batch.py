from __future__ import annotations
"""Parallel async LLM completion engine with concurrency control and retry logic."""

import asyncio as _asyncio
import json as _json
from typing import List, Dict, AsyncIterator, Any, Optional, Callable

try:
    from scillm.extras.json_utils import clean_json_string as _clean_json_string  # type: ignore
except Exception:  # pragma: no cover - optional
    _clean_json_string = None

from . import acompletion as _acompletion  # reuse wrapper
import os as _os
from .preprocess import expand_requests_io as _expand_requests_io

__all__ = ["parallel_acompletions", "parallel_acompletions_iter", "parallel_acompletions_env", "parallel_acompletions_simple", "parallel_acompletions_simple_env"]


async def parallel_acompletions(
    requests: List[Dict],
    *,
    api_base: str | None = None,
    api_key: str | None = None,
    custom_llm_provider: str = "openai_like",
    router: Any | None = None,
    model_list: Optional[List[Dict]] = None,
    concurrency: int = 6,
    tenacious: bool = True,
    wall_time_s: float = 900.0,
    timeout: float = 20.0,
    backoff_base: float = 0.5,
    backoff_cap_s: float = 30.0,
    default_max_tokens: int | None = None,
    default_temperature: float | None = None,
    response_format: dict | None = None,
    # NEW: structured JSON helpers
    schema: Any | None = None,  # jsonschema dict or callable(payload) -> Any
    retry_invalid_json: int = 0,
    repair_invalid_json: Optional[bool] = None,
    retry_5xx: Optional[bool] = None,
    max_retries_5xx: Optional[int] = None,
    backoff_base_5xx: Optional[float] = None,
    backoff_cap_5xx: Optional[float] = None,
) -> list:
    """Batch async chat completions with bounded concurrency and optional tenacity.

    Each request item may contain: {model, messages, api_base?, api_key?, max_tokens?, temperature?, response_format?}.
    Returns a list ordered like `requests`. On failure, an item is a dict: {"error": str, "status": int|None}.
    """
    sem = _asyncio.Semaphore(max(1, int(concurrency)))
    results: list = [None] * len(requests)

    # Fill CHUTES env defaults if not provided
    if not api_base:
        api_base = (_os.environ.get("CHUTES_API_BASE") or "").strip() or None
    if not api_key:
        api_key = (_os.environ.get("CHUTES_API_KEY") or "").strip() or None
    if not api_base or not api_key:
        raise ValueError(
            "parallel_acompletions requires api_base and api_key "
            "(set CHUTES_API_BASE / CHUTES_API_KEY or pass explicitly)"
        )

    # Optional Router support
    _router = router
    if _router is None and model_list:
        try:
            from . import Router as _Router  # lazy import to avoid cycles
            _router = _Router(model_list=model_list)
        except Exception:
            _router = None

    # Default models from env if missing on a request
    text_default = _os.environ.get("CHUTES_MODEL_ID") or _os.environ.get("CHUTES_TEXT_MODEL")
    vlm_default = _os.environ.get("CHUTES_VLM_MODEL")
    strict_env = str(_os.environ.get("SCILLM_JSON_STRICT", "0")).lower() in {"1", "true", "yes", "on"}
    env_repair_default = str(_os.environ.get("SCILLM_REPAIR_INVALID_JSON", "0")).lower() in {"1", "true", "yes", "on"}
    effective_repair = env_repair_default if repair_invalid_json is None else bool(repair_invalid_json)
    def _needs_vlm(req: dict) -> bool:
        try:
            msgs = req.get("messages") or []
            for m in msgs:
                content = m.get("content") if isinstance(m, dict) else None
                # OpenAI multimodal content is a list of parts
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "image_url":
                            return True
                # Some callers may embed a single image_url dict
                if isinstance(content, dict) and "image_url" in content:
                    return True
        except Exception:
            return False
        return False

    # Built-in detection for explicit IO fields: url, file_path, urls[], paths[]
    # This restores historical convenience while avoiding hidden I/O on plain strings.
    try:
        requests = _expand_requests_io(requests)
    except Exception:
        pass

    missing_model = []
    for idx, r in enumerate(requests):
        if not isinstance(r, dict):
            continue
        if r.get("model"):
            continue
        if _needs_vlm(r) and vlm_default:
            r["model"] = vlm_default
        elif text_default:
            r["model"] = text_default
        if not r.get("model"):
            missing_model.append(idx)
    if missing_model:
        raise ValueError(
            f"parallel_acompletions missing model for request(s): {missing_model}. "
            "Set model per request or set CHUTES_MODEL_ID/CHUTES_TEXT_MODEL."
        )

    retry_5xx_eff = bool(str(_os.environ.get("SCILLM_RETRY_5XX", "1")).lower() in {"1","true","yes","on"}) if retry_5xx is None else bool(retry_5xx)
    max_retries_5xx_eff = int(_os.environ.get("SCILLM_MAX_RETRIES_5XX", "3")) if max_retries_5xx is None else int(max_retries_5xx)
    backoff_base_5xx_eff = float(_os.environ.get("SCILLM_BACKOFF_BASE_5XX", "0.5")) if backoff_base_5xx is None else float(backoff_base_5xx)
    backoff_cap_5xx_eff = float(_os.environ.get("SCILLM_BACKOFF_CAP_5XX", "8")) if backoff_cap_5xx is None else float(backoff_cap_5xx)

    def _validate_payload(payload: Any) -> Optional[str]:
        """Return None if valid; error string otherwise."""
        if schema is None:
            return None
        try:
            if callable(schema):
                schema(payload)  # may raise
                return None
            # assume jsonschema-like dict
            try:
                import jsonschema  # type: ignore
            except Exception:
                return "jsonschema_not_installed"
            try:
                jsonschema.validate(payload, schema)  # type: ignore
                return None
            except Exception as e:  # noqa: BLE001
                return str(e)
        except Exception as e:  # noqa: BLE001
            return str(e)

    def _repair_json(text: str) -> tuple[Optional[Any], Optional[str]]:
        """Best-effort repair: trim to outer braces and parse."""
        try:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                trimmed = text[start : end + 1]
                return _json.loads(trimmed), None
        except Exception as e:  # noqa: BLE001
            return None, str(e)
        return None, "no_braces"

    async def _one(idx: int, req: dict):
        model = req.get("model")
        messages = req.get("messages") or []
        _api_base = req.get("api_base") or api_base
        _api_key = req.get("api_key") or api_key
        _rf = req.get("response_format") or response_format
        _mt = req.get("max_tokens", default_max_tokens)
        _temp = req.get("temperature", default_temperature)
        start = _asyncio.get_running_loop().time()
        attempt = 0
        last_err: dict | None = None
        async with sem:
            while True:
                attempt += 1
                try:
                    if _router is not None:
                        resp = await _router.acompletion(
                            model=model,
                            messages=messages,
                            response_format=_rf,
                            max_tokens=_mt,
                            temperature=_temp,
                            timeout=timeout,
                        )
                    else:
                        resp = await _acompletion(
                            model=model,
                            api_base=_api_base,
                            api_key=_api_key,
                            custom_llm_provider=custom_llm_provider,
                            messages=messages,
                            response_format=_rf,
                            max_tokens=_mt,
                            temperature=_temp,
                            timeout=timeout,
                            retry_on_empty=False,
                            empty_retries=0,
                        )
                    # Extract content now to allow json validation/retry
                    try:
                        content = _extract_content_from_response(resp)
                    except Exception:
                        content = None
                    # Validate JSON if requested
                    need_validate = strict_env or schema is not None or retry_invalid_json > 0 or effective_repair
                    repaired_flag = False
                    if need_validate and isinstance(content, str):
                        repaired = False
                        try:
                            parsed = _json.loads(content)
                        except Exception:
                            parsed = None
                        if parsed is None and effective_repair:
                            parsed, repair_err = _repair_json(content)
                            repaired = parsed is not None
                        if parsed is None and effective_repair and _clean_json_string:
                            try:
                                parsed = _clean_json_string(content, return_dict=True)  # type: ignore
                                repaired = parsed is not None
                            except Exception:
                                parsed = None
                        if parsed is not None:
                            schema_err = _validate_payload(parsed)
                            if schema_err:
                                parsed = None
                                je = ValueError(f"schema_invalid: {schema_err}")
                            else:
                                content = parsed
                                repaired_flag = repaired
                        if parsed is None:
                            if attempt <= retry_invalid_json + 1:
                                delay = min(backoff_cap_s, backoff_base * (2 ** max(0, attempt - 1)))
                                try:
                                    await _asyncio.sleep(delay)
                                except Exception:
                                    pass
                                continue
                            results[idx] = {
                                "error": "invalid_json",
                                "status": None,
                                "content": None,
                                "raw": str(content)[:240],
                                "repaired": False,
                            }
                            return
                    results[idx] = {
                        "error": None,
                        "status": None,
                        "content": content,
                        "repaired": repaired_flag,
                        "response": resp,
                    }
                    return
                except Exception as exc:  # normalize transient/backoff
                    status = getattr(exc, "status", None) or getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
                    last_err = {"error": str(exc), "status": status}
                    msg_low = str(exc).lower()
                    transient = (
                        status in {429, 500, 502, 503, 504}
                        or any(k in msg_low for k in ("timeout", "rate limit", "retry", "capacity", "temporarily", "backoff"))
                    )
                    if retry_5xx_eff and transient and status in {500,502,503,504} and attempt <= max_retries_5xx_eff:
                        elapsed = _asyncio.get_running_loop().time() - start
                        if elapsed >= wall_time_s:
                            results[idx] = last_err
                            return
                        delay = min(backoff_cap_5xx_eff, backoff_base_5xx_eff * (2 ** max(0, attempt - 1)))
                        try:
                            await _asyncio.sleep(delay)
                        except Exception:
                            pass
                        continue
                    if not tenacious:
                        results[idx] = last_err
                        return
                    elapsed = _asyncio.get_running_loop().time() - start
                    if elapsed >= wall_time_s:
                        results[idx] = last_err or {"error": "wall_time_exceeded", "status": status}
                        return
                    msg = str(exc).lower()
                    transient = (
                        status in {429, 500, 502, 503, 504} or
                        any(k in msg for k in ("timeout", "rate limit", "retry", "capacity", "temporarily", "backoff"))
                    )
                    if not transient:
                        results[idx] = last_err
                        return
                    delay = min(backoff_cap_s, backoff_base * (2 ** max(0, attempt - 1)))
                    try:
                        await _asyncio.sleep(delay)
                    except Exception:
                        pass

    await _asyncio.gather(*[_one(i, r or {}) for i, r in enumerate(requests)])

    # Normalize to Router-like parallel result objects
    summary = {
        "total": len(results),
        "ok": 0,
        "invalid_json": 0,
        "provider_error": 0,
        "empty_content": 0,
        "other_error": 0,
        "repaired": 0,
    }
    out: List[Dict[str, Any]] = []
    for i, r in enumerate(results):
        req = requests[i] if i < len(requests) else {}
        if isinstance(r, dict) and r.get("error"):
            err = r.get("error") or ""
            if "invalid_json" in err:
                summary["invalid_json"] += 1
            elif "empty_content" in err:
                summary["empty_content"] += 1
            else:
                summary["provider_error"] += 1
            out.append({
                "index": i,
                "request": req,
                "response": None,
                "error": r.get("error"),
                "status": r.get("status"),
                "content": r.get("content"),
                "raw": r.get("raw"),
                "repaired": r.get("repaired"),
            })
        else:
            summary["ok"] += 1
            if isinstance(r, dict) and r.get("repaired"):
                summary["repaired"] += 1
            content = r.get("content") if isinstance(r, dict) else _extract_content_from_response(r)
            out.append({
                "index": i,
                "request": req,
                "response": r if not isinstance(r, dict) else r.get("response"),
                "error": None,
                "status": None,
                "content": content,
                "repaired": r.get("repaired") if isinstance(r, dict) else False,
            })
    # attach summary on the first item to avoid breaking return type
    if out:
        out[0]["summary"] = summary
    return out


async def parallel_acompletions_env(
    requests: List[Dict],
    *,
    router: Any | None = None,
    model_list: Optional[List[Dict]] = None,
    concurrency: int = 6,
    tenacious: bool = True,
    wall_time_s: float = 900.0,
    timeout: float = 20.0,
    backoff_base: float = 0.5,
    backoff_cap_s: float = 30.0,
) -> list:
    """Convenience wrapper that pulls CHUTES env and fills missing fields.

    - Fills model from CHUTES_MODEL_ID or CHUTES_TEXT_MODEL when not provided
    - Fills api_base and api_key from CHUTES_API_BASE / CHUTES_API_KEY
    - Uses openai_like provider
    """
    base = (_os.environ.get("CHUTES_API_BASE") or "").strip()
    key = (_os.environ.get("CHUTES_API_KEY") or "").strip()
    model_default = _os.environ.get("CHUTES_MODEL_ID") or _os.environ.get("CHUTES_TEXT_MODEL")
    reqs: List[Dict] = []
    for r in requests:
        rr = dict(r or {})
        rr.setdefault("model", model_default)
        rr.setdefault("api_base", base)
        rr.setdefault("api_key", key)
        rr.setdefault("custom_llm_provider", "openai_like")
        reqs.append(rr)
    return await parallel_acompletions(
        reqs,
        api_base=base,
        api_key=key,
        custom_llm_provider="openai_like",
        router=router,
        model_list=model_list,
        concurrency=concurrency,
        tenacious=tenacious,
        wall_time_s=wall_time_s,
        timeout=timeout,
        backoff_base=backoff_base,
        backoff_cap_s=backoff_cap_s,
    )


async def parallel_acompletions_iter(
    requests: List[Dict],
    *,
    api_base: str | None = None,
    api_key: str | None = None,
    custom_llm_provider: str = "openai_like",
    router: Any | None = None,
    model_list: Optional[List[Dict]] = None,
    concurrency: int = 6,
    tenacious: bool = True,
    wall_time_s: float = 900.0,
    timeout: float = 20.0,
    backoff_base: float = 0.5,
    backoff_cap_s: float = 30.0,
    default_max_tokens: int | None = None,
    default_temperature: float | None = None,
    response_format: dict | None = None,
    # NEW: structured JSON helpers (iterator parity)
    schema: Any | None = None,
    retry_invalid_json: int = 0,
    repair_invalid_json: Optional[bool] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """Yield results as they complete (as_completed style).

    Each yielded item is {index, request, ok, response|error, status?, attempts, elapsed_s}.
    """
    sem = _asyncio.Semaphore(max(1, int(concurrency)))
    loop = _asyncio.get_running_loop()
    q: _asyncio.Queue = _asyncio.Queue()

    _router = router
    if _router is None and model_list:
        try:
            from . import Router as _Router
            _router = _Router(model_list=model_list)
        except Exception:
            _router = None

    text_default = _os.environ.get("CHUTES_MODEL_ID") or _os.environ.get("CHUTES_TEXT_MODEL")
    vlm_default = _os.environ.get("CHUTES_VLM_MODEL")
    strict_env = str(_os.environ.get("SCILLM_JSON_STRICT", "0")).lower() in {"1", "true", "yes", "on"}
    env_repair_default = str(_os.environ.get("SCILLM_REPAIR_INVALID_JSON", "0")).lower() in {"1", "true", "yes", "on"}
    effective_repair = env_repair_default if repair_invalid_json is None else bool(repair_invalid_json)

    def _needs_vlm(req: dict) -> bool:
        try:
            arts = req.get("artifacts") or {}
            urls = arts.get("urls") or []
            fps = arts.get("file_paths") or []
            if urls:
                return True
            if any(str(p).lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")) for p in fps):
                return True
            msgs = req.get("messages") or []
            for m in msgs:
                content = m.get("content")
                if isinstance(content, list):
                    if any(isinstance(part, dict) and part.get("type") == "image_url" for part in content):
                        return True
        except Exception:
            return False
        return False

    try:
        requests = _expand_requests_io(requests)
    except Exception:
        pass
    for r in requests:
        if isinstance(r, dict) and not r.get("model"):
            if _needs_vlm(r) and vlm_default:
                r["model"] = vlm_default
            elif text_default:
                r["model"] = text_default

    def _validate_payload(payload: Any) -> Optional[str]:
        """Return None if valid; error string otherwise."""
        if schema is None:
            return None
        try:
            if callable(schema):
                schema(payload)  # may raise
                return None
            # assume jsonschema-like dict
            try:
                import jsonschema  # type: ignore
            except Exception:
                return "jsonschema_not_installed"
            try:
                jsonschema.validate(payload, schema)  # type: ignore
                return None
            except Exception as e:  # noqa: BLE001
                return str(e)
        except Exception as e:  # noqa: BLE001
            return str(e)

    def _repair_json(text: str) -> tuple[Optional[Any], Optional[str]]:
        """Best-effort repair: trim to outer braces and parse."""
        try:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                trimmed = text[start : end + 1]
                return _json.loads(trimmed), None
        except Exception as e:  # noqa: BLE001
            return None, str(e)
        return None, "no_braces"

    async def _worker(idx: int, req: dict):
        model = req.get("model")
        messages = req.get("messages") or []
        _api_base = req.get("api_base") or api_base
        _api_key = req.get("api_key") or api_key
        _rf = req.get("response_format") or response_format
        _mt = req.get("max_tokens", default_max_tokens)
        _temp = req.get("temperature", default_temperature)
        start = loop.time()
        attempt = 0
        last_err: dict | None = None
        async with sem:
            while True:
                attempt += 1
                try:
                    if _router is not None:
                        resp = await _router.acompletion(
                            model=model,
                            messages=messages,
                            response_format=_rf,
                            max_tokens=_mt,
                            temperature=_temp,
                            timeout=timeout,
                        )
                    else:
                        resp = await _acompletion(
                            model=model,
                            api_base=_api_base,
                            api_key=_api_key,
                            custom_llm_provider=custom_llm_provider,
                            messages=messages,
                            response_format=_rf,
                            max_tokens=_mt,
                            temperature=_temp,
                            timeout=timeout,
                            retry_on_empty=False,
                            empty_retries=0,
                        )
                    # Extract content now to allow json validation/retry
                    content = _extract_content_from_response(resp)
                    need_validate = strict_env or schema is not None or retry_invalid_json > 0 or effective_repair
                    repaired_flag = False
                    if need_validate and isinstance(content, str):
                        repaired = False
                        try:
                            parsed = _json.loads(content)
                        except Exception:
                            parsed = None
                        if parsed is None and effective_repair:
                            parsed, _ = _repair_json(content)
                            repaired = parsed is not None
                        if parsed is None and effective_repair and _clean_json_string:
                            try:
                                parsed = _clean_json_string(content, return_dict=True)  # type: ignore
                                repaired = parsed is not None
                            except Exception:
                                parsed = None
                        if parsed is not None:
                            schema_err = _validate_payload(parsed)
                            if schema_err:
                                parsed = None
                            else:
                                content = parsed
                                repaired_flag = repaired
                        if parsed is None:
                            if attempt <= retry_invalid_json + 1:
                                delay = min(backoff_cap_s, backoff_base * (2 ** max(0, attempt - 1)))
                                await _asyncio.sleep(delay)
                                continue
                            await q.put({
                                "index": idx,
                                "request": req,
                                "ok": False,
                                "error": "invalid_json",
                                "status": None,
                                "content": None,
                                "raw": str(content)[:240],
                                "repaired": False,
                                "attempts": attempt,
                                "elapsed_s": round(loop.time() - start, 3),
                            })
                            return
                    await q.put({
                        "index": idx,
                        "request": req,
                        "ok": True,
                        "response": resp,
                        "content": content,
                        "repaired": repaired_flag,
                        "attempts": attempt,
                        "elapsed_s": round(loop.time() - start, 3),
                    })
                    return
                except Exception as exc:
                    status = getattr(exc, "status", None) or getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
                    last_err = {"error": str(exc), "status": status}
                    if not tenacious:
                        await q.put({
                            "index": idx,
                            "request": req,
                            "ok": False,
                            "error": last_err["error"],
                            "status": last_err["status"],
                            "content": None,
                            "attempts": attempt,
                            "elapsed_s": round(loop.time() - start, 3),
                        })
                        return
                    if (loop.time() - start) >= wall_time_s:
                        await q.put({
                            "index": idx,
                            "request": req,
                            "ok": False,
                            "error": last_err["error"] if last_err else "wall_time_exceeded",
                            "status": last_err and last_err.get("status"),
                            "content": None,
                            "attempts": attempt,
                            "elapsed_s": round(loop.time() - start, 3),
                        })
                        return
                    # transient backoff
                    msg = str(exc).lower()
                    status_code = last_err and last_err.get("status")
                    transient = (
                        status_code in {429, 500, 502, 503, 504} or
                        any(k in msg for k in ("timeout", "rate limit", "retry", "capacity", "temporarily", "backoff"))
                    )
                    if not transient:
                        await q.put({
                            "index": idx,
                            "request": req,
                            "ok": False,
                            "error": last_err["error"],
                            "status": last_err.get("status"),
                            "content": None,
                            "attempts": attempt,
                            "elapsed_s": round(loop.time() - start, 3),
                        })
                        return
                    delay = min(backoff_cap_s, backoff_base * (2 ** max(0, attempt - 1)))
                    await _asyncio.sleep(delay)

    tasks = [loop.create_task(_worker(i, r or {})) for i, r in enumerate(requests)]

    pending = set(tasks)
    while pending:
        done, pending = await _asyncio.wait(pending, return_when=_asyncio.FIRST_COMPLETED)
        # Drain all currently available queue items
        while not q.empty():
            yield await q.get()
    # Drain any last queued items
    while not q.empty():
        yield await q.get()


def _extract_content_from_response(resp: Any) -> str:
    try:
        if isinstance(resp, dict):
            msg = (resp.get("choices", [{}])[0].get("message", {}) or {})
            content = msg.get("content") or ""
            if not content and isinstance(msg.get("reasoning_content"), str):
                return msg["reasoning_content"]
            return content or ""
        # litellm object
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
    """Minimal wrapper: returns an ordered list of {ok, content, error?, status?}.

    This is the quiet, non-noisy default many users want.
    """
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
    base = (_os.environ.get("CHUTES_API_BASE") or "").strip()
    key = (_os.environ.get("CHUTES_API_KEY") or "").strip()
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
