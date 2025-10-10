# Batch Helpers (contrib)

This module provides two small, optional helpers for long‑running evals:

- JSONL checkpoint/resume to avoid re‑processing completed items after restarts.
- In‑process token bucket throttler to gently pace concurrent callers.

## Install & Import

```python
from scillm.contrib.batch import JsonlCheckpoint, TokenBucket, token_bucket_from_env
# or
from litellm.contrib.batch import JsonlCheckpoint, TokenBucket, token_bucket_from_env
```

## JsonlCheckpoint

```python
cp = JsonlCheckpoint("local/artifacts/compare/run_1730000000/results.jsonl", id_key="id")
done = cp.processed_ids()  # set of str IDs

for item in items:
    if item["id"] in done:
        continue
    # ... run your request ...
    cp.append({"id": item["id"], "out": out_json, "meta": {"model": model, "lat_ms": lat}})

# Optional summary location
print(cp.summary_path)
```

- Robust to malformed lines (skips them).
- Uses append + flush/fsync for durability.

## TokenBucket (process‑local)

```python
bucket = TokenBucket(rate_per_sec=3.0, capacity=6)
for item in items:
    with bucket.acquire():
        # call router.completion(...) or litellm.completion(...)
        pass
print(bucket.stats())  # {acquired, slept_s_total, rejects, last_sleep_s}
```

- Thread‑safe; uses monotonic time.
- rate_per_sec averaged with burst capacity.
- max_sleep_s caps per‑acquire sleep.

### From environment

```python
bucket = token_bucket_from_env(default_rate=3.0, default_capacity=6)
```

Respects:
- `SCILLM_BUCKET_RATE`, `SCILLM_BUCKET_CAPACITY`, `SCILLM_BUCKET_MAX_SLEEP_S`.

## Interop with Router retries

Use TokenBucket to reduce bursts across concurrent callers. Router’s 429 retrier still handles per‑request resilience (Retry‑After, backoff, budgets), so together they lower 429s and keep batches progressing.

## Limits

- TokenBucket is process‑local; for cross‑process coordination, use an external queue or rate‑limiter.
- JsonlCheckpoint is append‑oriented; if you need atomic multi‑file transactions, use a DB/store.
