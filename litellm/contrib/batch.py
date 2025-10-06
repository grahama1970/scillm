import os
import io
import json
import threading
import time
from contextlib import contextmanager
from typing import Optional, Dict, Set
import asyncio
from typing import Any, Awaitable, Callable, Iterable, List


class JsonlCheckpoint:
    """
    Minimal JSONL checkpoint helper for batch runners.

    - Each line is a JSON object containing at least {id_key: ...}.
    - processed_ids() returns the set of completed IDs.
    - append(row) appends one JSON line; safe under concurrent appends within a process.
    """

    def __init__(self, results_path: str, id_key: str = "id", summary_path: Optional[str] = None):
        self.results_path = results_path
        self.id_key = id_key
        self._lock = threading.Lock()
        self._summary_path = summary_path
        # Ensure parent dir exists
        os.makedirs(os.path.dirname(results_path) or ".", exist_ok=True)

    def processed_ids(self) -> Set[str]:
        ids: Set[str] = set()
        if not os.path.exists(self.results_path):
            return ids
        try:
            with open(self.results_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        val = obj.get(self.id_key)
                        if isinstance(val, (str, int)):
                            ids.add(str(val))
                    except Exception:
                        # Skip malformed lines
                        continue
        except Exception:
            # If the file is concurrently created/rotated, return best-effort
            return ids
        return ids

    def append(self, row: Dict, flush: bool = True) -> None:
        payload = json.dumps(row, ensure_ascii=False)
        # Use append with a lock to reduce interleaving within a process
        with self._lock:
            with open(self.results_path, "a", encoding="utf-8") as f:
                f.write(payload)
                f.write("\n")
                if flush:
                    f.flush()
                    os.fsync(f.fileno())

    @property
    def summary_path(self) -> str:
        if self._summary_path:
            return self._summary_path
        base = os.path.dirname(self.results_path)
        return os.path.join(base, "summary.json")


class TokenBucket:
    """
    Simple in-process token bucket for gentle throttling.

    - rate_per_sec: average refill rate (permits per second)
    - capacity: maximum burst size
    - max_sleep_s: cap per-acquire sleep
    - name: optional tag
    """

    def __init__(self, rate_per_sec: float, capacity: int, max_sleep_s: float = 2.0, name: Optional[str] = None):
        self.rate = float(rate_per_sec)
        self.capacity = int(max(1, capacity))
        self.tokens = float(self.capacity)
        self.max_sleep_s = float(max_sleep_s)
        self._lock = threading.Lock()
        self._last = time.monotonic()
        self._name = name or "default"
        self._acquired = 0
        self._slept_total = 0.0
        self._last_sleep = 0.0
        self._rejects = 0

    def _refill(self, now: float) -> None:
        elapsed = now - self._last
        if elapsed <= 0:
            return
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self._last = now

    @contextmanager
    def acquire(self):
        sleep_s = 0.0
        with self._lock:
            now = time.monotonic()
            self._refill(now)
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                self._acquired += 1
                self._last_sleep = 0.0
                yield
                return
            # Need to wait for more tokens
            need = 1.0 - self.tokens
            sleep_s = min(self.max_sleep_s, max(0.0, need / self.rate))
            if sleep_s <= 0.0:
                # Immediate retry without sleeping; treat as acquired
                self.tokens = max(0.0, self.tokens - 1.0)
                self._acquired += 1
                self._last_sleep = 0.0
                yield
                return
        # Sleep outside lock
        if sleep_s > 0:
            time.sleep(sleep_s)
            with self._lock:
                now2 = time.monotonic()
                self._refill(now2)
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    self._acquired += 1
                else:
                    # If still not enough tokens, consider this a soft reject
                    self._rejects += 1
                self._slept_total += sleep_s
                self._last_sleep = sleep_s
        try:
            yield
        finally:
            pass

    def stats(self) -> Dict[str, float]:
        return {
            "name": self._name,
            "acquired": float(self._acquired),
            "slept_s_total": float(self._slept_total),
            "rejects": float(self._rejects),
            "last_sleep_s": float(self._last_sleep),
        }


def token_bucket_from_env(default_rate: float = 3.0, default_capacity: int = 6, default_max_sleep_s: float = 2.0, name: Optional[str] = None) -> TokenBucket:
    rate = float(os.getenv("SCILLM_BUCKET_RATE", default_rate))
    cap = int(os.getenv("SCILLM_BUCKET_CAPACITY", default_capacity))
    maxs = float(os.getenv("SCILLM_BUCKET_MAX_SLEEP_S", default_max_sleep_s))
    return TokenBucket(rate_per_sec=rate, capacity=cap, max_sleep_s=maxs, name=name)


class AsyncTokenBucket:
    """Asyncio variant of TokenBucket for coroutine-based throttling."""

    def __init__(self, rate_per_sec: float, capacity: int, max_sleep_s: float = 2.0, name: Optional[str] = None):
        self.rate = float(rate_per_sec)
        self.capacity = int(max(1, capacity))
        self.tokens = float(self.capacity)
        self.max_sleep_s = float(max_sleep_s)
        self._lock = asyncio.Lock()
        self._last = time.monotonic()
        self._name = name or "default-async"
        self._acquired = 0
        self._slept_total = 0.0
        self._last_sleep = 0.0
        self._rejects = 0

    def _refill(self, now: float) -> None:
        elapsed = now - self._last
        if elapsed <= 0:
            return
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self._last = now

    async def acquire(self):
        class _Mgr:
            def __init__(self, outer: "AsyncTokenBucket"):
                self.outer = outer

            async def __aenter__(self):
                sleep_s = 0.0
                async with self.outer._lock:
                    now = time.monotonic()
                    self.outer._refill(now)
                    if self.outer.tokens >= 1.0:
                        self.outer.tokens -= 1.0
                        self.outer._acquired += 1
                        self.outer._last_sleep = 0.0
                        return
                    need = 1.0 - self.outer.tokens
                    sleep_s = min(self.outer.max_sleep_s, max(0.0, need / self.outer.rate))
                if sleep_s > 0:
                    await asyncio.sleep(sleep_s)
                    async with self.outer._lock:
                        now2 = time.monotonic()
                        self.outer._refill(now2)
                        if self.outer.tokens >= 1.0:
                            self.outer.tokens -= 1.0
                            self.outer._acquired += 1
                        else:
                            self.outer._rejects += 1
                        self.outer._slept_total += sleep_s
                        self.outer._last_sleep = sleep_s

            async def __aexit__(self, exc_type, exc, tb):
                return False

        return _Mgr(self)

    def stats(self) -> Dict[str, float]:
        return {
            "name": self._name,
            "acquired": float(self._acquired),
            "slept_s_total": float(self._slept_total),
            "rejects": float(self._rejects),
            "last_sleep_s": float(self._last_sleep),
        }


async def run_batch(
    items: Iterable[Dict[str, Any]],
    *,
    id_key: str,
    fn: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]],
    checkpoint_path: str,
    bucket: Optional[AsyncTokenBucket] = None,
    max_concurrency: Optional[int] = None,
) -> str:
    """
    Tiny batch runner: skip done via JsonlCheckpoint, throttle with AsyncTokenBucket, append results.

    Returns the results path used (checkpoint_path).
    """
    cp = JsonlCheckpoint(checkpoint_path, id_key=id_key)
    done = cp.processed_ids()
    sem = asyncio.Semaphore(max_concurrency) if max_concurrency else None

    async def _one(it: Dict[str, Any]):
        if it.get(id_key) is None:
            return
        if str(it[id_key]) in done:
            return
        if bucket is not None:
            async with (await bucket.acquire()):
                out = await fn(it)
        else:
            out = await fn(it)
        cp.append({id_key: it[id_key], "out": out})

    async def _guard(it: Dict[str, Any]):
        if sem:
            async with sem:
                await _one(it)
        else:
            await _one(it)

    # ensure predictable ordering
    tasks: List[Awaitable[None]] = []
    for it in items:
        tasks.append(_guard(it))
    if tasks:
        await asyncio.gather(*tasks)
    return checkpoint_path
