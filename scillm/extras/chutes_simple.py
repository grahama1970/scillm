from __future__ import annotations

import os
import datetime as _dt
import random
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from scillm import Router, completion
from litellm.exceptions import APIConnectionError, APIError, RateLimitError, Timeout

from .auto_router import auto_router_from_env

# Optional chutes.middleware imports - graceful fallback when unavailable
try:
    from chutes.middleware.budget_guard import budget_register_attempt, budget_snapshot
except ImportError:
    def budget_register_attempt() -> Optional[Dict[str, Any]]:
        """No-op stub when chutes.middleware is unavailable."""
        return None

    def budget_snapshot() -> Optional[Dict[str, Any]]:
        """No-op stub when chutes.middleware is unavailable."""
        return None

try:
    from chutes.middleware.metrics import tenacious_sleep, tenacious_retry
except ImportError:
    def tenacious_sleep(delay: float) -> None:
        """No-op stub when chutes.middleware is unavailable."""
        pass

    def tenacious_retry(reason: str) -> None:
        """No-op stub when chutes.middleware is unavailable."""
        pass

try:  # pragma: no cover - optional telemetry
    from scillm.telemetry import metrics as sc_metrics  # type: ignore
except Exception:  # pragma: no cover
    sc_metrics = None
try:  # pragma: no cover - optional pacing
    from scillm.telemetry import pacing as sc_pacing  # type: ignore
except Exception:  # pragma: no cover
    sc_pacing = None

__all__ = [
    "chutes_chat_json",
    "chutes_router_json",
    "chutes_healthcheck",
]

_DEFAULT_TIMEOUT = 20.0
_DEFAULT_TRANSIENT_RETRIES = 2
_RETRYABLE_HTTP_STATUSES = {502, 503, 504}
_RATE_LIMIT_HTTP_STATUSES = {409, 429}
_PAYG_HTTP_STATUS = 402
_MAX_TRANSIENT_BACKOFF_S = 3.0
_BASE_BACKOFF_S = 0.25
_PROVIDER_NAME = "chutes"


if not hasattr(Router, "is_closed"):
    def _router_is_closed(self: Router) -> bool:  # type: ignore[override]
        return bool(getattr(self, "_closed", False))

    Router.is_closed = _router_is_closed  # type: ignore[assignment]


def _bearer_headers() -> Dict[str, str]:
    key = os.environ.get("CHUTES_API_KEY", "").strip()
    if not key:
        raise RuntimeError("CHUTES_API_KEY not set")
    style = (os.environ.get("CHUTES_AUTH_STYLE") or "bearer").strip().lower()
    if style and style != "bearer":
        try:
            print(f"[scillm.chutes] WARNING: CHUTES_AUTH_STYLE='{style}' ignored; using Bearer header.")
        except Exception:  # pragma: no cover
            pass
    return {"Authorization": f"Bearer {key}"}


def _extract_status_code(err: Exception) -> Optional[int]:
    for attr in ("status_code", "status", "http_status"):
        val = getattr(err, attr, None)
        if isinstance(val, int):
            return val
        try:
            if isinstance(val, str) and val.isdigit():
                return int(val)
        except Exception:  # pragma: no cover
            continue
    return None


def _extract_retry_after_seconds(err: Exception) -> Optional[float]:
    val = getattr(err, "retry_after", None)
    if val is not None:
        try:
            return max(float(val), 0.0)
        except Exception:  # pragma: no cover
            pass
    txt = (str(err) or "").lower()
    if "retry-after" in txt:
        for token in txt.replace(",", " ").split():
            if token.isdigit():
                return float(token)
    return None


def _extract_reset_after_seconds(err: Exception) -> Optional[float]:
    """If the exception carries an x-budget-reset-at header, return seconds until that time.
    Accepts ISO8601 with optional 'Z'.
    """
    try:
        resp = getattr(err, "response", None)
        headers = getattr(resp, "headers", None) or {}
        reset_at = headers.get("x-budget-reset-at") or headers.get("X-Budget-Reset-At")
        if not reset_at:
            return None
        s = str(reset_at).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        tgt = _dt.datetime.fromisoformat(s)
        if tgt.tzinfo is None:
            tgt = tgt.replace(tzinfo=_dt.timezone.utc)
        now = _dt.datetime.now(tz=_dt.timezone.utc)
        delta = (tgt - now).total_seconds()
        return max(0.0, float(delta))
    except Exception:
        return None


def _classify_transient(err: Exception) -> Tuple[Optional[str], Optional[float]]:
    reason_attr = getattr(err, "reason", None)
    if isinstance(reason_attr, str) and reason_attr:
        return reason_attr, _extract_retry_after_seconds(err)
    status = _extract_status_code(err)
    txt = (str(err) or "").lower()
    if isinstance(err, RateLimitError) or (status in _RATE_LIMIT_HTTP_STATUSES):
        return "429_capacity", _extract_retry_after_seconds(err)
    if status in _RETRYABLE_HTTP_STATUSES:
        return "5xx", _extract_retry_after_seconds(err)
    if isinstance(err, Timeout) or "timeout" in txt:
        return "timeout", _extract_retry_after_seconds(err)
    if isinstance(err, APIConnectionError) or any(kw in txt for kw in ("connection reset", "temporarily unavailable", "transport error")):
        return "transport", _extract_retry_after_seconds(err)
    if isinstance(err, APIError) and status and 500 <= status < 600:
        return "5xx", _extract_retry_after_seconds(err)
    if "429" in txt or "capacity" in txt:
        return "429_capacity", _extract_retry_after_seconds(err)
    return None, None


def _record_retry(reason: Optional[str]) -> None:
    if not reason:
        return
    try:
        if sc_metrics:
            sc_metrics.record_retry(reason=reason)
    except Exception:  # pragma: no cover
        pass
    if reason == "429_capacity" and sc_pacing is not None:
        try:
            sc_pacing.note_429_capacity()
        except Exception:  # pragma: no cover
            pass


def _transient_should_retry(err: Exception, attempt: int, max_transient_retries: int) -> Tuple[bool, Optional[float], Optional[str]]:
    reason, hint = _classify_transient(err)
    if not reason:
        return False, None, None
    if reason == "429_capacity" and attempt >= 1:
        return False, None, reason
    if attempt >= max_transient_retries:
        return False, None, reason
    return True, hint, reason


def _sleep_with_backoff(attempt: int, hint: Optional[float]) -> float:
    if hint is not None and hint > 0:
        delay = min(hint, _MAX_TRANSIENT_BACKOFF_S)
    else:
        delay = min((_BASE_BACKOFF_S * (2 ** attempt)) + random.uniform(0.0, 0.25), _MAX_TRANSIENT_BACKOFF_S)
    time.sleep(delay)
    return delay


def _env_true(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or "").strip().lower() in {"1", "true", "yes", "y"}


def _attach_metadata(resp: Any, *, model: str, attempts: int, tenacious: bool = False) -> None:
    try:
        if not getattr(resp, "model", None):
            setattr(resp, "model", model)
    except Exception:  # pragma: no cover
        pass
    try:
        meta = dict(getattr(resp, "scillm_meta", {}) or {})
        meta.setdefault("served_model", getattr(resp, "model", model))
        meta["attempts"] = attempts
        if tenacious:
            meta["tenacious"] = True
        setattr(resp, "scillm_meta", meta)
    except Exception:  # pragma: no cover
        pass
    try:
        if getattr(resp, "usage", None) is None:
            setattr(resp, "usage", getattr(resp, "usage", {}) or {})
    except Exception:  # pragma: no cover
        pass


class EmptyContentError(APIError):
    def __init__(self, model: str):
        super().__init__(
            502,
            "Empty response content",
            _PROVIDER_NAME,
            model,
        )
        self.reason = "empty_content"
        self.retry_after = None


def _fail_on_empty_enabled() -> bool:
    return os.getenv("SCILLM_ALLOW_EMPTY_RESPONSES", "0").lower() not in {"1", "true", "yes", "y"}


def _message_has_substantive_content(payload: Any) -> bool:
    if isinstance(payload, str):
        return bool(payload.strip())
    if isinstance(payload, list):
        for part in payload:
            if isinstance(part, dict):
                if part.get("type") == "text" and str(part.get("text", "")).strip():
                    return True
                if "content" in part and _message_has_substantive_content(part.get("content")):
                    return True
            elif isinstance(part, str) and part.strip():
                return True
        return False
    if isinstance(payload, dict):
        for value in payload.values():
            if _message_has_substantive_content(value):
                return True
        return False
    return False

def _env_confirm_payg() -> bool:
    return (os.getenv("SCILLM_CONFIRM_PAYG", "0") or "").strip().lower() in {"1", "true", "yes"}


def _request_has_meaningful_prompt(messages: List[Dict[str, Any]]) -> bool:
    for msg in messages:
        role = (msg.get("role") or "").lower()
        if role not in {"user", "system"}:
            continue
        if _message_has_substantive_content(msg.get("content")):
            return True
    return False


def _ensure_nonempty_response(
    resp: Any,
    *,
    request_messages: List[Dict[str, Any]],
    model: str,
    attempts: int,
) -> None:
    if not _fail_on_empty_enabled():
        return
    if not _request_has_meaningful_prompt(request_messages):
        return
    try:
        choice = resp.choices[0]
        content = getattr(choice.message, "content", None)
        if content is None and hasattr(choice.message, "get"):
            try:
                content = choice.message.get("content")
            except Exception:
                content = None
    except Exception:
        return
    if _message_has_substantive_content(content):
        return
    err = EmptyContentError(model=model)
    setattr(err, "scillm_meta", {
        "reason": "empty_content",
        "served_model": getattr(resp, "model", model),
        "attempts": attempts,
    })
    raise err


def _record_success(route: str, model_tier: str, attempts: int, started_at: float) -> None:
    try:
        if sc_metrics:
            sc_metrics.record_request(
                route=route,
                result="ok",
                retried="1" if attempts > 1 else "0",
                model_tier=model_tier,
                latency_s=time.time() - started_at,
            )
    except Exception:  # pragma: no cover
        pass


def _with_transient_retries(
    call: Callable[[], Any],
    *,
    max_transient_retries: int,
    model_tier: str,
    route: str,
    served_model: str,
    request_messages: List[Dict[str, Any]],
) -> Any:
    attempt = 0
    started = time.time()
    while True:
        try:
            resp = call()
            _ensure_nonempty_response(resp, request_messages=request_messages, model=served_model, attempts=attempt + 1)
            _attach_metadata(resp, model=served_model, attempts=attempt + 1)
            _record_success(route, model_tier, attempt + 1, started)
            return resp
        except Exception as exc:
            should_retry, hint, reason = _transient_should_retry(exc, attempt, max_transient_retries)
            if not should_retry:
                raise
            _record_retry(reason)
            try:
                if reason:
                    tenacious_retry(reason)
            except Exception:
                pass
            _sleep_with_backoff(attempt, hint)
            attempt += 1


def chutes_chat_json(
    *,
    messages: List[Dict[str, Any]],
    model: Optional[str] = None,
    max_tokens: int = 256,
    temperature: float = 0.0,
    timeout: float = _DEFAULT_TIMEOUT,
    tenacious: bool = False,
    max_wall_time_s: int = 6 * 3600,
    backoff_cap_s: int = 300,
    backoff_base: float = 2.0,
    transient_retries: int = _DEFAULT_TRANSIENT_RETRIES,
) -> Any:
    base = os.environ.get("CHUTES_API_BASE", "").strip()
    if not base:
        raise RuntimeError("CHUTES_API_BASE not set")
    if not model:
        model = os.environ.get("CHUTES_TEXT_MODEL", "").strip()
        if not model:
            raise RuntimeError("CHUTES_TEXT_MODEL not set (and no model= provided)")
    if os.getenv("SCILLM_TENACIOUS", "0").lower() in {"1", "true", "yes", "y"}:
        tenacious = True
    if timeout <= 0:
        timeout = _DEFAULT_TIMEOUT

    headers = _bearer_headers()

    if not tenacious:
        if sc_pacing is not None:
            try:
                sc_pacing.wait_if_needed()
            except Exception:  # pragma: no cover
                pass

        def _call() -> Any:
            # Optional local budget enforcement
            if os.getenv("SCILLM_BUDGET_ENFORCE_LOCAL", "0").lower() in {"1","true","yes","y"}:
                try:
                    snap = budget_snapshot() or {}
                    if int(snap.get("remaining", 1)) <= 0:
                        # Provide a RateLimitError consistent with proxy behavior
                        err = RateLimitError(
                            message="local budget exhausted",
                            llm_provider="openai_like",
                            model=model,
                        )
                        # Attach reset hint when known
                        try:
                            rst = snap.get("reset_at")
                            if rst:
                                setattr(err, "response", type("_R", (), {"headers": {"x-budget-reset-at": rst}})())
                        except Exception:
                            pass
                        raise err
                except Exception:
                    pass
            return completion(
                model=model,
                api_base=base,
                api_key=None,
                custom_llm_provider="openai_like",
                extra_headers=headers,
                messages=messages,
                response_format={"type": "json_object"},
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout,
                max_retries=0,
            )

        return _with_transient_retries(
            _call,
            max_transient_retries=max(0, transient_retries),
            model_tier="text",
            route="direct",
            served_model=model,
            request_messages=messages,
        )

    # Tenacious loop: keep trying single model until success or wall time
    start = time.time()
    attempt = 0
    total_sleep_s = 0.0
    last_retry_after_s: Optional[float] = None
    confirm_payg_used = False
    while True:
        attempt += 1
        try:
            if sc_pacing is not None:
                try:
                    total_sleep_s += sc_pacing.wait_if_needed()
                except Exception:  # pragma: no cover
                    pass
            # Register attempt for budget tracking
            try:
                snap = budget_register_attempt()
            except Exception:
                snap = None
            resp = completion(
                model=model,
                api_base=base,
                api_key=None,
                custom_llm_provider="openai_like",
                extra_headers=headers,
                messages=messages,
                response_format={"type": "json_object"},
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout,
                max_retries=0,
                **({"confirm_payg": True} if confirm_payg_used else {}),
            )
            _ensure_nonempty_response(resp, request_messages=messages, model=model, attempts=attempt)
            _attach_metadata(resp, model=model, attempts=attempt, tenacious=True)
            try:
                meta = dict(getattr(resp, "scillm_meta", {}) or {})
                meta.update({
                    "tenacious_attempts": attempt,
                    "tenacious_total_sleep_s": total_sleep_s,
                    "tenacious_last_retry_after_s": last_retry_after_s,
                    "budget": budget_snapshot() or {},
                })
                setattr(resp, "scillm_meta", meta)
            except Exception:  # pragma: no cover
                pass
            _record_success("direct", "text", attempt, start)
            return resp
        except Exception as exc:
            reason, hint = _classify_transient(exc)
            # Handle one-shot PAYG confirmation retry if enabled
            is_payg, _ = _is_payg_confirmation(exc)
            if is_payg and not confirm_payg_used and _env_confirm_payg():
                confirm_payg_used = True
                # Retry immediately without advancing attempt (treat as same try)
                try:
                    resp = completion(
                        model=model,
                        api_base=base,
                        api_key=None,
                        custom_llm_provider="openai_like",
                        extra_headers=headers,
                        messages=messages,
                        response_format={"type": "json_object"},
                        max_tokens=max_tokens,
                        temperature=temperature,
                        timeout=timeout,
                        max_retries=0,
                        confirm_payg=True,
                    )
                    _ensure_nonempty_response(resp, request_messages=messages, model=model, attempts=attempt)
                    _attach_metadata(resp, model=model, attempts=attempt, tenacious=True)
                    try:
                        meta = dict(getattr(resp, "scillm_meta", {}) or {})
                        meta.update({
                            "tenacious_attempts": attempt,
                            "tenacious_total_sleep_s": total_sleep_s,
                            "tenacious_last_retry_after_s": last_retry_after_s,
                            "budget": budget_snapshot() or {},
                            "payg_confirmed": True,
                        })
                        setattr(resp, "scillm_meta", meta)
                    except Exception:
                        pass
                    _record_success("direct", "text", attempt, start)
                    return resp
                except Exception:
                    # Fall through to normal retry logic
                    pass
            if not reason:
                raise type(exc)(f"{exc} (not retried: auth/mapping/schema)") from exc
            _record_retry(reason)
            if reason == "429_capacity" and sc_pacing is not None:
                try:
                    sc_pacing.note_429_capacity()
                except Exception:  # pragma: no cover
                    pass
            # Auto-wait until reset if enabled and header present
            if reason == "429_capacity" and _env_true("SCILLM_TENACIOUS_UNTIL_RESET", "0"):
                wait_reset = _extract_reset_after_seconds(exc)
                if wait_reset is not None and wait_reset > 0:
                    remaining_wall = max(0.0, max_wall_time_s - (time.time() - start))
                    delay = min(wait_reset, remaining_wall)
                    try:
                        tenacious_sleep(delay)
                    except Exception:
                        pass
                    time.sleep(delay)
                    last_retry_after_s = delay
                    total_sleep_s += delay
                    if time.time() - start > max_wall_time_s:
                        raise Timeout("tenacious wall time exceeded while waiting for reset") from exc
                    continue
            last_retry_after_s = hint
            delay = _tenacious_sleep(attempt, hint, base=backoff_base, cap_s=backoff_cap_s)
            total_sleep_s += delay
            if time.time() - start > max_wall_time_s:
                raise Timeout(f"tenacious wall time exceeded after {attempt} attempts") from exc


def _tenacious_sleep(attempt: int, retry_after_hint: Optional[float], *, base: float, cap_s: int) -> float:
    if retry_after_hint is not None and retry_after_hint > 0:
        delay = min(retry_after_hint, float(cap_s))
    else:
        delay = min((base ** attempt) + random.uniform(0.0, 1.0), float(cap_s))
    time.sleep(delay)
    return delay


def chutes_router_json(
    *,
    messages: List[Dict[str, Any]],
    kind: str = "text",
    max_retries: int = _DEFAULT_TRANSIENT_RETRIES,
    retry_after: float = 2.0,
    timeout: float = _DEFAULT_TIMEOUT,
    tenacious: bool = False,
    max_wall_time_s: int = 6 * 3600,
    backoff_cap_s: int = 300,
    backoff_base: float = 2.0,
) -> Any:
    base = os.environ.get("CHUTES_API_BASE", "").strip()
    if not base:
        raise RuntimeError("CHUTES_API_BASE not set")
    headers = _bearer_headers()
    env_primary = os.environ.get("CHUTES_TEXT_MODEL" if kind != "vlm" else "CHUTES_VLM_MODEL", "").strip()
    if os.environ.get("CHUTES_TEXT_MODEL_ALT1") or os.environ.get("CHUTES_TEXT_MODEL_ALT2") or \
       os.environ.get("CHUTES_VLM_MODEL_ALT1") or os.environ.get("CHUTES_VLM_MODEL_ALT2"):
        raise RuntimeError("LOCKED: Only one pinned CHUTES_*_MODEL is allowed. Remove ALT1/ALT2 envs.")

    group = "chutes/text" if kind != "vlm" else "chutes/vlm"
    if os.getenv("SCILLM_TENACIOUS", "0").lower() in {"1", "true", "yes", "y"}:
        tenacious = True
    if timeout <= 0:
        timeout = _DEFAULT_TIMEOUT

    if env_primary:
        router = Router(model_list=[
            {
                "model_name": group,
                "litellm_params": {
                    "custom_llm_provider": "openai_like",
                    "api_base": base,
                    "api_key": None,
                    "extra_headers": headers,
                    "model": env_primary,
                },
            }
        ], default_litellm_params={"timeout": timeout})
        result: Any
        try:
            if tenacious:
                start = time.time()
                attempt = 0
                total_sleep_s = 0.0
                last_retry_after_s: Optional[float] = None
                while True:
                    attempt += 1
                    try:
                        if sc_pacing is not None:
                            try:
                                total_sleep_s += sc_pacing.wait_if_needed()
                            except Exception:  # pragma: no cover
                                pass
                        # Register attempt for budget tracking
                        try:
                            snap = budget_register_attempt()
                        except Exception:
                            snap = None
                        # Optional local budget enforcement
                        if os.getenv("SCILLM_BUDGET_ENFORCE_LOCAL", "0").lower() in {"1","true","yes","y"}:
                            try:
                                snap = budget_snapshot() or {}
                                if int(snap.get("remaining", 1)) <= 0:
                                    err = RateLimitError(
                                        message="local budget exhausted",
                                        llm_provider="openai_like",
                                        model=env_primary or group,
                                    )
                                    try:
                                        rst = snap.get("reset_at")
                                        if rst:
                                            setattr(err, "response", type("_R", (), {"headers": {"x-budget-reset-at": rst}})())
                                    except Exception:
                                        pass
                                    raise err
                            except Exception:
                                pass
                        resp = router.completion(
                            model=group,
                            messages=messages,
                            response_format={"type": "json_object"},
                            max_retries=0,
                            retry_after=retry_after,
                            timeout=timeout,
                        )
                        _ensure_nonempty_response(resp, request_messages=messages, model=env_primary or group, attempts=attempt)
                        _attach_metadata(resp, model=env_primary or group, attempts=attempt, tenacious=True)
                        meta = dict(getattr(resp, "scillm_meta", {}) or {})
                        meta.update({
                            "tenacious_attempts": attempt,
                            "tenacious_total_sleep_s": total_sleep_s,
                            "tenacious_last_retry_after_s": last_retry_after_s,
                            "budget": budget_snapshot() or {},
                        })
                        setattr(resp, "scillm_meta", meta)
                        _record_success("router", "vlm" if kind == "vlm" else "text", attempt, start)
                        result = resp
                        break
                    except Exception as exc:
                        reason, hint = _classify_transient(exc)
                        if not reason:
                            raise
                        _record_retry(reason)
                        try:
                            if reason:
                                tenacious_retry(reason)
                        except Exception:
                            pass
                        if reason == "429_capacity" and sc_pacing is not None:
                            try:
                                sc_pacing.note_429_capacity()
                            except Exception:  # pragma: no cover
                                pass
                        # Auto-wait until reset if enabled and header present
                        if reason == "429_capacity" and _env_true("SCILLM_TENACIOUS_UNTIL_RESET", "0"):
                            wait_reset = _extract_reset_after_seconds(exc)
                            if wait_reset is not None and wait_reset > 0:
                                remaining_wall = max(0.0, max_wall_time_s - (time.time() - start))
                                delay = min(wait_reset, remaining_wall)
                                try:
                                    tenacious_sleep(delay)
                                except Exception:
                                    pass
                                time.sleep(delay)
                                last_retry_after_s = delay
                                total_sleep_s += delay
                                if time.time() - start > max_wall_time_s:
                                    raise Timeout("tenacious wall time exceeded while waiting for reset") from exc
                                continue
                        last_retry_after_s = hint
                        delay = _tenacious_sleep(attempt, hint, base=backoff_base, cap_s=backoff_cap_s)
                        total_sleep_s += delay
                        if time.time() - start > max_wall_time_s:
                            raise Timeout(f"tenacious wall time exceeded after {attempt} attempts") from exc
                return result
            else:
                if sc_pacing is not None:
                    try:
                        sc_pacing.wait_if_needed()
                    except Exception:  # pragma: no cover
                        pass

                def _call() -> Any:
                    return router.completion(
                        model=group,
                        messages=messages,
                        response_format={"type": "json_object"},
                        max_retries=0,
                        retry_after=retry_after,
                        timeout=timeout,
                    )

                result = _with_transient_retries(
                    _call,
                    max_transient_retries=max(0, max_retries),
                    model_tier="vlm" if kind == "vlm" else "text",
                    route="router",
                    served_model=env_primary or group,
                    request_messages=messages,
                )
                return result
        finally:
            try:
                router.close()
            except Exception:  # pragma: no cover
                pass
    raise RuntimeError("LOCKED: Set a single pinned CHUTES_TEXT_MODEL/CHUTES_VLM_MODEL. Discovery is disabled.")


def chutes_healthcheck(
    *,
    model: Optional[str] = None,
    timeout: float = 10.0,
    transient_retries: int = 1,
) -> Dict[str, Any]:
    base = os.environ.get("CHUTES_API_BASE", "").strip()
    if not base:
        raise RuntimeError("CHUTES_API_BASE not set")
    target_model = model or os.environ.get("CHUTES_TEXT_MODEL", "").strip()
    if not target_model:
        raise RuntimeError("CHUTES_TEXT_MODEL not set (and no model= provided)")
    probe_messages = [
        {"role": "system", "content": 'Return only {"ok":true} as JSON.'},
        {"role": "user", "content": "ping"},
    ]
    try:
        resp = chutes_chat_json(
            messages=probe_messages,
            model=target_model,
            max_tokens=16,
            temperature=0.0,
            timeout=max(timeout, 5.0),
            tenacious=False,
            transient_retries=max(0, transient_retries),
        )
        served = getattr(resp, "model", None) or target_model
        return {"ok": True, "served_model": served}
    except Exception as exc:
        return {"ok": False, "served_model": None, "reason": str(exc)}
def _is_payg_confirmation(err: Exception) -> Tuple[bool, Optional[float]]:
    status = _extract_status_code(err)
    if status == _PAYG_HTTP_STATUS:
        # optional headers may include x-budget-reset-at but not a numeric retry-after
        return True, _extract_retry_after_seconds(err)
    txt = (str(err) or "").lower()
    if "payg" in txt and "confirmation" in txt:
        return True, _extract_retry_after_seconds(err)
    return False, None
