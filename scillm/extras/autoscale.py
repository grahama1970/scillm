#!/usr/bin/env python3
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager


class _TokenBucket:
    def __init__(self, rate_per_sec: float, burst: float | None = None) -> None:
        self.rate = max(0.01, float(rate_per_sec or 1.0))
        self.capacity = float(burst if burst is not None else max(1.0, self.rate))
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def take(self, amount: float = 1.0) -> float:
        """Return seconds to wait to acquire tokens (0 if immediately available)."""
        now = time.monotonic()
        with self._lock:
            # Refill tokens
            elapsed = now - self.updated
            self.updated = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            if self.tokens >= amount:
                self.tokens -= amount
                return 0.0
            deficit = amount - self.tokens
            wait = deficit / self.rate
            # Reserve future tokens
            self.tokens = 0.0
            return max(0.0, wait)


class GlobalAutoScaler:
    """
    Minimal local controller: single-flight concurrency and token-bucket pacing.

    Enabled when SCILLM_AUTOSCALE=1 (defaults to on if not set and profile is present).
    Tunables:
      - SCILLM_QPS_TARGET (float, default 1.0)
      - SCILLM_CONCURRENCY (int, default 1)
      - SCILLM_BURST (float, default equals QPS)
    """

    _instance = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        qps = float(os.getenv("SCILLM_QPS_TARGET", "1.0") or 1.0)
        conc = int(os.getenv("SCILLM_CONCURRENCY", "1") or 1)
        burst = os.getenv("SCILLM_BURST")
        self.enabled = (os.getenv("SCILLM_AUTOSCALE", "1") in {"1", "true", "yes"})
        # Dynamic mode defaults ON when a Chutes base is present, unless explicitly disabled
        chutes_present = bool(os.getenv("CHUTES_API_BASE") or os.getenv("CHUTES_BASE"))
        dyn_env = os.getenv("SCILLM_AUTOSCALE_DYNAMIC")
        self.dynamic = (dyn_env in {None, "", "1", "true", "yes"}) if chutes_present else False
        # Bounds for adaptation (conservative by default)
        self.min_qps = float(os.getenv("SCILLM_MIN_QPS", "0.3"))
        self.max_qps = float(os.getenv("SCILLM_MAX_QPS", "1.5"))
        self.min_conc = int(os.getenv("SCILLM_MIN_CONCURRENCY", "1"))
        self.max_conc = int(os.getenv("SCILLM_MAX_CONCURRENCY", "2"))

        self._bucket = _TokenBucket(rate_per_sec=qps, burst=float(burst) if burst else None)
        self._sema = threading.Semaphore(max(1, conc))
        self._curr_conc = max(1, conc)
        # Simple EWMA for latency tracking and a success counter since last backoff
        self._ewma_latency_ms = None  # type: float | None
        self._success_since_backoff = 0
        self._last_backoff_ts = 0.0
        self._state_lock = threading.Lock()

    @classmethod
    def get(cls) -> "GlobalAutoScaler":
        with cls._lock:
            if cls._instance is None:
                cls._instance = GlobalAutoScaler()
            return cls._instance

    @contextmanager
    def acquire(self):
        if not self.enabled:
            # no-op
            yield
            return
        # Concurrency gate
        self._sema.acquire()
        try:
            # Pacing gate
            wait = self._bucket.take(1.0)
            if wait > 0:
                time.sleep(wait)
            yield
        finally:
            self._sema.release()

    # ---- Dynamic adaptation signals -------------------------------------
    def _set_qps(self, new_qps: float) -> None:
        new_qps = max(self.min_qps, min(self.max_qps, float(new_qps)))
        with self._lock:
            # Adjust underlying bucket rate; capacity remains the same
            self._bucket.rate = max(0.01, new_qps)

    def _set_concurrency(self, new_conc: int) -> None:
        new_conc = max(self.min_conc, min(self.max_conc, int(new_conc)))
        with self._state_lock:
            delta = new_conc - self._curr_conc
            if delta > 0:
                for _ in range(delta):
                    self._sema.release()
            elif delta < 0:
                # Acquire extra permits to reduce available capacity (non-blocking best-effort)
                for _ in range(-delta):
                    acquired = self._sema.acquire(blocking=False)
                    if not acquired:
                        break
            self._curr_conc = new_conc

    def note_success(self, latency_ms: float | None = None) -> None:
        if not (self.enabled and self.dynamic):
            return
        now = time.monotonic()
        with self._state_lock:
            # Update EWMA latency
            if latency_ms is not None:
                a = 0.2  # smoothing factor
                if self._ewma_latency_ms is None:
                    self._ewma_latency_ms = float(latency_ms)
                else:
                    self._ewma_latency_ms = (1 - a) * self._ewma_latency_ms + a * float(latency_ms)
            # If we haven't seen a recent backoff, gently increase throughput
            if (now - self._last_backoff_ts) > 10.0:
                self._success_since_backoff += 1
                if self._success_since_backoff >= 3:
                    # Prefer raising QPS up to max; then allow concurrency to climb
                    new_qps = min(self._bucket.rate * 1.1, self.max_qps)
                    self._set_qps(new_qps)
                    if abs(new_qps - self.max_qps) < 1e-6 and self._curr_conc < self.max_conc:
                        self._set_concurrency(self._curr_conc + 1)
                    self._success_since_backoff = 0

    def note_rate_limit(self, retry_after_s: float | None = None) -> None:
        if not (self.enabled and self.dynamic):
            return
        with self._state_lock:
            self._last_backoff_ts = time.monotonic()
            self._success_since_backoff = 0
            # Aggressive cooldown: halve QPS (floor to min) and drop concurrency to min
            new_qps = max(self.min_qps, self._bucket.rate * 0.5)
            if retry_after_s and retry_after_s > 0:
                # If server told us a window, clamp target QPS accordingly (rough heuristic)
                new_qps = min(new_qps, max(self.min_qps, 1.0 / float(retry_after_s)))
            self._set_qps(new_qps)
            self._set_concurrency(self.min_conc)


def controller() -> GlobalAutoScaler:
    return GlobalAutoScaler.get()
