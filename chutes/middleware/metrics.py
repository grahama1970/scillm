from __future__ import annotations

from typing import Optional

import os
_PROM = None


def _load():  # lazy import to avoid hard dependency
    global _PROM
    if _PROM is not None:
        return _PROM
    try:
        from prometheus_client import Counter, Gauge

        _PROM = {
            "calls": Counter(
                "chutes_calls_total",
                "Chutes calls",
                labelnames=("provider", "result", "env"),
            ),
            "e429": Counter(
                "chutes_429_total",
                "Chutes 429s",
                labelnames=("provider", "kind", "env"),
            ),
            "limit": Gauge(
                "chutes_budget_limit",
                "Daily call limit",
                labelnames=("provider", "env"),
            ),
            "remain": Gauge(
                "chutes_budget_remaining",
                "Remaining calls in current window",
                labelnames=("provider", "env"),
            ),
            "reset": Gauge(
                "chutes_reset_timestamp_seconds",
                "Next reset time (epoch seconds)",
                labelnames=("provider", "env"),
            ),
            "ten_retry": Counter(
                "sc_tenacious_retries_total",
                "Tenacious retries",
                labelnames=("provider", "reason", "env"),
            ),
            "ten_sleep": Counter(
                "sc_tenacious_sleep_seconds_total",
                "Tenacious sleep seconds",
                labelnames=("provider", "env"),
            ),
        }
    except Exception:
        _PROM = {}
    return _PROM


def _labels(provider: str = "chutes"):
    env = os.getenv("METRICS_ENV", "")
    return provider, env


def inc_call(result: str, provider: str = "chutes") -> None:
    p = _load()
    if not p:
        return
    try:
        prov, env = _labels(provider)
        p["calls"].labels(prov, result, env).inc()
    except Exception:
        pass


def inc_429(kind: str, provider: str = "chutes") -> None:
    p = _load()
    if not p:
        return
    try:
        prov, env = _labels(provider)
        p["e429"].labels(prov, kind, env).inc()
    except Exception:
        pass


def set_budget(limit: Optional[int], remaining: Optional[int], reset_epoch: Optional[float], provider: str = "chutes") -> None:
    p = _load()
    if not p:
        return
    try:
        prov, env = _labels(provider)
        if limit is not None:
            p["limit"].labels(prov, env).set(float(limit))
        if remaining is not None:
            p["remain"].labels(prov, env).set(float(remaining))
        if reset_epoch is not None:
            p["reset"].labels(prov, env).set(float(reset_epoch))
    except Exception:
        pass


def tenacious_retry(reason: str, provider: str = "chutes") -> None:
    p = _load()
    if not p:
        return
    try:
        prov, env = _labels(provider)
        p["ten_retry"].labels(prov, reason, env).inc()
    except Exception:
        pass


def tenacious_sleep(seconds: float, provider: str = "chutes") -> None:
    p = _load()
    if not p:
        return
    try:
        prov, env = _labels(provider)
        p["ten_sleep"].labels(prov, env).inc(float(seconds))
    except Exception:
        pass
