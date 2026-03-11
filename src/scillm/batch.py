from __future__ import annotations
"""Parallel async LLM completion engine with concurrency control and retry logic."""

import asyncio as _asyncio
import json as _json
from pathlib import Path as _Path
from typing import List, Dict, AsyncIterator, Any, Optional, Callable

import openai as _openai
from loguru import logger as _logger

from .checkpoint import load_checkpoint as _load_checkpoint, append_checkpoint as _append_checkpoint

try:
    from json_repair import repair_json as _repair_json_lib
except ImportError:
    _repair_json_lib = None
try:
    from rapidfuzz.fuzz import token_set_ratio as _token_set_ratio
except ImportError:
    _token_set_ratio = None
import os as _os
import pathlib as _pathlib
from .preprocess import expand_requests_io as _expand_requests_io

# Cache of AsyncOpenAI clients keyed by (api_base, api_key)
_clients: dict[tuple[str, str], _openai.AsyncOpenAI] = {}


def _get_client(api_base: str, api_key: str) -> _openai.AsyncOpenAI:
    key = (api_base, api_key)
    if key not in _clients:
        _clients[key] = _openai.AsyncOpenAI(base_url=api_base, api_key=api_key)
    return _clients[key]


async def _acompletion(*, model: str, messages: list, api_base: str, api_key: str,
                       timeout: float = 30.0, **kwargs) -> Any:
    """Native openai.AsyncOpenAI chat completion (replaces litellm wrapper)."""
    # Strip kwargs that openai SDK doesn't accept
    kwargs.pop("custom_llm_provider", None)
    kwargs.pop("retry_on_empty", None)
    kwargs.pop("empty_retries", None)
    client = _get_client(api_base, api_key)
    return await client.chat.completions.create(
        model=model, messages=messages, timeout=timeout, **kwargs,
    )

__all__ = ["parallel_acompletions", "parallel_acompletions_iter", "parallel_acompletions_env", "parallel_acompletions_simple", "parallel_acompletions_simple_env"]

# -- Shared helpers --

def _normalize_messages(messages: Any) -> list:
    """Normalize messages to OpenAI format. Accepts plain string or list of dicts."""
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    if not messages:
        return []
    return messages


def _needs_vlm(req: dict) -> bool:
    """Detect if a request contains image content requiring VLM routing."""
    try:
        msgs = _normalize_messages(req.get("messages"))
        for m in msgs:
            content = m.get("content") if isinstance(m, dict) else None
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        return True
        # Also check artifacts (batch convenience fields)
        arts = req.get("artifacts") or {}
        if arts.get("urls"):
            return True
        fps = arts.get("file_paths") or []
        if any(str(p).lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")) for p in fps):
            return True
    except Exception:
        return False
    return False


def _repair_json_text(text: str) -> tuple[Any | None, str | None]:
    """Multi-stage JSON repair: json_repair lib → brace trimming → fail.

    Returns (parsed_object, error_string_or_none).
    """
    # Stage 1: json_repair library (handles trailing commas, single quotes, etc.)
    if _repair_json_lib is not None:
        try:
            repaired = _repair_json_lib(text, return_objects=True)
            if isinstance(repaired, (dict, list)):
                return repaired, None
        except Exception:
            pass
    # Stage 2: brace trimming (handles markdown fences wrapping JSON)
    try:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            trimmed = text[start : end + 1]
            return _json.loads(trimmed), None
    except Exception as e:
        return None, str(e)
    return None, "no_braces"


def _resolve_source(source: str | list[str] | None) -> str | None:
    """Resolve source text for grounding verification.

    Accepts inline text or file paths (auto-detected). Multiple sources are concatenated.
    Returns None if source is empty or unresolvable.
    """
    if source is None:
        return None
    items = [source] if isinstance(source, str) else source
    parts: list[str] = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        p = _pathlib.Path(item)
        if p.is_file():
            try:
                parts.append(p.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                parts.append(item)  # fall back to treating as inline text
        else:
            parts.append(item)
    return "\n".join(parts) if parts else None


def _check_grounding(response_text: str, source_text: str, threshold: float = 0.7) -> float:
    """Check if response_text is grounded in source_text using fuzzy token matching.

    Returns similarity score (0.0–1.0). Requires rapidfuzz; returns 1.0 if not installed.
    """
    if _token_set_ratio is None:
        return 1.0  # can't verify → assume grounded
    if not response_text or not source_text:
        return 0.0
    score = _token_set_ratio(response_text.lower(), source_text.lower())
    return score / 100.0  # rapidfuzz returns 0–100


def _validate_payload(payload: Any, schema: Any) -> str | None:
    """Return None if valid; error string otherwise."""
    if schema is None:
        return None
    try:
        if callable(schema):
            schema(payload)
            return None
        try:
            import jsonschema  # type: ignore
        except Exception:
            return "jsonschema_not_installed"
        try:
            jsonschema.validate(payload, schema)  # type: ignore
            return None
        except Exception as e:
            return str(e)
    except Exception as e:
        return str(e)


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
    # Source grounding verification
    source: str | list[str] | None = None,
    grounding_threshold: float = 0.7,
    grounding_retries: int = 2,
    # Checkpoint / resume
    checkpoint_path: str | _Path | None = None,
) -> list:
    """Batch async chat completions with bounded concurrency and optional tenacity.

    Each request item may contain: {model, messages, api_base?, api_key?, max_tokens?, temperature?, response_format?}.
    Returns a list ordered like `requests`. On failure, an item is a dict: {"error": str, "status": int|None}.

    When *checkpoint_path* is set, each completed result is appended as JSONL.
    If the file already exists, completed indices are resumed (skipped).
    """
    _ckpt = _Path(checkpoint_path) if checkpoint_path is not None else None
    _resumed: dict[int, dict] = _load_checkpoint(_ckpt) if _ckpt else {}
    if _resumed:
        _logger.info(f"Resumed {len(_resumed)} / {len(requests)} from checkpoint")

    sem = _asyncio.Semaphore(max(1, int(concurrency)))
    results: list = [None] * len(requests)

    # Fill env defaults: prefer SCILLM_API_BASE (proxy), fall back to CHUTES_API_BASE (direct)
    if not api_base:
        api_base = (_os.environ.get("SCILLM_API_BASE") or _os.environ.get("CHUTES_API_BASE") or "").strip() or None
    if not api_key:
        api_key = (_os.environ.get("SCILLM_PROXY_KEY") or _os.environ.get("CHUTES_API_KEY") or "").strip() or None
    if not api_base or not api_key:
        raise ValueError(
            "parallel_acompletions requires api_base and api_key "
            "(set SCILLM_API_BASE / SCILLM_PROXY_KEY or pass explicitly)"
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

    # Built-in detection for explicit IO fields: url, file_path, urls[], paths[]
    # This restores historical convenience while avoiding hidden I/O on plain strings.
    try:
        requests = _expand_requests_io(requests)
    except Exception:
        pass

    # Resolve source grounding text (file paths → content, or inline text)
    # Per-request source field overrides the batch-level source.
    _batch_source_text = _resolve_source(source)

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

    def _set_result(idx: int, value: dict) -> None:
        results[idx] = value
        if _ckpt is not None:
            _append_checkpoint(_ckpt, {"index": idx, **value})

    async def _one(idx: int, req: dict):
        if idx in _resumed:
            results[idx] = _resumed[idx]
            return
        model = req.get("model")
        messages = _normalize_messages(req.get("messages"))
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
                            parsed, _ = _repair_json_text(content)
                            repaired = parsed is not None
                        if parsed is not None:
                            schema_err = _validate_payload(parsed, schema)
                            if schema_err:
                                parsed = None
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
                            _set_result(idx, {
                                "error": "invalid_json",
                                "status": None,
                                "content": None,
                                "raw": str(content)[:240],
                                "repaired": False,
                            })
                            return
                    # Grounding verification (opt-in via source field)
                    _src = _resolve_source(req.get("source")) or _batch_source_text
                    _g_threshold = req.get("grounding_threshold", grounding_threshold)
                    _g_retries = req.get("grounding_retries", grounding_retries)
                    _g_score = None
                    _g_attempts = 0
                    if _src and isinstance(content, str):
                        _g_score = _check_grounding(content, _src, _g_threshold)
                        _g_attempts = 1
                        if _g_score < _g_threshold and attempt <= _g_retries:
                            # Progressive retry: add grounding instruction
                            _grounding_hint = (
                                "Base your answer strictly on the provided source text. "
                                "Quote directly from the source."
                                if attempt > 1
                                else "Base your answer strictly on the provided source text."
                            )
                            messages = list(messages) + [
                                {"role": "user", "content": _grounding_hint}
                            ]
                            continue

                    _set_result(idx, {
                        "error": None,
                        "status": None,
                        "content": content,
                        "repaired": repaired_flag,
                        "response": resp,
                        "grounding_score": _g_score,
                        "grounding_attempts": _g_attempts,
                    })
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
                            _set_result(idx, last_err)
                            return
                        delay = min(backoff_cap_5xx_eff, backoff_base_5xx_eff * (2 ** max(0, attempt - 1)))
                        try:
                            await _asyncio.sleep(delay)
                        except Exception:
                            pass
                        continue
                    if not tenacious:
                        _set_result(idx, last_err)
                        return
                    elapsed = _asyncio.get_running_loop().time() - start
                    if elapsed >= wall_time_s:
                        _set_result(idx, last_err or {"error": "wall_time_exceeded", "status": status})
                        return
                    msg = str(exc).lower()
                    transient = (
                        status in {429, 500, 502, 503, 504} or
                        any(k in msg for k in ("timeout", "rate limit", "retry", "capacity", "temporarily", "backoff"))
                    )
                    if not transient:
                        _set_result(idx, last_err)
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
                "grounding_score": r.get("grounding_score") if isinstance(r, dict) else None,
                "grounding_attempts": r.get("grounding_attempts") if isinstance(r, dict) else 0,
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
    """Convenience wrapper that pulls env vars and fills missing fields.

    - Fills model from CHUTES_MODEL_ID or CHUTES_TEXT_MODEL when not provided
    - Fills api_base and api_key from SCILLM_API_BASE / SCILLM_PROXY_KEY (proxy-first)
    - Falls back to CHUTES_API_BASE / CHUTES_API_KEY for backward compat
    - Uses openai_like provider
    """
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
    # Source grounding verification
    source: str | list[str] | None = None,
    grounding_threshold: float = 0.7,
    grounding_retries: int = 2,
    # Checkpoint / resume
    checkpoint_path: str | _Path | None = None,
) -> AsyncIterator[Dict[str, Any]]:
    """Yield results as they complete (as_completed style).

    Each yielded item is {index, request, ok, response|error, status?, attempts, elapsed_s}.
    When *checkpoint_path* is set, completed results are appended as JSONL.
    If the file already exists, resumed results are yielded first with ``"resumed": True``.
    """
    _ckpt = _Path(checkpoint_path) if checkpoint_path is not None else None
    _resumed: dict[int, dict] = _load_checkpoint(_ckpt) if _ckpt else {}
    if _resumed:
        _logger.info(f"Resumed {len(_resumed)} / {len(requests)} from checkpoint")

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

    try:
        requests = _expand_requests_io(requests)
    except Exception:
        pass

    _batch_source_text = _resolve_source(source)

    for r in requests:
        if isinstance(r, dict) and not r.get("model"):
            if _needs_vlm(r) and vlm_default:
                r["model"] = vlm_default
            elif text_default:
                r["model"] = text_default

    async def _emit(result: dict) -> None:
        if _ckpt is not None:
            _append_checkpoint(_ckpt, result)
        await q.put(result)

    async def _worker(idx: int, req: dict):
        if idx in _resumed:
            return  # already completed — resumed results yielded separately
        model = req.get("model")
        messages = _normalize_messages(req.get("messages"))
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
                            parsed, _ = _repair_json_text(content)
                            repaired = parsed is not None
                        if parsed is not None:
                            schema_err = _validate_payload(parsed, schema)
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
                            await _emit({
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
                    # Grounding verification (opt-in via source field)
                    _src = _resolve_source(req.get("source")) or _batch_source_text
                    _g_threshold = req.get("grounding_threshold", grounding_threshold)
                    _g_retries = req.get("grounding_retries", grounding_retries)
                    _g_score = None
                    _g_attempts = 0
                    if _src and isinstance(content, str):
                        _g_score = _check_grounding(content, _src, _g_threshold)
                        _g_attempts = 1
                        if _g_score < _g_threshold and attempt <= _g_retries:
                            _grounding_hint = (
                                "Base your answer strictly on the provided source text. "
                                "Quote directly from the source."
                                if attempt > 1
                                else "Base your answer strictly on the provided source text."
                            )
                            messages = list(messages) + [
                                {"role": "user", "content": _grounding_hint}
                            ]
                            continue

                    await _emit({
                        "index": idx,
                        "request": req,
                        "ok": True,
                        "response": resp,
                        "content": content,
                        "repaired": repaired_flag,
                        "grounding_score": _g_score,
                        "grounding_attempts": _g_attempts,
                        "attempts": attempt,
                        "elapsed_s": round(loop.time() - start, 3),
                    })
                    return
                except Exception as exc:
                    status = getattr(exc, "status", None) or getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
                    last_err = {"error": str(exc), "status": status}
                    if not tenacious:
                        await _emit({
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
                        await _emit({
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
                        await _emit({
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

    # Yield resumed results first (marked so caller can distinguish)
    for _r_idx in sorted(_resumed):
        resumed_item = dict(_resumed[_r_idx])
        resumed_item["resumed"] = True
        yield resumed_item

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


# Convenience wrappers extracted to batch_wrappers.py for size compliance
from .batch_wrappers import (  # noqa: F401
    _extract_content_from_response,
    parallel_acompletions_simple,
    parallel_acompletions_simple_env,
    parallel_acompletions_proxy,
)
