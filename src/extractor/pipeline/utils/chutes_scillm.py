#!/usr/bin/env python3
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from scillm import completion as sc_completion
from scillm.extras.autoscale import controller
import httpx, time, random


def _base_and_key() -> tuple[str, str]:
    base = (os.getenv("CHUTES_API_BASE") or "").strip()
    key = (os.getenv("CHUTES_API_KEY") or "").strip()
    if not base:
        raise ValueError("CHUTES_API_BASE not set")
    if not key:
        raise ValueError("CHUTES_API_KEY not set")
    return base, key


def _retry_after_seconds(resp: Optional[httpx.Response]) -> float | None:
    try:
        if resp is None:
            return None
        ra = resp.headers.get("Retry-After")
        if not ra:
            return None
        ra = ra.strip()
        # Prefer integer seconds
        if ra.isdigit():
            return float(int(ra))
        # Fallback: HTTP-date; do minimal parse
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(ra)
            return max(0.0, (dt.timestamp() - time.time()))
        except Exception:
            return None
    except Exception:
        return None


def _backoff_sleep(attempt: int, retry_after: float | None, base: float, cap: float) -> None:
    if retry_after is not None and retry_after > 0:
        time.sleep(min(retry_after, cap))
        return
    # Exponential backoff with jitter
    delay = min(cap, base * (2 ** (attempt - 1)))
    jitter = delay * 0.25 * random.random()
    time.sleep(delay + jitter)


def chutes_chat(
    *,
    model: str,
    messages: List[Dict[str, Any]],
    response_format: Optional[Dict[str, Any]] = None,
    temperature: Optional[float] = None,
    timeout: int | float = 60,
) -> Dict[str, Any]:
    base, key = _base_and_key()
    def _call(headers: Dict[str, str]) -> Dict[str, Any]:
        eff_headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            **(headers or {}),
        }
        t0 = time.perf_counter()
        res = sc_completion(
            model=model,
            api_base=base,
            api_key=None,
            custom_llm_provider="openai_like",
            messages=messages,
            response_format=response_format,
            temperature=temperature,
            timeout=timeout,
            extra_headers=eff_headers,
        )
        try:
            controller().note_success(latency_ms=(time.perf_counter() - t0) * 1000.0)
        except Exception:
            pass
        return res
    # Attempt scillm path first with 2 auth styles; then fallback to direct HTTP with backoff
    auth_attempts = [
        {"Authorization": f"Bearer {key}"},
        {"x-api-key": key},
        {"Authorization": key},
    ]
    for hdr in auth_attempts:
        try:
            with controller().acquire():
                return _call(hdr)
        except Exception as e:  # try next style
            last_err = e
    # Direct HTTP with backoff on 429/503
    payload = {"model": model, "messages": messages}
    if response_format is not None:
        payload["response_format"] = response_format
    if temperature is not None:
        payload["temperature"] = temperature
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "application/json"}

    max_attempts = int(os.getenv("CHUTES_BACKOFF_MAX", "3"))
    base_delay = float(os.getenv("CHUTES_BACKOFF_BASE", "1.0"))
    cap_delay = float(os.getenv("CHUTES_BACKOFF_CAP", "8.0"))
    url = base.rstrip('/') + "/chat/completions"
    last_status = None
    with httpx.Client(timeout=httpx.Timeout(timeout=float(timeout or 60), connect=5.0)) as s:
        for attempt in range(1, max_attempts + 1):
            try:
                t0 = time.perf_counter()
                with controller().acquire():
                    r = s.post(url, headers=headers, json=payload)
                if r.status_code in (429, 503):
                    last_status = r.status_code
                    try:
                        controller().note_rate_limit(_retry_after_seconds(r))
                    except Exception:
                        pass
                    _backoff_sleep(attempt, _retry_after_seconds(r), base_delay, cap_delay)
                    continue
                r.raise_for_status()
                try:
                    controller().note_success(latency_ms=(time.perf_counter() - t0) * 1000.0)
                except Exception:
                    pass
                return r.json()
            except httpx.HTTPStatusError as he:
                resp = he.response
                if resp is not None and resp.status_code in (429, 503):
                    last_status = resp.status_code
                    try:
                        controller().note_rate_limit(_retry_after_seconds(resp))
                    except Exception:
                        pass
                    _backoff_sleep(attempt, _retry_after_seconds(resp), base_delay, cap_delay)
                    continue
                raise
            except Exception:
                # Network error; backoff and retry
                _backoff_sleep(attempt, None, base_delay, cap_delay)
                continue
    # Exhausted
    if last_status in (429, 503):
        raise RuntimeError(f"Chutes backoff exhausted (HTTP {last_status}) after {max_attempts} attempts")
    raise last_err


def chutes_chat_json(
    *,
    model: str,
    messages: List[Dict[str, Any]],
    temperature: float = 0.0,
    timeout: int | float = 60,
) -> Dict[str, Any]:
    return chutes_chat(
        model=model,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=temperature,
        timeout=timeout,
    )
