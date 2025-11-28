SciLLM Sanity Checks

This folder holds small, deterministic sanity scripts that return a single
JSON summary and exit 0 on success (non‑zero on failure). Use these locally
and in CI to verify Chutes text/VLM models and batch behavior.

Scripts
- chutes_batch_sanity.py — 5-call batch (text + VLM) using scillm.parallel_acompletions.
  Prints {"ok": true|false, "items":[…]} and exits 0/1 accordingly.
  Cases covered: strict JSON echo, France/Paris, HTTPS VLM, local file-path VLM, inline HTML classification via `assets/inline_classification.html` (token `luminous-harvest`).
- chutes_experimental_json_sanity.py — 5-call JSON-only probe that targets the experimental model set via `CHUTES_EXPERIMENTAL` (for example `moonshotai/Kimi-K2-Thinking`).
  Validates strict JSON parsing/shape for scenarios ranging from basic echoes to decision matrices via `parallel_acompletions_iter`. Prints a single `RESULT PASS/FAIL … reasons=` line by default; add `--details` for per-scenario rows and/or `--json-summary` for the machine-readable payload. Use `--model <model_id>` to override the env var ad hoc.
- chutes_experimental_json_sanity_curl.py — sequential JSON probe that mirrors the scenarios above but shells out to `curl` for every request instead of using SciLLM. Helpful when debugging raw HTTP requests, reproducing issues outside Python, or running on machines that only provide curl. Add `--print-curl` to capture the exact commands or `--verbose-json` to dump the raw response bodies for each scenario.

Required env
- CHUTES_API_BASE, CHUTES_API_KEY
- CHUTES_TEXT_MODEL (recommended) or CHUTES_MODEL_ID
- CHUTES_VLM_MODEL (for the two image prompts)
- CHUTES_EXPERIMENTAL (for chutes_experimental_json_sanity unless `--model` is set)

Run
  PYTHONPATH=/path/to/litellm \
  python scripts/sanity/chutes_batch_sanity.py
