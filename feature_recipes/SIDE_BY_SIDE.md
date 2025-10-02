# Side‑by‑Side Guide — Lean4 and CodeWorld

This short guide shows identical call shapes for both projects via the shared
canonical bridge schema and via LiteLLM Router providers. Copy/paste the blocks
as‑is and only change base URLs or provider args as needed.

## Canonical Bridge Requests

Both bridges accept this envelope:

- `messages`: OpenAI‐style chat history
- `items`: provider‑specific work items
- `provider`: `{ name, args }` (args are provider‑specific)
- `options`: `{ max_seconds }`

Lean4 (bridge)
```json
{
  "messages": [{"role": "system", "content": "Batch proof run"}],
  "items": [
    {"requirement_text": "0 + n = n", "context": {"section_id": "S1"}},
    {"requirement_text": "m + n = n + m", "context": {"section_id": "S2"}}
  ],
  "provider": {"name": "lean4", "args": {"flags": ["--deterministic"]}},
  "options": {"max_seconds": 180}
}
```

CodeWorld (bridge)
```json
{
  "messages": [{"role": "system", "content": "Strategy comparison"}],
  "items": [
    {"task": "strategy_compare", "context": {"section_id": "CW1"}},
    {"task": "strategy_compare", "context": {"section_id": "CW2"}}
  ],
  "provider": {"name": "codeworld", "args": {"metrics": ["correctness","speed"], "iterations": 1, "allowed_languages": ["python"]}},
  "options": {"max_seconds": 60}
}
```

Back‑compat aliases still work:
- Lean4: `lean4_requirements`, `lean4_flags`, `max_seconds`
- CodeWorld: `codeworld_metrics`, `codeworld_iterations`, `codeworld_allowed_languages`, `request_timeout`

## Router Config (YAML snippets)

Lean4 (env‑gated)
```yaml
model_list:
  - model_name: lean4-bridge
    litellm_params:
      model: lean4/bridge
      custom_llm_provider: lean4
      api_base: ${LEAN4_BRIDGE_BASE}  # e.g., http://127.0.0.1:8787
```
Enable: `LITELLM_ENABLE_LEAN4=1`.

CodeWorld (env‑gated)
```yaml
model_list:
  - model_name: codeworld-bridge
    litellm_params:
      model: codeworld/bridge
      custom_llm_provider: codeworld
      api_base: ${CODEWORLD_BASE}  # e.g., http://127.0.0.1:8887
```
Enable: `LITELLM_ENABLE_CODEWORLD=1`.

## Router Calls (Python)

Lean4 (Router)
```python
from litellm import Router
router = Router(model_list=[{"model_name":"lean4-bridge","litellm_params":{"model":"lean4/bridge","custom_llm_provider":"lean4","api_base": "http://127.0.0.1:8787"}}])
messages=[{"role":"system","content":"Batch proof run"}]
items=[{"requirement_text":"0 + n = n"},{"requirement_text":"m + n = n + m"}]
out = router.completion(model="lean4-bridge", messages=messages, items=items, options={"max_seconds":180})
print(out.choices[0].message.content)
print(getattr(out, "additional_kwargs", {}).get("lean4"))
```

CodeWorld (Router)
```python
from litellm import Router
router = Router(model_list=[{"model_name":"codeworld-bridge","litellm_params":{"model":"codeworld/bridge","custom_llm_provider":"codeworld","api_base": "http://127.0.0.1:8887"}}])
messages=[{"role":"system","content":"Strategy comparison"}]
items=[{"task":"strategy_compare"},{"task":"strategy_compare"}]
out = router.completion(model="codeworld-bridge", messages=messages, items=items, options={"max_seconds":60}, codeworld_metrics=["correctness","speed"], codeworld_iterations=1)
print(out.choices[0].message.content)
print(getattr(out, "additional_kwargs", {}).get("codeworld"))
```

## One‑liners
- Lean4 bridge: `make lean4-bridge` then `LEAN4_BRIDGE_BASE=http://127.0.0.1:8787 python scenarios/lean4_bridge_release.py`
- CodeWorld bridge: `make codeworld-bridge` then `CODEWORLD_BASE=http://127.0.0.1:8887 python scenarios/codeworld_bridge_release.py`
- Router live sweep: `python scenarios/run_all.py`
