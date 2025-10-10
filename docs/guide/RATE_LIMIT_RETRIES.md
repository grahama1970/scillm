# Rate Limiting & Retries (Router)

SciLLM provides opt‑in resilience for provider 429 responses.

Features
- Honors `Retry-After` header (seconds or HTTP-date).
- Exponential full‑jitter fallback when header is absent.
- Time/attempt budgets to cap wall‑clock delay.
- Callbacks (on_attempt/on_success/on_giveup) for checkpoint/resume.
- Optional JSON logs when `SCILLM_LOG_JSON=1`; throttle via `SCILLM_RETRY_LOG_EVERY`.
- Edge guards: past HTTP-date clamped to 0; `Retry-After: 0` floored to 0.5s; capped by remaining budget.

Enable (env)
```
SCILLM_RETRY_ENABLED=1
SCILLM_RETRY_MAX_ATTEMPTS=8
SCILLM_RETRY_TIME_BUDGET_S=600
SCILLM_RETRY_BASE_S=5
SCILLM_RETRY_MAX_S=120
SCILLM_RETRY_JITTER_PCT=0.25
SCILLM_RETRY_LOG_EVERY=2
SCILLM_LOG_JSON=1
```

Per‑call example
```python
resp = router.completion(
  model="openai/gpt-4o",
  messages=[{"role":"user","content":"Hi"}],
  retry_enabled=True,
  retry_max_attempts=6,
  retry_time_budget_s=300,
  retry_base_s=4,
  retry_max_s=90,
  retry_jitter_pct=0.3,
  honor_retry_after=True,
)
```

Callback meta (on_attempt)
- attempt, next_sleep_s, retry_after, elapsed_s
- remaining_time_s, remaining_attempts, cumulative_sleep_s
- attempt_start_monotonic

Giveup meta
- attempts, elapsed_s, last_error, cumulative_sleep_s

Streaming
- Retries restart the stream; partial tokens aren’t preserved by design.
