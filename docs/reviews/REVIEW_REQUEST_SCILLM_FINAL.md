Forks:
- SciLLM (this repo): https://github.com/grahama1970/scillm
- CodeWorld (bridge/engine): https://github.com/grahama1970/codeworld
- Certainly (Lean4 bridge): https://github.com/grahama1970/certainly

Branch: feat/final-polish
Path: git@github.com:grahama1970/scillm.git#feat/final-polish

# Request: Comprehensive Code Review and State of Project — SciLLM + CodeWorld + Certainly

Please review the SciLLM fork (LiteLLM-based) with its integrated components: CodeWorld (code orchestration + optional MCTS) and Certainly (Lean4). Reply with direct answers to clarifying questions, prioritized recommendations, and unified diffs (git-apply’able). Keep changes minimal and aligned with the “one happy path per surface” principle.

## Project Context (Concise)
- Goal: a lean, reproducible experimental layer over LiteLLM with optional modules and runnable scenarios. Deterministic tests remain offline; live behavior is exercised via `scenarios/` and guarded by readiness gates.
- Providers (env‑gated): `codeworld`, `certainly` (alias for Lean4), `mini-agent`, `codex-agent`.
- Readiness: deterministic baseline + strict live gates (`READINESS_EXPECT`, `STRICT_WARMUPS`).
- Warm‑ups: daily TTL and strict gating for Chutes/Runpod to avoid first‑token latency spikes.
- Determinism: single seed precedence (`SCILLM_DETERMINISTIC_SEED` → per‑call seed); scenarios show how to set/record.

## Live Scenarios (what to try)
- CodeWorld
  - `scenarios/codeworld_bridge_release.py` — bridge call, manifest parity.
  - `scenarios/codeworld_judge_live.py` — judge metrics + speed effect.
  - `scenarios/mcts_codeworld_demo.py` — adaptive variant selection (MCTS strategy).
  - `scenarios/codeworld_baseline_vs_mcts.py` — side‑by‑side comparison.
- Certainly (Lean4)
  - `scenarios/lean4_bridge_release.py` — bridge call.
  - `scenarios/lean4_router_release.py` — Router provider path.
  - `scenarios/certainly_router_release.py` — alias path.
- Agents
  - `scenarios/mini_agent_http_release.py` — local deterministic tool loop.
  - `scenarios/codex_agent_router.py` — Router → codex-agent sidecar.
- Warm‑ups
  - `scenarios/provider_warmup_probe.py --provider chutes|runpod` — quick health/latency.

## Key Files to Review (relative paths)
- Providers/registration
  - `litellm/__init__.py` — env‑gated registration; `codeworld/mcts` alias.
  - `litellm/llms/codeworld.py` — provider + sugar folding (MCTS knobs, CI auto‑scaling).
  - `litellm/llms/codex_agent.py` — bounded retries + metrics (`retry_stats`).
  - `litellm/llms/lean4.py` and adapter `certainly` (if available in this branch).
- Readiness & Warm‑ups
  - `readiness.yml` — strict warm‑ups: `chutes_warmup_strict`, `runpod_warmup_strict`, `warmups_strict_all`.
  - `.github/workflows/mvp-check.yml` — live gate example; and README warm‑ups snippet.
- Scenarios & Scripts
  - `scenarios/*.py` — especially `mcts_codeworld_demo.py`, `codeworld_baseline_vs_mcts.py`, `provider_warmup_probe.py`.
  - `scripts/provider_warmup.py`, `scripts/chutes_warmup.py`, `scripts/warmup_strict_gate.py`.
- Docs
  - `README.md` — Warm‑ups in CI + getting started.
  - `local/docs/01_guides/HAPPYPATH_GUIDE.md` — quick start and one‑liners.
  - `feature_recipes/MCTS_CODEWORLD.md`, `feature_recipes/CODEX_AGENT.md`, `feature_recipes/MINI_AGENT.md`.

## Clarifying Questions (please answer directly)
1) MCTS placement: do you agree it belongs in the CodeWorld engine layer (strategy policy) rather than mini‑agent or codex‑agent? If not, propose a counter‑design with pros/cons.
2) Determinism: is the current seed precedence clear and sufficient? Should we add a single runtime warning when both `seed` and `SCILLM_DETERMINISTIC_SEED` are set but differ?
3) codex‑agent resilience: are the retry/backoff defaults and `retry_stats` shape adequate? Would you add per‑attempt status codes everywhere (sync+async) and a test? If so, include diffs.
4) Warm‑ups: is the `STRICT_WARMUPS` composite gating idiomatic for CI? Suggest improvements or a simpler pattern if any.
5) Docs: does the “one happy path per surface” come through, or would you consolidate/rename any scenario/recipe for clarity?

## Requests for Changes (unified diffs welcome)
- Provider ergonomics
  - Improve sugar folding in `litellm/llms/codeworld.py` if you see edge cases (e.g., conflict detection when both `exploration_constant` and `uct_c` differ).
- Observability
  - Extend `retry_stats` with anything minimal but high‑value (we already include `attempts, failures, total_sleep_ms, final_status, first_failure_status, retry_sequence, statuses`).
- Readiness
  - If you recommend different strict/live policy defaults, include diffs to `readiness.yml`.
- Docs
  - Tighten wording or reorder sections to minimize “option anxiety”; keep one‑liner commands front and center.

## State of the Project (today)
- Strengths: uniform call shapes; determinism boundary; lean readiness and scenarios; retry/backoff sanity; optional MCTS with transparent manifest.
- Risks/Improvements: warn once when deprecating `mcts_stats`; guard conflicting aliases; add a feature matrix table (README updated) and GH Actions snippet (added).

## Where to Put/Improve MCTS (explicit ask)
- We plan to keep MCTS as an opt‑in CodeWorld strategy with a root‑bandit UCT for variant selection, deterministic seeding, and clear telemetry. If you disagree, propose the alternative (mini‑agent or codex‑agent) with specific diffs and a migration path.

Thank you — please include answers plus diffs we can apply directly.

