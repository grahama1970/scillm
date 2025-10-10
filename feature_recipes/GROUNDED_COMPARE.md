# Grounded QA Model Comparison (Experimental)

Compare two chat models on grounded Q&A items with an internal judge, writing per‑item JSONL and a summary.

## Why
- Many teams need A/B with citations, grounding, and an internal judge.
- Aligns with SciLLM’s “scientific” goals: reproducible inputs/outputs, metrics, and artifacts.

## CLI

```bash
python scripts/compare_grounded_qa.py \
  --items data/items.jsonl --n 50 \
  --model-a "openai/gpt-4o-mini" \
  --model-b "openai/gpt-4o" \
  --judge-model "openai/gpt-4o-mini" \
  --out local/artifacts/compare
```

- `--items`: JSONL with objects `{id, question, evidence?[]}`; evidence is optional (pass-through for now).
- `--n`: cap number of items.
- `--model-a`, `--model-b`: chat models to compare (OpenAI-compatible strings OK).
- `--judge-model`: judge model; returns `{supported_a, supported_b, better, confidence, rationale_short}`.
- `--out`: output directory; script writes `results.jsonl` + `summary.json`.

### Using codex‑agent provider

To route through the codex‑agent provider (benefits: unified retries/logging; optional metrics), set `CODEX_AGENT_API_BASE` and optionally `CODEX_AGENT_API_KEY`, and pass flags:

```bash
export CODEX_AGENT_API_BASE=http://127.0.0.1:8788  # or your sidecar URL
python scripts/compare_grounded_qa.py \
  --items data/items.jsonl --n 25 \
  --model-a "gpt-4o-mini" --use-codex-a \
  --model-b "gpt-4o" --use-codex-b \
  --judge-model "gpt-4o-mini" --use-codex-judge \
  --out local/artifacts/compare
```

## JSON Shapes

- Model outputs (requested JSON):
  ```json
  {
    "answer": "...",
    "evidence_refs": ["e1"],
    "grounded": true,
    "grounded_score": 0.9,
    "rationale_short": "..."
  }
  ```
- Judge output (requested JSON):
  ```json
  {
    "supported_a": true,
    "supported_b": false,
    "better": "A",
    "confidence": 0.8,
    "rationale_short": "..."
  }
  ```

## Output Summary
- `grounded_pass_rate` per model (self‑reported grounded field).
- `judge_supported_rate` per model (judge decision).
- Latency p50/p95 per model.
- A few sample diffs.
- Path to `results.jsonl` for post‑hoc analysis.

## Extensibility (next steps)
- Pluggable retrieval + judge (callables) with default OpenAI‑compatible surface.
- Writers (CSV/JSONL) and cost/usage counters.
- Items schema can include metadata fields; script preserves unknown keys.

## Determinism
See [Determinism Policy](../docs/policies/DETERMINISM.md). This is a live scenario by design; keep tests/ deterministic.
