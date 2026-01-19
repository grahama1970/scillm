# Task List: Align scillm skill with paved-path contract

## Context
We identified three gaps between the pi-mono `.pi/skills/scillm` skill and `docs/scillm/SCILLM_PAVED_PATH_CONTRACT.md`:
1. No CLI entry-point for paved-path preflight/list-models helpers.
2. `vlm.py batch` does not guard against missing `CHUTES_API_BASE`, unlike single-image describe command.
3. Batch/VLM flows expose only `--json`; they lack switches for strict JSON validation/repair expected by the contract.

Goal: ship these improvements without regressing existing behavior.

## Tasks

- [x] **Task 1**: Add orchestrated `preflight` command that shells into `scillm.paved.sanity_preflight` (plus `list-models` helper) via `run.sh`
  - Agent: general-purpose
  - Parallel: 0
  - Notes: extend `.pi/skills/scillm/run.sh` to support a `preflight` verb that calls a new `preflight.py` (or inline) which invokes `sanity_preflight`/`list_models_openai_like`, prints structured results, and exits non-zero on failure.

- [x] **Task 2**: Enforce `CHUTES_API_BASE` validation in `vlm.py batch`
  - Agent: general-purpose
  - Parallel: 0
  - Notes: mirror the existing credential guard from `describe` (lines 72-80) so batch mode errors early when `CHUTES_API_BASE` (and optionally `CHUTES_VLM_MODEL`) are missing.

- [x] **Task 3**: Add strict JSON options to batch + VLM CLIs
  - Agent: general-purpose
  - Parallel: 0
  - Notes: expose flags/env support for `SCILLM_JSON_STRICT`, `retry-invalid-json`, and `schema` placeholders (even stub). Default to enabling `SCILLM_JSON_STRICT` when `--json` flag is set; optionally allow `--repair-invalid-json`. Update docs accordingly.

## Completion Criteria
- New preflight/list-models commands work via `run.sh` using `uv run` and return non-zero on failure.
- `vlm.py batch` fails fast with a clear error if `CHUTES_API_BASE` or `CHUTES_API_KEY` missing.
- Batch + VLM commands expose strict JSON flags, setting `SCILLM_JSON_STRICT=1` (or equivalent) whenever `--json` is set, with optional retry/repair knobs documented in help text.
- `sanity.sh` passes.

## Questions/Blockers
None.
