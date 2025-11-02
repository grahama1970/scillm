from __future__ import annotations

import os
from typing import Optional

_PROM = None


def _reg():
    global _PROM
    if _PROM is not None:
        return _PROM
    try:
        from prometheus_client import Counter, Gauge, Histogram  # type: ignore

        _PROM = {
            "calls": Counter(
                "sc_calls_total",
                "SciLLM calls",
                labelnames=("feature", "result", "env"),
            ),
            "latency": Histogram(
                "sc_request_seconds",
                "SciLLM request duration seconds",
                labelnames=("feature", "env"),
                buckets=(0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 30),
            ),
            "retries": Counter(
                "sc_retries_total",
                "SciLLM retries",
                labelnames=("feature", "reason", "env"),
            ),
            "budget_limit": Gauge(
                "sc_budget_limit",
                "Budget limit (calls per window)",
                labelnames=("feature", "env"),
            ),
            "budget_remaining": Gauge(
                "sc_budget_remaining",
                "Budget remaining (calls)",
                labelnames=("feature", "env"),
            ),
            "reset_ts": Gauge(
                "sc_reset_timestamp_seconds",
                "Next budget reset epoch seconds",
                labelnames=("feature", "env"),
            ),
            "spend_today": Gauge(
                "sc_spend_usd_today",
                "Spend today in USD (provider runtime reported)",
                labelnames=("feature", "env"),
            ),
            "payg_active": Gauge(
                "sc_payg_active",
                "PAYG mode active (0/1)",
                labelnames=("feature", "env"),
            ),
            "payg_decisions": Counter(
                "sc_payg_prompts_total",
                "PAYG policy prompts/decisions",
                labelnames=("decision", "env"),
            ),
            "cost_total": Counter(
                "sc_cost_usd_total",
                "Accumulated request cost in USD",
                labelnames=("feature", "vendor", "model", "env"),
            ),
        }
    except Exception:
        _PROM = {}
    return _PROM


def _labels(feature: str):
    env = os.getenv("METRICS_ENV", "")
    return feature, env


def record_call(feature: str, result: str) -> None:
    p = _reg()
    if not p:
        return
    try:
        f, e = _labels(feature)
        p["calls"].labels(f, result, e).inc()
    except Exception:
        pass


def record_latency(feature: str, seconds: float) -> None:
    p = _reg()
    if not p:
        return
    try:
        f, e = _labels(feature)
        p["latency"].labels(f, e).observe(float(seconds))
    except Exception:
        pass


def record_retry(feature: str, reason: str) -> None:
    p = _reg()
    if not p:
        return
    try:
        f, e = _labels(feature)
        p["retries"].labels(f, reason, e).inc()
    except Exception:
        pass


def set_budget(feature: str, *, limit: Optional[int], remaining: Optional[int], reset_epoch: Optional[float]) -> None:
    p = _reg()
    if not p:
        return
    try:
        f, e = _labels(feature)
        if limit is not None:
            p["budget_limit"].labels(f, e).set(float(limit))
        if remaining is not None:
            p["budget_remaining"].labels(f, e).set(float(remaining))
        if reset_epoch is not None:
            p["reset_ts"].labels(f, e).set(float(reset_epoch))
    except Exception:
        pass


__all__ = [
    "record_call",
    "record_latency",
    "record_retry",
    "set_budget",
    "set_spend_today",
    "set_payg_active",
    "record_payg_decision",
    "record_cost_usd",
]


def set_spend_today(feature: str, usd: float) -> None:
    p = _reg()
    if not p:
        return
    try:
        f, e = _labels(feature)
        p["spend_today"].labels(f, e).set(float(usd))
    except Exception:
        pass


def set_payg_active(feature: str, active: bool) -> None:
    p = _reg()
    if not p:
        return
    try:
        f, e = _labels(feature)
        p["payg_active"].labels(f, e).set(1.0 if active else 0.0)
    except Exception:
        pass


def record_payg_decision(decision: str) -> None:
    p = _reg()
    if not p:
        return
    try:
        e = os.getenv("METRICS_ENV", "")
        p["payg_decisions"].labels(decision, e).inc()
    except Exception:
        pass


def record_cost_usd(feature: str, usd: float, *, vendor: str = "", model: str = "") -> None:
    p = _reg()
    if not p:
        return
    try:
        f, e = _labels(feature)
        v = (vendor or "").strip() or "unknown"
        m = (model or "").strip() or "unknown"
        p["cost_total"].labels(f, v, m, e).inc(float(max(0.0, usd)))
    except Exception:
        pass
