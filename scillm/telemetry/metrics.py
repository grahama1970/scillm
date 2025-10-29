from __future__ import annotations

import os
from typing import Optional

_ENABLED = False
_MODE = None
_INIT_DONE = False

try:
    from prometheus_client import Counter, Histogram, Gauge, start_http_server  # type: ignore
    _PROM_OK = True
except Exception:  # pragma: no cover
    # No-op fallbacks if prometheus-client is not installed
    _PROM_OK = False
    class _Noop:
        def labels(self, *a, **k): return self
        def inc(self, *a, **k): return None
        def dec(self, *a, **k): return None
        def observe(self, *a, **k): return None
        def set(self, *a, **k): return None
    def Counter(*a, **k): return _Noop()  # type: ignore
    def Histogram(*a, **k): return _Noop()  # type: ignore
    def Gauge(*a, **k): return _Noop()  # type: ignore
    def start_http_server(*a, **k): return None  # type: ignore

# Core metric primitives (low-cardinality)
_REQUESTS = Counter(
    "scillm_requests_total", "Total SciLLM requests",
    labelnames=("route", "result", "retried", "model_tier")
)
_LATENCY = Histogram(
    "scillm_request_latency_seconds", "SciLLM request latency (seconds)",
    labelnames=("route", "model_tier"),
    buckets=(0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 30, 60, 120, 300)
)
_RETRIES = Counter(
    "scillm_retries_total", "Total SciLLM retries",
    labelnames=("reason",)
)
_ROUTER_FALLBACKS = Counter(
    "scillm_router_fallbacks_total", "Router fallbacks triggered",
    labelnames=("reason",)
)
_IN_FLIGHT = Gauge(
    "scillm_requests_in_flight", "In-flight SciLLM requests",
    labelnames=("route",)
)


def _bool_env(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def init_from_env() -> None:
    """
    Initialize metrics exporter based on environment variables.
    - SCILLM_METRICS=prom | prometheus   Start a /metrics server.
    - SCILLM_METRICS_ADDR=0.0.0.0        Bind address for /metrics (default 0.0.0.0)
    - SCILLM_METRICS_PORT=9464           Port for /metrics (default 9464)
    Safe to call multiple times.
    """
    global _ENABLED, _MODE, _INIT_DONE
    if _INIT_DONE:
        return
    mode = (os.getenv("SCILLM_METRICS") or "").strip().lower()
    if mode in {"prom", "prometheus"} and _PROM_OK:
        addr = os.getenv("SCILLM_METRICS_ADDR", "0.0.0.0")
        try:
            port = int(os.getenv("SCILLM_METRICS_PORT", "9464"))
        except Exception:
            port = 9464
        try:
            start_http_server(port, addr=addr)  # type: ignore[call-arg]
            _ENABLED = True
            _MODE = "prom"
        except Exception:
            # If server cannot start, stay disabled (no-op)
            _ENABLED = False
            _MODE = None
    _INIT_DONE = True


def _ensure_init() -> None:
    if not _INIT_DONE:
        init_from_env()


def record_request(*, route: str, result: str, retried: str, model_tier: str, latency_s: Optional[float] = None) -> None:
    """
    Record a completed request outcome and latency.
    route: "direct" | "router"
    result: "ok" | "error"
    retried: "0" | "1"
    model_tier: "text" | "vlm" | "tools"
    """
    _ensure_init()
    try:
        _REQUESTS.labels(route, result, retried, model_tier).inc()
        if latency_s is not None:
            _LATENCY.labels(route, model_tier).observe(float(latency_s))
    except Exception:
        pass


def record_retry(*, reason: str) -> None:
    """
    reason: "429_capacity" | "timeout" | "5xx" | "other"
    """
    _ensure_init()
    try:
        _RETRIES.labels(reason).inc()
    except Exception:
        pass


def record_router_fallback(*, reason: str) -> None:
    _ensure_init()
    try:
        _ROUTER_FALLBACKS.labels(reason).inc()
    except Exception:
        pass


class track_in_flight:
    """
    Context manager for tracking in-flight counts.
    Usage:
        with track_in_flight(route="router"):
            ...
    """
    def __init__(self, *, route: str):
        self.route = route

    def __enter__(self):
        _ensure_init()
        try:
            _IN_FLIGHT.labels(self.route).inc()
        except Exception:
            pass
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            _IN_FLIGHT.labels(self.route).dec()
        except Exception:
            pass
        # Do not suppress exceptions
        return False


__all__ = [
    "init_from_env",
    "record_request",
    "record_retry",
    "record_router_fallback",
    "track_in_flight",
]

