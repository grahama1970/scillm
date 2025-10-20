# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import os
import random
import time
from typing import Any, Dict, Optional, Tuple

import httpx
from .errors import CodexCloudError

DEFAULT_BASE = "https://chatgpt.com/backend-api"


def normalize_base_url(input_url: Optional[str]) -> str:
    base = (input_url or os.getenv("CODEX_CLOUD_TASKS_BASE_URL") or DEFAULT_BASE).strip()
    while base.endswith("/"):
        base = base[:-1]
    if (base.startswith("https://chatgpt.com") or base.startswith("https://chat.openai.com")) and "/backend-api" not in base:
        base = f"{base}/backend-api"
    return base


SERVICE_PREFIX = os.getenv("CODEX_CLOUD_SERVICE_PREFIX", "/wham")


def make_async_client(headers: dict[str, str], timeout: float = 60.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=headers, timeout=timeout)


def _parse_retry_after(v: Optional[str]) -> Optional[float]:
    if not v:
        return None
    try:
        return max(0.0, float(v))
    except Exception:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(v)
        delta = (dt - dt.now(dt.tzinfo)).total_seconds() if dt else None
        return max(0.0, delta or 0.0)
    except Exception:
        return None


def request_json(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    timeout_s: float = 30.0,
    retry_enabled: bool = True,
    retry_time_budget_s: float = 60.0,
    retry_max_attempts: int = 8,
    retry_base_s: float = 1.5,
    retry_cap_s: float = 90.0,
    retry_jitter_pct: float = 0.25,
    honor_retry_after: bool = True,
) -> Tuple[int, Dict[str, str], Any, Dict[str, Any]]:
    hdrs = dict(headers or {})
    hdrs.setdefault("content-type", "application/json")
    start = time.time()
    attempts = 0
    total_sleep_s = 0.0
    last_retry_after_s: Optional[float] = None
    request_id: Optional[str] = None

    async_mode = False
    # Use httpx.Client for sync calls; it's available in our env.
    while True:
        attempts += 1
        try:
            with httpx.Client(timeout=timeout_s) as client:
                r = client.request(method.upper(), url, headers=hdrs, json=json_body)
            status = r.status_code
            rh = {k.lower(): v for k, v in r.headers.items()}
            request_id = rh.get("x-request-id") or rh.get("request-id") or request_id
            text = r.text or ""
            try:
                parsed: Any = json.loads(text) if text else None
            except Exception:
                parsed = {"_raw": text}
            if 200 <= status < 300:
                return status, rh, parsed, {
                    "attempts": attempts,
                    "total_sleep_s": round(total_sleep_s, 3),
                    "last_retry_after_s": last_retry_after_s,
                    "request_id": request_id,
                }
            if retry_enabled and (status == 429 or 500 <= status < 600):
                if attempts >= retry_max_attempts or (time.time() - start) > retry_time_budget_s:
                    raise CodexCloudError(
                        f"HTTP {status} after {attempts} attempt(s)",
                        status=status,
                        request_id=request_id,
                        body=parsed,
                        attempt=attempts,
                    )
                if honor_retry_after:
                    ra = _parse_retry_after(rh.get("retry-after"))
                else:
                    ra = None
                if ra is None:
                    # exponential full jitter
                    exp = retry_base_s * (2 ** max(0, attempts - 1))
                    exp = min(exp, retry_cap_s)
                    sleep_s = random.uniform(0, exp)
                    sleep_s = max(0.5, min(retry_cap_s, sleep_s + sleep_s * retry_jitter_pct))
                else:
                    sleep_s = max(0.25, min(retry_cap_s, ra))
                    last_retry_after_s = sleep_s
                remaining = retry_time_budget_s - (time.time() - start)
                if remaining <= 0.25:
                    raise CodexCloudError(
                        "Retry time budget exhausted",
                        status=status,
                        request_id=request_id,
                        body=parsed,
                        attempt=attempts,
                    )
                sleep_s = max(0.25, min(sleep_s, remaining))
                time.sleep(sleep_s)
                total_sleep_s += sleep_s
                continue
            raise CodexCloudError(
                f"HTTP {status}", status=status, request_id=request_id, body=parsed, attempt=attempts
            )
        except CodexCloudError:
            raise
        except Exception as e:
            if not retry_enabled or attempts >= retry_max_attempts or (time.time() - start) > retry_time_budget_s:
                raise CodexCloudError(
                    f"Transport error after {attempts} attempt(s): {e.__class__.__name__}",
                    attempt=attempts,
                ) from None
            exp = retry_base_s * (2 ** max(0, attempts - 1))
            exp = min(exp, retry_cap_s)
            sleep_s = random.uniform(0, exp)
            sleep_s = max(0.25, min(retry_cap_s, sleep_s + sleep_s * retry_jitter_pct))
            remaining = retry_time_budget_s - (time.time() - start)
            sleep_s = max(0.25, min(sleep_s, remaining))
            time.sleep(sleep_s)
            total_sleep_s += sleep_s
