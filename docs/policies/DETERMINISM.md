# Determinism & Reproducibility (SciLLM)

This document defines the single, cross‑provider seed and precedence order used by SciLLM.

## Single Cross‑Provider Seed

- `SCILLM_DETERMINISTIC_SEED` is the canonical cross‑provider knob.
- Providers MAY also implement optional overrides (e.g., `CODEX_AGENT_DETERMINISTIC_SEED`).

## Precedence (highest → lowest)

1. Per‑request parameter (e.g., `seed` or `strategy_config.seed`)
2. Provider‑specific override (e.g., `CODEX_AGENT_DETERMINISTIC_SEED`)
3. Global `SCILLM_DETERMINISTIC_SEED`
4. Unset → nondeterministic

Keep `tests/` deterministic and offline; live behavior belongs in `scenarios/` and readiness flows.

