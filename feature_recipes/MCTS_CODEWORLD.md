# CodeWorld MCTS Strategy (Experimental)

Run adaptive variant selection with Monte-Carlo Tree Search (root-bandit UCT).

## Prereqs

```bash
docker compose -f local/docker/compose.scillm.stack.yml up --build -d
export CODEWORLD_BASE=http://127.0.0.1:8887
# Optional deterministic seed
export SCILLM_DETERMINISTIC_SEED=7
```

## Python Example (SciLLM Call)

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
  messages=[{"role": "user", "content": "Select best variant adaptively"}],
  custom_llm_provider="codeworld",
  items=items,
  provider={
    "name":"codeworld",
    "args":{
      "strategy":"mcts",
      "strategy_config":{"name":"mcts","rollouts":48,"depth":6,"uct_c":1.3}
    }
  },
  options={"session_id":"mcts-session","track_id":"trial-1","max_seconds":10},
  api_base=os.getenv("CODEWORLD_BASE")
)

print(resp.choices[0].message["content"])  # type: ignore[index]
details = getattr(resp, "additional_kwargs", {}).get("codeworld")
print("MCTS block:", details["results"][0]["mcts"])
```

## Output (Excerpt)

```jsonc
{
  "results": [
    {
      "index": 0,
      "mcts": {
        "best_variant": "algo_b",
        "best_value": 0.74231,
        "rollouts": 48,
        "depth": 6,
        "uct_c": 1.3,
        "visits": {"algo_a": 14, "algo_b": 20, "algo_c": 14},
        "explored": 3,
        "seed": 7,
        "error": null
      }
    }
  ],
  "run_manifest": {
    "mcts_stats": { "… same summary …" },
    "strategy": "mcts"
  }
}
```

## Notes
- Phase 1 MCTS uses a deterministic pseudo reward (no extra code execution).
- Determinism: set `SCILLM_DETERMINISTIC_SEED` or pass `strategy_config.seed`.
- Disable via `CODEWORLD_ENABLE_MCTS=0` if needed (handled by the CodeWorld bridge).
- Future: plug real early metrics or partial executions for tighter fidelity.

