SciLLM Sanity Checks

This folder holds small, deterministic sanity scripts that return a single
JSON summary and exit 0 on success (non‑zero on failure). Use these locally
and in CI to verify Chutes text/VLM models and batch behavior.

Scripts
- chutes_batch_sanity.py — 5‑call batch (text + VLM) using scillm.parallel_acompletions.
  Prints {"ok": true|false, "items":[…]} and exits 0/1 accordingly.
  Cases covered: strict JSON echo, France/Paris, HTTPS VLM, local file‑path VLM, inline HTML classification via `assets/inline_classification.html` (token `luminous-harvest`).

Required env
- CHUTES_API_BASE, CHUTES_API_KEY
- CHUTES_TEXT_MODEL (recommended) or CHUTES_MODEL_ID
- CHUTES_VLM_MODEL (for the two image prompts)

Run
  PYTHONPATH=/path/to/litellm \
  python scripts/sanity/chutes_batch_sanity.py
