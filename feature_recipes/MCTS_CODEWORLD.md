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
  uct_c=1.25,  # exploration_constant (UCT C)
  options={"session_id":"mcts-session","track_id":"trial-1","max_seconds":10},
  api_base=os.getenv("CODEWORLD_BASE")
)

print(resp.choices[0].message["content"])  # type: ignore[index]
details = getattr(resp, "additional_kwargs", {}).get("codeworld")
print("MCTS block:", details["results"][0]["mcts"])
```

## Alias Call (No Explicit Param)

Note
- Canonical alias is `codeworld/mcts:auto`. The synonym `codeworld/mcts+auto` is accepted for convenience but normalized to the canonical form in responses/manifests.

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
  custom_llm_provider="codeworld",
  messages=[{"role": "user", "content": "Alias test"}],
  items=items,
  options={"session_id":"alias-run","track_id":"trial-2"},
  api_base=os.getenv("CODEWORLD_BASE")
)
```

## Determinism

- Set `SCILLM_DETERMINISTIC_SEED` or pass `seed=` or `strategy_config={"seed": N}`.
- Seed is recorded in `results[i].mcts.seed` and mirrored to `run_manifest.strategy_seed`.
 - See determinism policy and seed precedence: `docs/policies/DETERMINISM.md`.

## Output

`results[i].mcts` (per-item detail):
```jsonc
{
  "best_variant": "algo_b",
  "best_value": 0.74231,
  "rollouts": 48,
  "depth": 6,
  "uct_c": 1.25,
  "visits": 48,
  "explored": 3,
  "seed": 7,
  "error": null
}
```

`run_manifest.mcts_stats` provides a run-level summary for quick indexing:
```jsonc
{
  "rollouts": 48, "depth": 6, "uct_c": 1.25,
  "visits": 48, "explored": 3, "best_value": 0.74231,
  "best_variant": "algo_b", "seed": 7, "error": null
}
```

## Autogenerate Variants (optional)

You can have the provider generate N approaches from the prompt, then run MCTS over them. Enable via alias or config:

```python
from litellm import completion

resp = completion(
  model="codeworld/mcts:auto",      # implies strategy="mcts" + autogenerate enabled
  custom_llm_provider="codeworld",
  n_variants=6,                      # how many approaches
  depth=6,
  uct_c=1.25,
  temperature=0.0,
)

# Env overrides supported: CODEWORLD_MCTS_AUTO_N, CODEWORLD_MCTS_AUTO_TEMPERATURE,
# CODEWORLD_MCTS_AUTO_MODEL, CODEWORLD_MCTS_AUTO_MAX_TOKENS

Environment gate
- The bridge honors `CODEWORLD_ENABLE_MCTS_GENERATE=1|0` to allow/skip autogeneration while still mirroring generator metadata to the manifest.
```

Generator details are mirrored into the manifest:

```jsonc
{
  "run_manifest": {
    "strategy_generator": {
      "enabled": true,
      "skipped_by_env": false,
      "n": 6,
      "model": "gpt-4o-mini",
      "temperature": 0.0,
      "max_tokens": 2000,
      "prompt_hash": "…",
      "response_hash": "…",
      "error": null
    }
  }
}
```

## Disable

```bash
export CODEWORLD_ENABLE_MCTS=0
```

## Notes

Phase‑1 uses deterministic pseudo reward (no extra variant execution). Future phases may integrate partial execution or early correctness metrics behind additional flags.

Baseline CodeWorld usage: see `feature_recipes/codeworld_provider.py` and `scenarios/codeworld_bridge_release.py`.
