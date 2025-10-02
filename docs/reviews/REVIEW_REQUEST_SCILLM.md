Review branch: https://github.com/grahama1970/scillm/tree/feat/codeworld-provider

# Code Review Request — SciLLM (LiteLLM fork) with CodeWorld + Certainly (Lean4)

This review covers the integrated stack: providers, bridges, scenarios, readiness, and docs. Please propose unified diffs to improve correctness, safety, and developer ergonomics.

## Components & Intent
- Providers (Router):
  - `codeworld` (env‑gated)
  - `lean4` (env‑gated) with alias `certainly` (Lean4 only for alpha)
- Bridges:
  - CodeWorld FastAPI shim (`:8887`)
  - Lean4/Certainly FastAPI shim (`:8787`)
- Readiness: strict + optional gates; scenarios‑live workflow
- Scenarios: live demos for bridges + Router; deterministic tests for core logic

## Changes Since Upstream (key files)
- Providers/registration:
  - `litellm/__init__.py` — registers `codeworld`, `lean4`, and alias `certainly` when env‑gated
  - `litellm/llms/lean4.py` — provider surface for Lean4/Certainly; backend param placeholder; CERTAINLY_* env support
  - `litellm/llms/codeworld.py` (unchanged here; bridge alignment happens server‑side)
- Bridges:
  - `src/codeworld/bridge/server.py` — dynamic scoring/strategy runners (sandboxed), lex judge mode, Redis‑backed plateau signals; run_id/item_ids in manifest
  - `src/lean4_prover/bridge/server.py` — Lean4 batch bridge; echo session_id/track_id; run_id/item_id parity; CERTAINLY_REPO alias
- Safety:
  - `src/codeworld/engine/scoring_runner.py` — AST allowlist/denylist; RLIMITs; safe builtins; optional no‑net
  - `src/codeworld/engine/strategy_runner.py` — new; same hardening
- Scenarios:
  - CodeWorld: `scenarios/codeworld_bridge_release.py`, `codeworld_judge_live.py`
  - Lean4: `scenarios/lean4_bridge_release.py`, `lean4_router_release.py`
  - Certainly: `scenarios/certainly_bridge_release.py`, `certainly_router_release.py`
  - Runner: `scenarios/run_all.py`
- Readiness/CI:
  - `readiness.yml` — health checks (optional + strict variants); alias `certainly_health`; lean4_health_strict
  - `.github/workflows/scenarios-live.yml` — optional + strict health gates; always upload artifacts
- Docs:
  - `README.md` — “Certainly (Lean4 only, alpha)” section; logo usage
  - `QUICK_START.md` — compose bring‑up; session_id/track_id; judge demo
  - `feature_recipes/SIDE_BY_SIDE.md` — Router examples for codeworld/lean4/certainly

## Architecture
- Canonical bridge schema for both bridges: `{ messages, items, provider, options }`, success payload includes `{ summary, results, statistics, run_manifest }`.
- Providers post to `/bridge/complete`; Router usage mirrors between CodeWorld and Lean4/Certainly.
- Scenarios live under `scenarios/` (skip‑friendly); deterministic unit tests under `tests/` (no network).

## What Needs Attention (ask for diffs)
1) Provider ergonomics & aliasing
   - Is keeping both `additional_kwargs['lean4']` and `['certainly']` for one release sufficient? Propose a phased deprecation plan with code.
   - Validate `backend` param handling (currently pass‑through; single backend in alpha). Suggest a minimal adapter if needed.

2) Safety & isolation
   - CodeWorld runners use RLIMITs + `unshare -n`; propose containerized worker profiles (seccomp/AppArmor) and a minimal compose for worker in a follow‑up.
   - Recommend any additional AST/node bans for strategy code.

3) Readiness
   - Confirm lean4_health_strict and bridges_fullstack_health_strict logic; suggest stricter gating policy or adjusted env expectations.

4) Tests & determinism
   - Coverage gaps: provider alias path; Redis plateau signals; lex judge behavior beyond unit shape checks.

5) Docs & developer flow
   - Any friction in QUICK_START/run_all? Suggest edits.

## Repro
```bash
docker compose -f local/docker/compose.scillm.stack.yml up --build -d
python scenarios/run_all.py  # skip‑friendly

# Lean4/Certainly bridge
LEAN4_BRIDGE_BASE=http://127.0.0.1:8787 python scenarios/lean4_bridge_release.py
LITELLM_ENABLE_CERTAINLY=1 CERTAINLY_BRIDGE_BASE=http://127.0.0.1:8787 \
  python scenarios/certainly_router_release.py
```

## Please Reply With
- Unified diffs against the files listed above.
- If changing provider routing, include a tiny scenario demonstrating the new flow.
