<p align="center">
  <!-- Use outlined balanced logo for pixel-consistent rendering across systems -->
  <img src="local/artifacts/logo/SciLLM_balanced_outlined.svg" alt="SciLLM" width="100" />
  <br/>
  <img src="SciLLM_icon.svg" alt="SciLLM Icon" width="32" />
</p>

# Lean4 Prover — Quickstart

This quickstart mirrors the SciLLM (LiteLLM scientific fork) conventions. Scenarios live under
`scenarios/` and produce human- and machine-friendly JSON that can be embedded
in readiness reports or review bundles.

## 1) Install

```bash
uv venv --python=3.10.11 .venv
source .venv/bin/activate
uv pip install -e .[dev]
cp env.example .env  # optional, enables cached Lean/LiteLLM settings
```

## Mini‑Agent & codex‑agent (OpenAI‑compatible) — 60‑sec local setup

Use this if you want an OpenAI‑style endpoint for agent/router tests without any external gateway.

1) Start the mini‑agent shim (default 127.0.0.1:8788)

```bash
uvicorn litellm.experimental_mcp_client.mini_agent.agent_proxy:app --host 127.0.0.1 --port 8788
```

2) Export env (before importing Router). Do NOT append `/v1` to the base.

```bash
export LITELLM_ENABLE_CODEX_AGENT=1
export CODEX_AGENT_API_BASE=http://127.0.0.1:8788
# export CODEX_AGENT_API_KEY=...   # usually unset for local
```

3) Quick verify

```bash
curl -sSf http://127.0.0.1:8788/healthz
curl -sS -H 'content-type: application/json' \
  -d '{"model":"gpt-5","messages":[{"role":"user","content":"say hello"}]}' \
  http://127.0.0.1:8788/v1/chat/completions | jq -r '.choices[0].message.content'
```

4) Router usage (copy/paste)

```python
from litellm import Router
r = Router()
out = r.completion(
    model="gpt-5",
    custom_llm_provider="codex-agent",
    messages=[{"role":"user","content":"Return STRICT JSON only: {\"ok\":true}"}],
    response_format={"type":"json_object"}
)
print(out.choices[0].message["content"])  # OpenAI‑format
```

Port busy? Run on another port (e.g., 8789) and set `CODEX_AGENT_API_BASE=http://127.0.0.1:8789`.

## 2) Run release scenarios (fast confidence)

```bash
make run-scenarios
```

This executes `scenarios/run_all.py` which currently runs:

1. `lean4_batch_demo.py` – live E2E `cli_mini batch` using your configured Lean4 env (LLM/Docker).
2. `lean4_suggest_demo.py` – live single requirement “run” flow.

Each script prints the exact command, proof statistics, and normalized JSON
summary. Use `SCENARIOS_STOP_ON_FIRST_FAILURE=1 make run-scenarios` to
short-circuit after the first regression.

## 3) Run scenarios individually

```bash
# Deterministic batch proof (override input via LEAN4_SCENARIO_BATCH_INPUT)
python scenarios/lean4_batch_demo.py

# Single requirement (override requirement via LEAN4_SCENARIO_REQUIREMENT)
python scenarios/lean4_suggest_demo.py
```

All scripts load `.env` automatically (`python-dotenv`) so cached configuration
(e.g., `LEAN4_CLI_CMD`, LiteLLM keys) is respected.

Environment flags (preferred)
- SCILLM_ENABLE_LEAN4=1 (alias: LITELLM_ENABLE_LEAN4=1)
- SCILLM_ENABLE_CODEWORLD=1 (alias: LITELLM_ENABLE_CODEWORLD=1)
- SCILLM_ENABLE_MINI_AGENT=1 (alias: LITELLM_ENABLE_MINI_AGENT=1)

## Bridge API (optional)

```bash
uvicorn lean4_prover.bridge.server:app --reload
```

POST `/bridge/complete` with:

```json
{
  "messages": [{"role": "system", "content": "Analyse the batch"}],
  "lean4_requirements": [
    {"requirement_text": "0 + n = n"},
    {"requirement_text": "The sum of two even numbers is even"}
  ],
  "lean4_flags": [],
  "max_seconds": 180
}
```

The response mirrors the scenario JSON (summary, statistics, proof_results, stdout, stderr, duration).
Use `feature_recipes/lean4_bridge_client.py` to call the bridge directly.
For Router-style usage:

```bash
export LITELLM_ENABLE_LEAN4=1
export LEAN4_BRIDGE_BASE=http://127.0.0.1:8787
python scenarios/lean4_router_release.py
```

This mirrors the CodeWorld Router pattern for a consistent developer experience.

### Certainly alias (Lean4 umbrella)

To call Lean4 via the umbrella provider:

```bash
export LITELLM_ENABLE_CERTAINLY=1
export CERTAINLY_BRIDGE_BASE=http://127.0.0.1:8787
python scenarios/certainly_adapter_demo.py
```

Results attach under `additional_kwargs['certainly']` (optionally mirrored to `['lean4']` while migrating).

Canonical bridge schema
- Both bridges accept a canonical envelope alongside provider-specific aliases:
  - Request: { messages, items, provider: {name, args}, options: {max_seconds} }
  - Lean4 aliases still supported: lean4_requirements, lean4_flags, max_seconds
  - CodeWorld aliases still supported: codeworld_metrics, codeworld_iterations, codeworld_allowed_languages, request_timeout

CodeWorld quickstart (bridge vs Router)

Bridge
```bash
PYTHONPATH=src uvicorn codeworld.bridge.server:app --port 8887
CODEWORLD_BASE=http://127.0.0.1:8887 python scenarios/codeworld_bridge_release.py
```

Router
```bash
CODEWORLD_BASE=http://127.0.0.1:8887 python scenarios/codeworld_router_release.py
```


## 4) Deterministic tests and readiness

```bash
# Full test suite (unit + integration)
uv run pytest -q

# Composite readiness gate
python scripts/mvp_check.py

# Strict/live gate (requires Docker + providers, e.g. Ollama)
READINESS_LIVE=1 STRICT_READY=1 READINESS_EXPECT=ollama python scripts/mvp_check.py
```

Legacy pytest smokes (`tests/smoke`, `tests/ndsmoke`) are now archived. They can
still be run manually (`pytest -q tests/smoke`), but new work should prefer the
scenario scripts above for parity with LiteLLM projects.

## 5) Optional web viewer + analyzer

The viewer is an operator aid (not required for the CLI contract).

```bash
cd prototypes/lemma-graph-viewer
npm ci
npm run serve:checked
```

Optional analyzer endpoint:

```bash
npm run analyzer:serve                       # Terminal A
npm run serve:checked                        # Terminal B
# Visit the printed URL with ?lean4_api=http://127.0.0.1:8787
```

Generate demo graphs:

```bash
uv run scripts/viewers/make_synthetic_graph.py prototypes/lemma-graph-viewer/public/graph.json
```

### Need more?
- `docs/EXTRACTOR_INTEGRATION.md`—Stage 08 batch contract
- `docs/readiness/FINAL_MANUAL_CHECKLIST.md`—manual gate after `mvp_check`
- `feature_recipes/`—sample LiteLLM bridge showing how Router calls could invoke
  Lean4 via the `/bridge` surface
> Looking for the full SciLLM stack (Lean4/Certainly + CodeWorld + proxy)? See `QUICK_START.md` for Docker bring‑up (`deploy/docker/compose.scillm.stack.yml`) and scenarios covering both bridges.
