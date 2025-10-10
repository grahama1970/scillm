Forks / Paths
- Fork: https://github.com/grahama1970/scillm
  - Branch: feat/final-polish
  - Path: git@github.com:grahama1970/scillm.git#feat/final-polish
- Related forks (context only):
  - CodeWorld: https://github.com/grahama1970/codeworld
  - Certainly (Lean4): https://github.com/grahama1970/certainly

Title: Comprehensive Review — SciLLM (uv migration, Router enhancements, MCTS strategy), plus readiness/scenarios sanity

Goals
- Validate our migration from Poetry → uv/hatch (PEP 621) and ensure plain `uv sync` works.
- Review first‑class Router features (deterministic mode, bounded parallelism, schema‑first response with fallback, budget hooks, minimal image policy).
- Confirm CodeWorld MCTS strategy remains opt‑in and transparent; codex‑agent retry telemetry parity & tests.
- Request concrete unified diffs (git‑apply’able) for any fixes or improvements.

Live Scenarios (runnable, skip‑friendly)
- CodeWorld
  - scenarios/codeworld_bridge_release.py — bridge call & manifest
  - scenarios/codeworld_judge_live.py — judge metrics
  - scenarios/mcts_codeworld_demo.py — MCTS strategy (opt‑in)
  - scenarios/codeworld_baseline_vs_mcts.py — baseline vs MCTS
- Certainly (Lean4)
  - scenarios/lean4_bridge_release.py
  - scenarios/lean4_router_release.py
  - scenarios/certainly_router_release.py
- Agents / Router
  - scenarios/mini_agent_http_release.py — local deterministic tool loop
  - scenarios/codex_agent_router.py — Router→codex‑agent sidecar
  - scripts/compare_grounded_qa.py — Grounded QA A/B + judge (live scenario)
- Warm‑ups / Readiness
  - scenarios/provider_warmup_probe.py — chutes/runpod probes
  - readiness.yml — chutes_warmup_strict, runpod_warmup_strict, warmups_strict_all, grounded_compare_smoke, grounded_compare_strict

Recent Changes To Review (relative paths)
- Build/Packaging (uv/hatch)
  - pyproject.toml — PEP‑621 [project], hatchling build, extras → optional‑dependencies; updated authors/Upstream; description notes fork
  - uv.lock — committed for deterministic sync
  - Makefile — switched to `uv sync`/`uv run`
- Router features (downstream extractor integration)
  - litellm/router.py — deterministic mode; schema‑first + single fallback; image policy (minimal); set_budget(); parallel_acompletions max_concurrency; result meta
  - litellm/router_utils/parallel_acompletion.py — timing_ms on ParallelResult
  - tests/local_testing/test_router_extractor_features.py — unit tests
- CodeWorld & codex‑agent polish
  - litellm/llms/codeworld.py — one‑time warnings (uct_c alias conflict, seed mismatch)
  - litellm/llms/codex_agent.py — async retry telemetry parity (statuses + first_failure_status); duck‑typed client support
  - tests/local_testing/test_codex_agent_retry_async_statuses.py — async test
- Readiness & scenarios
  - readiness.yml — grounded_compare_* gates, strict warm‑ups composite
  - scripts/compare_grounded_qa.py — live A/B + judge scenario

What to Verify / Clarifying Questions
1) uv migration
   - Is pyproject.toml minimal and correct for PEP 621 + hatchling? Any extras/env markers to tighten to avoid resolver pain across 3.8–3.12? Propose diffs if so.
   - Is committing uv.lock appropriate here (we want consistent local installs)? If not, propose policy + .gitignore update.
2) Router additions
   - Deterministic mode: are we forcing only what’s necessary (temp=0, top_p=1), and is the one‑time warning useful? Propose alternative if noisy.
   - Schema‑first: is one fallback to json_object the right minimum? Suggest improved validation or more constrained response_format if you see a low‑risk win.
   - Budget hooks: is the soft/hard behavior and meta flag sufficient? Suggest refinements.
   - Image policy: minimal reject path only for data:image/* — would you add a tiny downscale or leave as reject only? If yes, include a safe, small utility.
3) CodeWorld MCTS & seed policy
   - Placement and manifest signals OK? Any missing guardrails or better parameter names (e.g., exploration_constant alias) you’d like to enforce?
4) codex‑agent telemetry
   - Are statuses/first_failure_status + retry_sequence sufficient? Suggest any additional fields/tests.
5) Readiness
   - Grounded compare gates (smoke/strict) — are they appropriately skip‑friendly unless STRICT_COMPARE=1? Any change to env names recommended?

Acceptance & Output Requested
- Please respond with:
  1) Direct answers to the clarifying questions.
  2) Prioritized recommendations (High→Medium→Low), with rationale.
  3) Unified diffs (git‑apply’able) against the files above.
     - Keep changes minimal and backward‑compatible.
     - If you add tests, place them under tests/local_testing and keep them deterministic/offline.

Quick Repro
```
uv sync
uv run pytest -q tests/local_testing/test_router_extractor_features.py
uv run pytest -q tests/local_testing/test_codex_agent_retry_async_statuses.py
```

