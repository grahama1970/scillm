# CodeWorld MCTS Strategy (Experimental)

A single‑parameter adaptive variant selection policy: `strategy="mcts"` (optionally with `strategy_config={...}` or individual `rollouts/depth/uct_c/seed` overrides).

## Prereqs

```bash
docker compose -f local/docker/compose.scillm.stack.yml up --build -d
export CODEWORLD_BASE=http://127.0.0.1:8887
# Optional deterministic seed
export SCILLM_DETERMINISTIC_SEED=7
```

## Quick Start — Minimal Call (Sugar)

```python
from litellm import completion
import os

items = [{
  "task": "matrix-opt",
  "context": {
    "inputs": {"size": 256},
    "code_variants": {
      "algo_a": "def solve(ctx): return {'result': 1}",
      "algo_b": "def solve(ctx): return {'result': 1}",
      "algo_c": "def solve(ctx): return {'result': 1}"
    }
  }
}]

resp = completion(
  model="codeworld",
  custom_llm_provider="codeworld",
  messages=[{"role": "user", "content": "Select best variant adaptively"}],
  items=items,
  strategy="mcts",
  rollouts=48,
  depth=6,
  uct_c=1.25,
  options={"session_id":"mcts-session","track_id":"trial-1","max_seconds":10},
  api_base=os.getenv("CODEWORLD_BASE")
)

print(resp.choices[0].message["content"])  # type: ignore[index]
details = getattr(resp, "additional_kwargs", {}).get("codeworld")
print("MCTS block:", details["results"][0]["mcts"])
```

## Alias Call (No Explicit Param)

```python
from litellm import completion
import os

items = [{
  "task": "matrix-opt",
  "context": {
    "inputs": {"size": 256},
    "code_variants": {
      "algo_a": "def solve(ctx): return {'result': 1}",
      "algo_b": "def solve(ctx): return {'result': 1}",
      "algo_c": "def solve(ctx): return {'result': 1}"
    }
  }
}]

resp = completion(
  model="codeworld/mcts",  # alias injects strategy="mcts"
  custom_llm_provider="codeworld/mcts",
  messages=[{"role": "user", "content": "Alias test"}],
  items=items,
  options={"session_id":"alias-run","track_id":"trial-2"},
  api_base=os.getenv("CODEWORLD_BASE")
)
```

## Determinism

- Set `SCILLM_DETERMINISTIC_SEED` or pass `seed=` or `strategy_config={"seed": N}`.
- Seed is recorded in `results[i].mcts.seed` and `run_manifest.mcts_stats`.

## Output

`results[i].mcts`:
```jsonc
{
  "best_variant": "algo_b",
  "best_value": 0.74231,
  "rollouts": 48,
  "depth": 6,
  "uct_c": 1.25,
  "visits": {"algo_a":14,"algo_b":20,"algo_c":14},
  "explored": 3,
  "seed": 7,
  "error": null
}
```

`run_manifest.mcts_stats` duplicates the summary for quick indexing.

## Disable

```bash
export CODEWORLD_ENABLE_MCTS=0
```

## Notes

Phase‑1 uses deterministic pseudo reward (no extra variant execution). Future phases may integrate partial execution or early correctness metrics behind additional flags.
