# Code Review Request — Certainly (Multi‑Prover) Integration

This document summarizes recent changes enabling a generic "Certainly" provider/alias and aligning Lean4 under it. Please review and propose unified diffs to improve correctness, safety, and ergonomics.

## Scope and Goals
- Treat "Certainly" as the umbrella multi‑prover surface; Lean4 is the first backend.
- Keep public API symmetry with CodeWorld and preserve back‑compat (lean4 provider still works).
- Improve readiness, scenarios, and docs for a seamless local bring‑up.

## Key Changes (relative paths)

Providers / Registration
- `litellm/__init__.py`: Register alias `custom_llm_provider="certainly"` alongside `"lean4"` when env‑gated.
- `litellm/llms/lean4.py`:
  - Resolve `CERTAINLY_BRIDGE_BASE` and attach payloads under both `additional_kwargs['lean4']` and `['certainly']`.
  - Accept `backend` optional param (defaults to "lean4").

Lean4/Certainly Bridge
- `src/lean4_prover/bridge/server.py`:
  - Accept `CERTAINLY_REPO` env alias.
  - Options now echo `session_id` and `track_id` into `run_manifest.options`.

Readiness
- `readiness.yml`: Add `certainly_health` (alias) and strict bridges gate (already present from prior patch).

Scenarios
- `scenarios/certainly_bridge_release.py`: Generic bridge demo, `CERTAINLY_BACKEND` (default lean4).
- `scenarios/certainly_router_release.py`: Router alias demo with `backend` param.
- `scenarios/run_all.py`: Includes both certainly scenarios.

Docs
- `feature_recipes/SIDE_BY_SIDE.md`: Adds "Certainly (Router; multi‑prover)" example.
- `QUICK_START.md` and `SCILLM_DEPLOY.md`: Bring‑up and alias notes.

CodeWorld Parity (earlier patch context)
- Dynamic scoring runner hardening, optional Redis plateau signals, lex judge toggle.
- Centralized Docker stack with nonet env for scoring/strategy.

## Questions for the Reviewer
1) Provider/alias design:
   - Is aliasing `certainly` → Lean4 handler acceptable for alpha, or should we introduce a tiny adapter (e.g., `llms/certainly.py`) that resolves `backend` explicitly before delegating?
   - Should the provider attach only `additional_kwargs['certainly']` going forward (and keep `['lean4']` for compat one release), or keep both indefinitely?

2) Backend selection contract:
   - Proposed: `provider.args.backend` (string, required when multiple backends exist), with `CERTAINLY_BACKEND` as fallback. Any objection to finalizing this spelling now?
   - For future Coq, are there additional fields we should standardize in `items` to avoid backend‑specific branching?

3) Tool execution & Ollama (stronger agent input requested):
   - Recommend best practices for default code model and text model when the user hasn’t set them (env fallbacks). Today we rely on repo defaults and Ollama presets.
   - Preferred policy for tool timeouts and retries during proof orchestration (e.g., Lean4 suggest/repair loops): global ceiling vs per‑tool budget?
   - Guidance for choosing coder models that balance determinism and usability for local developer loops.

4) Safety & isolation:
   - CodeWorld runners use resource limits + optional `unshare -n`. Would you push us to a containerized worker now (seccomp/AppArmor profiles) for both scoring and strategy, or keep this for beta?
   - Any extra AST restrictions you recommend for strategy code beyond current allowlist/denylist?

5) Readiness gates:
   - We added `bridges_fullstack_health_strict` for deploy gates and kept the optional check for non‑strict runs. OK to promote the strict one when `READINESS_EXPECT` includes both `codeworld,lean4`?

6) Parity with CodeWorld manifests:
   - Lean4 bridge now echoes `session_id/track_id` in manifest options. Do we need additional run ids or per‑item IDs for deterministic replays similar to CodeWorld’s `run_manifest.tools`?

## Please Reply With
- Unified diffs against the files listed above.
- If proposing non‑trivial provider routing changes, include both the provider module and a minimal scenario demonstrating the behavior.

## How to Reproduce Locally
- Bring up the stack: `docker compose -f local/docker/compose.scillm.stack.yml up --build -d`
- Run scenarios: `python scenarios/run_all.py` (skip‑friendly)
- Test certainly alias:
  - `CERTAINLY_BRIDGE_BASE=http://127.0.0.1:8787 python scenarios/certainly_bridge_release.py`
  - `LITELLM_ENABLE_CERTAINLY=1 CERTAINLY_BRIDGE_BASE=http://127.0.0.1:8787 python scenarios/certainly_router_release.py`

Thank you! Please call out anything that would reduce friction for users switching between backends under the Certainly surface.

