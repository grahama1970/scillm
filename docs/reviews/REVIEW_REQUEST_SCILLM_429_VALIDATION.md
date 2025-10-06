Forks / Paths
- Fork: https://github.com/grahama1970/scillm
  - Branch: feat/final-polish
  - Path: git@github.com:grahama1970/scillm.git#feat/final-polish
- Related forks (context only):
  - CodeWorld: https://github.com/grahama1970/codeworld
  - Certainly (Lean4): https://github.com/grahama1970/certainly

Title: Validation Request — 429 handling (Retry‑After, jitter backoff, budgets, callbacks) + uv migration + Router features

Context & Scope
- Added robust 429 handling in Router (async path) with:
  - Retry‑After (seconds or HTTP-date) awareness
  - Exponential backoff with full jitter when header absent
  - Per‑call retry budgets (attempt/time)
  - Callbacks (on_attempt/on_success/on_giveup) for checkpoint/resume
  - Optional JSON logs (SCILLM_LOG_JSON=1)
- Migrated to uv/hatch (PEP 621) with plain `uv sync` success; Makefile updated.
- Router also includes deterministic mode, schema‑first response with single fallback, minimal image policy, simple budget hooks.
- CodeWorld MCTS is opt‑in; codex‑agent has async retry telemetry parity + tests.

Live Scenarios (skip‑friendly)
- CodeWorld: scenarios/codeworld_bridge_release.py, codeworld_judge_live.py, mcts_codeworld_demo.py, codeworld_baseline_vs_mcts.py
- Certainly (Lean4): scenarios/lean4_bridge_release.py, lean4_router_release.py, certainly_router_release.py
- Router/Agents: scenarios/mini_agent_http_release.py, scenarios/codex_agent_router.py, scripts/compare_grounded_qa.py
- Readiness: readiness.yml (warmups + grounded compare smoke/strict)

Key Files To Review
- 429 handling & tests
  - litellm/router.py — async_function_with_retries retry layer (Retry‑After, backoff/jitter, budgets, callbacks, logs)
  - tests/local_testing/test_router_retry_429_budget.py — succeed‑after‑retry and giveup‑on‑budget
- Router features
  - litellm/router.py — deterministic mode, schema‑first fallback, image policy, set_budget
  - litellm/router_utils/parallel_acompletion.py — timing_ms on ParallelResult
  - tests/local_testing/test_router_extractor_features.py — deterministic, schema, concurrency cap, budgets
- Build/Packaging
  - pyproject.toml — PEP 621 + hatchling; extras; author/upstream; fork description
  - uv.lock — committed for deterministic sync
  - Makefile — uv sync/run
- CodeWorld/codex‑agent polish
  - litellm/llms/codeworld.py — alias/seed one‑time warnings
  - litellm/llms/codex_agent.py — async statuses + first_failure_status telemetry
  - tests/local_testing/test_codex_agent_retry_async_statuses.py

Direct Answers (from DevOps agent)
1) 429 logic: Yes — Retry‑After first, then exponential jitter; add guards
   - Clamp negative HTTP‑date deltas to 0 and treat Retry‑After: 0 as a floor (e.g., 0.5s)
   - Cap Retry‑After to remaining time budget minus a small epsilon
2) Budgets + callbacks: Add attempt_start_monotonic, remaining_time_s, remaining_attempts, cumulative_sleep_s; throttle JSON retry logs via every_n_attempts
3) Deterministic mode: Force frequency_penalty=0 and presence_penalty=0; warn once on conflicts
4) Schema‑first: Validate root object; set schema_validation and response_format_used meta; no extra model round yet
5) uv: Add readme, classifiers for 3.9–3.12; optional extras markers

Requests For Validation & Diffs
- Please confirm our implementation aligns with the above answers and propose unified diffs (git‑apply’able) to:
  1) Add/adjust the 429 edge‑case guards and callback fields (if any gaps remain)
  2) Normalize deterministic penalties and silent‑mode env flag if you prefer
  3) Add schema‑validation meta exactly as suggested (or better)
  4) Add packaging hygiene (classifiers/readme) and any missing markers
  5) Docs: a short RATE_LIMIT_RETRIES.md and a CLI toggle in scripts/compare_grounded_qa.py

Acceptance
- Keep changes minimal and backward‑compatible; tests deterministic/offline under tests/local_testing
- Return direct answers + prioritized recommendations + unified diffs

Quick Repro
```
uv sync
uv run pytest -q tests/local_testing/test_router_retry_429_budget.py
uv run pytest -q tests/local_testing/test_router_extractor_features.py
uv run pytest -q tests/local_testing/test_codex_agent_retry_async_statuses.py
```

