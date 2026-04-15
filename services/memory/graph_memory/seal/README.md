# SEAL in Memory (hooks only)

- `export_judging.py` (CLI: `lessons-export-judging`): export positives/negatives for SEAL finetuning.
- Training/eval lives in devops/sparta; we call it via Make targets (see scripts/seal/README.md).
- Runtime remains unchanged unless you point `RERANKER_MODEL_BUNDLE` to a trained bundle.
