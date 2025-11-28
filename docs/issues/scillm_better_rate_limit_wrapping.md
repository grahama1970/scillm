# SciLLM should wrap LiteLLM 429s cleanly

**Problem**
- When CHUTES returns 429 “Infrastructure is at maximum capacity,” LiteLLM raises `RateLimitError`.
- SciLLM lets this propagate, and the coroutine gets cancelled, producing warnings like:
  - `RuntimeWarning: coroutine 'OpenAIChatCompletion.acompletion' was never awaited`
- The stack shows LiteLLM classes (`litellm.RateLimitError`) instead of a clean SciLLM exception, which confuses operators and violates the paved-path ergonomics.

**Why it matters**
- Operators expect SciLLM to present a consistent, awaited exception surface (or retry) without noisy coroutine warnings.
- The current behavior looks like a coding bug (un-awaited coroutine) even though it’s just a capacity hit.
- It also obscures that the failure came from CHUTES capacity, not from caller misuse.

**Requested fix**
- In SciLLM, intercept LiteLLM `RateLimitError` (and other recoverable provider errors), ensure the coroutine is awaited/consumed, and raise a SciLLM-specific exception (or perform built‑in backoff when `tenacious` is enabled).
- Ensure no `coroutine was never awaited` warnings are emitted.
- Include provider/model and a concise reason (e.g., `capacity_exhausted`) in the exception payload for logging.

**Workarounds today**
- Run with `SCILLM_TENACIOUS=1` (and low `concurrency`) so internal retries hide the 429.
- Catch `litellm.RateLimitError` in callers and retry/sleep manually.

**Repro**
- Call `scillm.acompletion` against CHUTES when infra is saturated; observe `RateLimitError: Infrastructure is at maximum capacity` and the RuntimeWarning about an un-awaited coroutine.
