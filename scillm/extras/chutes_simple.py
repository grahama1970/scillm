from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from scillm import Router, completion
from litellm.exceptions import RateLimitError, APIConnectionError, APIError, Timeout
from .auto_router import auto_router_from_env
try:
    from scillm.telemetry import metrics as sc_metrics  # type: ignore
except Exception:  # pragma: no cover
    sc_metrics = None
try:
    from scillm.telemetry import pacing as sc_pacing  # type: ignore
except Exception:  # pragma: no cover
    sc_pacing = None


def _bearer_headers() -> Dict[str, str]:
    key = os.environ.get("CHUTES_API_KEY", "").strip()
    if not key:
        raise RuntimeError("CHUTES_API_KEY not set")
    return {"Authorization": f"Bearer {key}"}


def _tenacious_should_retry(err: Exception) -> bool:
    msg = (str(err) or "").lower()
    return (
        isinstance(err, (RateLimitError, APIConnectionError, Timeout, APIError))
        or "429" in msg
        or "capacity" in msg
        or "try again" in msg or "temporarily unavailable" in msg
        or "503" in msg or "502" in msg or "504" in msg
        or "timeout" in msg
    ) and not any(x in msg for x in ("400", "401", "403", "404", "422"))


def _tenacious_sleep(attempt: int, retry_after_hint: Optional[int], *, base: float, cap_s: int) -> int:
    import random, time
    if retry_after_hint is not None:
        delay = max(1, min(int(retry_after_hint), cap_s))
    else:
        jitter = random.randint(0, 3)
        delay = min(int(base ** attempt) + jitter, cap_s)
    time.sleep(delay)
    return delay


def chutes_chat_json(
    *,
    messages: List[Dict[str, Any]],
    model: Optional[str] = None,
    max_tokens: int = 256,
    temperature: float = 0.0,
    timeout: float = 45.0,
    tenacious: bool = False,
    max_wall_time_s: int = 6 * 3600,
    backoff_cap_s: int = 300,
    backoff_base: float = 2.0,
) -> Any:
    """
    Zero‑guess JSON chat against a Chutes OpenAI‑compatible host using Bearer.

    Env: CHUTES_API_BASE, CHUTES_API_KEY, CHUTES_TEXT_MODEL (optional default)
    Returns the ModelResponse from scillm.completion.
    """
    base = os.environ.get("CHUTES_API_BASE", "").strip()
    if not base:
        raise RuntimeError("CHUTES_API_BASE not set")
    if not model:
        model = os.environ.get("CHUTES_TEXT_MODEL", "").strip()
        if not model:
            raise RuntimeError("CHUTES_TEXT_MODEL not set (and no model= provided)")
    # Allow env override without code change
    if os.getenv("SCILLM_TENACIOUS", "0") in {"1", "true", "yes"}:
        tenacious = True

    if not tenacious:
        if sc_pacing:
            try:
                sc_pacing.wait_if_needed()
            except Exception:
                pass
        _t0 = __import__("time").time()
        _resp = completion(
            model=model,
            api_base=base,
            api_key=None,
            custom_llm_provider="openai_like",
            extra_headers=_bearer_headers(),
            messages=messages,
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )
        try:
            if sc_metrics:
                sc_metrics.record_request(
                    route="direct", result="ok", retried="0", model_tier="text",
                    latency_s=__import__("time").time() - _t0
                )
        except Exception:
            pass
        return _resp

    # Tenacious outer loop (single model)
    start = __import__("time").time()
    attempt = 0
    total_sleep_s = 0.0
    last_retry_after_s = None
    while True:
        attempt += 1
        try:
            if sc_pacing:
                try:
                    total_sleep_s += sc_pacing.wait_if_needed()
                except Exception:
                    pass
            r = completion(
                model=model,
                api_base=base,
                api_key=None,
                custom_llm_provider="openai_like",
                extra_headers=_bearer_headers(),
                messages=messages,
                response_format={"type": "json_object"},
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout,
                max_retries=0,
            )
            try:
                if sc_metrics:
                    sc_metrics.record_request(
                        route="direct", result="ok",
                        retried=("1" if attempt > 1 else "0"),
                        model_tier="text",
                        latency_s=__import__("time").time() - start
                    )
            except Exception:
                pass
            try:
                setattr(r, "scillm_meta", {
                    "tenacious": True,
                    "attempts": attempt,
                    "total_sleep_s": total_sleep_s,
                    "last_retry_after_s": last_retry_after_s,
                })
            except Exception:
                pass
            return r
        except Exception as e:
            if not _tenacious_should_retry(e):
                raise type(e)(f"{e} (not retried: auth/mapping/schema)") from e
            # Parse retry-after hint if present
            hint = None
            txt = (str(e) or "").lower()
            try:
                if sc_metrics:
                    reason = "429_capacity" if ("429" in txt or "capacity" in txt) else ("5xx" if any(x in txt for x in ("503", "502", "504")) else ("timeout" if "timeout" in txt else "other"))
                    sc_metrics.record_retry(reason=reason)
            except Exception:
                pass
            if "retry-after" in txt:
                for tok in txt.replace(",", " ").split():
                    if tok.isdigit():
                        hint = int(tok)
                        break
            last_retry_after_s = hint
            _tenacious_sleep(attempt, hint, base=backoff_base, cap_s=backoff_cap_s)
            if sc_pacing and ("429" in txt or "capacity" in txt):
                try:
                    sc_pacing.note_429_capacity()
                except Exception:
                    pass
            if __import__("time").time() - start > max_wall_time_s:
                raise Timeout(f"tenacious wall time exceeded after {attempt} attempts") from e


def chutes_router_json(
    *,
    messages: List[Dict[str, Any]],
    kind: str = "text",
    max_retries: int = 3,
    retry_after: float = 2.0,
    timeout: float = 45.0,
    tenacious: bool = False,
    max_wall_time_s: int = 6 * 3600,
    backoff_cap_s: int = 300,
    backoff_base: float = 2.0,
) -> Any:
    """
    One‑liner Router with Bearer auth and JSON response, auto‑discovering peers.

    Requires CHUTES_API_BASE/KEY and optionally CHUTES_TEXT_MODEL to bias peers.
    Returns the ModelResponse from Router.completion.
    """
    # Prefer explicit env pins for determinism; fall back to discovery only if absent.
    base = os.environ.get("CHUTES_API_BASE", "").strip()
    if not base:
        raise RuntimeError("CHUTES_API_BASE not set")
    auth = _bearer_headers()
    env_primary = os.environ.get("CHUTES_TEXT_MODEL" if kind != "vlm" else "CHUTES_VLM_MODEL", "").strip()
    env_alts = [
        os.environ.get(("CHUTES_TEXT_MODEL_ALT1" if kind != "vlm" else "CHUTES_VLM_MODEL_ALT1"), "").strip(),
        os.environ.get(("CHUTES_TEXT_MODEL_ALT2" if kind != "vlm" else "CHUTES_VLM_MODEL_ALT2"), "").strip(),
    ]
    env_alts = [m for m in env_alts if m]

    model_list = []
    group = "chutes/text" if kind != "vlm" else "chutes/vlm"
    if os.getenv("SCILLM_TENACIOUS", "0") in {"1", "true", "yes"}:
        tenacious = True

    if env_primary:
        # Build from env pins only (most reliable)
        all_ids = [env_primary] + env_alts
        for mid in all_ids:
            model_list.append({
                "model_name": group,
                "litellm_params": {
                    "custom_llm_provider": "openai_like",
                    "api_base": base,
                    "api_key": None,
                    "extra_headers": auth,
                    "model": mid,
                },
            })
        router = Router(model_list=model_list, default_litellm_params={"timeout": timeout})
        # Tenacious only applies when exactly one entry
        if tenacious and len(model_list) == 1:
            start = __import__("time").time()
            attempt = 0
            total_sleep_s = 0.0
            last_retry_after_s = None
            while True:
                attempt += 1
                try:
                    if sc_pacing:
                        try:
                            total_sleep_s += sc_pacing.wait_if_needed()
                        except Exception:
                            pass
                    try:
                        if sc_metrics:
                            sc_metrics.record_request(
                                route="router", result="ok",
                                retried=("1" if attempt > 1 else "0"),
                                model_tier=("vlm" if kind == "vlm" else "text"),
                                latency_s=__import__("time").time() - start
                            )
                    except Exception:
                        pass
                    r = router.completion(
                        model=group,
                        messages=messages,
                        response_format={"type": "json_object"},
                        max_retries=0,
                        retry_after=retry_after,
                        timeout=timeout,
                    )
                    try:
                        setattr(r, "scillm_meta", {
                            "tenacious": True,
                            "attempts": attempt,
                            "total_sleep_s": total_sleep_s,
                            "last_retry_after_s": last_retry_after_s,
                        })
                    except Exception:
                        pass
                    return r
                except Exception as e:
                    if not _tenacious_should_retry(e):
                        raise type(e)(f"{e} (not retried: auth/mapping/schema)") from e
                    # Retry-After parsing
                    hint = None
                    txt = (str(e) or "").lower()
                    try:
                        if sc_metrics:
                            reason = "429_capacity" if ("429" in txt or "capacity" in txt) else ("5xx" if any(x in txt for x in ("503", "502", "504")) else ("timeout" if "timeout" in txt else "other"))
                            sc_metrics.record_retry(reason=reason)
                    except Exception:
                        pass
                    if "retry-after" in txt:
                        for tok in txt.replace(",", " ").split():
                            if tok.isdigit():
                                hint = int(tok)
                                break
                    last_retry_after_s = hint
                    _tenacious_sleep(attempt, hint, base=backoff_base, cap_s=backoff_cap_s)
                    if sc_pacing and ("429" in txt or "capacity" in txt):
                        try:
                            sc_pacing.note_429_capacity()
                        except Exception:
                            pass
                    if __import__("time").time() - start > max_wall_time_s:
                        raise Timeout(f"tenacious wall time exceeded after {attempt} attempts") from e
        else:
            if sc_pacing:
                try:
                    sc_pacing.wait_if_needed()
                except Exception:
                    pass
            _t0 = __import__("time").time()
            _r = router.completion(
                model=group,
                messages=messages,
                response_format={"type": "json_object"},
                max_retries=max_retries,
                retry_after=retry_after,
                timeout=timeout,
            )
            try:
                if sc_metrics:
                    sc_metrics.record_request(
                        route="router", result="ok", retried="0",
                        model_tier=("vlm" if kind == "vlm" else "text"),
                        latency_s=__import__("time").time() - _t0
                    )
            except Exception:
                pass
            return _r
    # Fallback: dynamic discovery when no env pins present
    router: Router = auto_router_from_env(kind=kind, require_json=True)
    for e in router.model_list:
        e.setdefault("litellm_params", {}).setdefault("extra_headers", {}).update(auth)
        e["litellm_params"]["api_key"] = None
        e["litellm_params"]["custom_llm_provider"] = "openai_like"
    if sc_pacing:
        try:
            sc_pacing.wait_if_needed()
        except Exception:
            pass
    _t0 = __import__("time").time()
    _r2 = router.completion(
        model=router.model_list[0]["model_name"],
        messages=messages,
        response_format={"type": "json_object"},
        max_retries=max_retries,
        retry_after=retry_after,
        timeout=timeout,
    )
    try:
        if sc_metrics:
            sc_metrics.record_request(
                route="router", result="ok", retried="0",
                model_tier=("vlm" if kind == "vlm" else "text"),
                latency_s=__import__("time").time() - _t0
            )
    except Exception:
        pass
    return _r2


__all__ = ["chutes_chat_json", "chutes_router_json"]
