<p align="center">
  <!-- Use outlined balanced logo for pixel-consistent rendering across systems -->
  <img src="local/artifacts/logo/SciLLM_balanced_outlined.svg" alt="SciLLM" width="100" />
  <br/>
  <img src="SciLLM_icon.svg" alt="SciLLM Icon" width="32" />
</p>

# SciLLM Multi‑Surface Quickstart

> Environment Prefix Preference: Use `SCILLM_` (e.g. `SCILLM_ENABLE_CODEX_AGENT=1`). Legacy `LITELLM_` variables still work.
> Model IDs: Replace `<MODEL_ID>` placeholders with real IDs from `GET $CODEX_AGENT_API_BASE/v1/models`.

Former references to `gpt-5` were illustrative; they are now replaced with `<MODEL_ID>`.

This unified quickstart covers:
1. codex‑agent (OpenAI‑compatible shim / sidecar)
2. mini‑agent (local deterministic MCP‑style loop)
3. CodeWorld (strategy orchestration + MCTS)
4. Certainly / Lean4 bridge

If you only need Lean4 specifics, see `LEAN4_QUICKSTART.md` (can be factored separately).
## 0) Prerequisites

| Item | Minimum | Notes |
|------|---------|-------|
| Python | 3.10.11 | Tested with uv; 3.11+ generally fine |
| uv | Latest | For environment + sync speed |
| Docker | 24+ | Required for sidecars/full stack |
| jq | Any | For JSON filtering in shell examples |
| Redis (optional) | 6+ | Caching & session history; auto‑fallback to in‑memory |

> Replace `<MODEL_ID>` wherever shown with an actual ID discovered via `GET $CODEX_AGENT_API_BASE/v1/models` or your provider listing.

## 1) Install (Local Dev)

```bash
uv venv --python=3.10.11 .venv
source .venv/bin/activate
uv pip install -e .[dev]
cp env.example .env  # optional, enables cached Lean/LiteLLM settings
```

## 2) Mini‑Agent & codex‑agent (OpenAI‑compatible) — 60‑sec local setup (Zero‑ambiguity)

Happy Path (copy/paste):
- Run ONE of these: local mini‑agent or Docker sidecar
- Set `CODEX_AGENT_API_BASE` without `/v1`
- Discover a model id via `/v1/models`
- Make a high‑reasoning cURL, or call via Router

Extractor pipeline — zero‑ambiguity checklist (copy/paste)

```bash
# 0) Activate env and load .env if present
source .venv/bin/activate
set -a; [ -f .env ] && source .env; set +a

# 1) Choose ONE base (no /v1) mini‑agent (8788) or sidecar (8077)
export CODEX_AGENT_API_BASE=http://127.0.0.1:8788  # or 8077

# 2) Map to OpenAI envs expected by the extractor HTTP client
export OPENAI_BASE_URL="$CODEX_AGENT_API_BASE"     # no /v1 suffix
export OPENAI_API_KEY="${CODEX_AGENT_API_KEY:-none}"  # 'none' for echo/dev

# 3) Sanity probes
curl -sSf "$CODEX_AGENT_API_BASE/healthz"
curl -sS  "$CODEX_AGENT_API_BASE/v1/models" | jq -r '.data[].id'

# 4) (Optional) High‑reasoning chat
MODEL_ID=$(curl -sS "$CODEX_AGENT_API_BASE/v1/models" | jq -r '.data[0].id')
curl -sS "$CODEX_AGENT_API_BASE/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL_ID\",\"reasoning\":{\"effort\":\"high\"},\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}" \
  | jq -r '.choices[0].message.content'
```

Provider name & model ID — avoid confusion
- `model="codex-agent/<MODEL_ID>"`, or
- `model="<MODEL_ID>", custom_llm_provider="codex-agent"`.
- Always discover a valid `<MODEL_ID>` via `GET $CODEX_AGENT_API_BASE/v1/models`.

One-time session preflight (optional)

JSON mode (auto sanitize)
- Enable once so responses in JSON mode are cleaned automatically if providers return fences/prose.
  - `export SCILLM_JSON_SANITIZE=1`
  - Or pass `auto_json_sanitize=True` to completion()/acompletion() when using `response_format={"type":"json_object"}` or `response_mime_type="application/json"`.
- Cache model IDs once at process start to avoid repeated lookups and fail fast on unknown IDs.
  ```python
  from litellm.extras.preflight import preflight_models
  import os
  preflight_models(api_base=os.environ["CHUTES_API_BASE"], api_key=os.environ.get("CHUTES_API_KEY"))
  # Enable guard in calls (reads cached set; zero network):
  #   export SCILLM_MODEL_PREFLIGHT=1
# Optional: enable alias resolution for doc-style names -> canonical IDs
export SCILLM_MODEL_ALIAS=1
  ```



OpenAI‑compatible (Chutes) usage note
- Optional: `fallback_closest=True` (default) resolves unknown doc-style names to the closest live ID from your cached catalog. Tune with `fallback_closest_cutoff=0.55`.
- Use vendor‑first model IDs from `/v1/models` (e.g., `moonshotai/Kimi-K2-Instruct-0905`).
- Passing `custom_llm_provider="openai"` is optional — scillm now defaults
  to 'openai' when `api_base` points to an OpenAI‑compatible gateway (e.g., `CHUTES_API_BASE`).
- Avoid adding an `openai/` prefix; if present, it will be stripped for OpenAI‑compatible providers.

Python one‑liner (Chutes):
```python
from scillm import completion; import os
print(completion(model='moonshotai/Kimi-K2-Instruct-0905',
                messages=[{'role':'user','content':'{}'}],
                response_format={'type':'json_object'},
                api_base=os.environ['CHUTES_API_BASE'],
                api_key=os.environ.get('CHUTES_API_KEY','')).choices[0].message.content)
```
Extractor pipeline (HTTP client) env mapping (single place; reused later)

```bash
# If your client expects OpenAI envs, map them to codex‑agent
export OPENAI_BASE_URL="$CODEX_AGENT_API_BASE"     # do NOT append /v1
export OPENAI_API_KEY="${CODEX_AGENT_API_KEY:-none}"
```

### Mini‑Agent (MCP) Quickstart

```bash
# Start the MCP-style mini‑agent locally (OpenAI‑compatible shim for tools)
uvicorn litellm.experimental_mcp_client.mini_agent.agent_proxy:app \
  --host 127.0.0.1 --port 8788 --log-level warning

# Probe
curl -sSf http://127.0.0.1:8788/ready || true

# In‑process (deterministic) variant example
python examples/mini_agent_inprocess.py
```

Notes:
- Mini‑Agent uses MCP‑style semantics for tool execution. Point HTTP clients at `$CODEX_AGENT_API_BASE` with no `/v1` suffix (provider adds endpoints).
- Limitations: no streaming responses; images in multi‑part messages are detected but not processed.
- Trace storage (optional):
  ```
  export MINI_AGENT_STORE_TRACES=1
  export MINI_AGENT_STORE_PATH=local/artifacts/mini_agent_traces.jsonl
  ```

Use this if you want an OpenAI‑style endpoint for agent/router tests without any external gateway.

1) Start the mini‑agent shim (default 127.0.0.1:8788)

```bash
uvicorn litellm.experimental_mcp_client.mini_agent.agent_proxy:app --host 127.0.0.1 --port 8788
```

2) Export env (before importing Router). (No `/v1`.)

```bash
export LITELLM_ENABLE_CODEX_AGENT=1
export CODEX_AGENT_API_BASE=http://127.0.0.1:8788
# export CODEX_AGENT_API_KEY=...   # usually unset for local
```

3) Quick verify

```bash
curl -sSf "$CODEX_AGENT_API_BASE/healthz"
curl -sS -H 'content-type: application/json' \
  -d '{"model":"<MODEL_ID>","messages":[{"role":"user","content":"say hello"}]}' \
  "$CODEX_AGENT_API_BASE/v1/chat/completions" | jq -r '.choices[0].message.content'
```

Also available (sidecar 8077) — identical API surface:

```bash
docker compose -f local/docker/compose.agents.yml up --build -d codex-sidecar
export CODEX_AGENT_API_BASE=http://127.0.0.1:8077   # no /v1
curl -sSf http://127.0.0.1:8077/healthz
curl -sS  "$CODEX_AGENT_API_BASE/v1/models" | jq .
curl -sS -H 'content-type: application/json' \
  -d '{"model":"<MODEL_ID>","reasoning":{"effort":"high"},"messages":[{"role":"system","content":"Return STRICT JSON only: {\"ok\": true}"}]}' \
  "$CODEX_AGENT_API_BASE/v1/chat/completions" | jq -r '.choices[0].message.content'
```

Doctor (one‑shot self‑test):

```bash
make codex-agent-doctor
```

Capabilities & expectations

- Mini‑agent (local shim):
  - Text‑only, non‑streaming; images are ignored
  - No server‑side caching; no token usage fields
  - Great for transport sanity and Router demos
- Sidecar (Docker):
  - OpenAI‑compatible pass‑through; streaming/usage/vision depend on the upstream provider

Strict JSON & stop tokens (client‑side enforcement)

- Gateway does not enforce strict JSON. Enforce client‑side and validate:

```python
from openai import OpenAI
client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"], api_key=os.getenv("OPENAI_API_KEY","none"))
resp = client.chat.completions.create(
    model="<MODEL_ID>",
    response_format={"type":"json_object"},
    stop=["```","END_JSON"],
    messages=[{"role":"user","content":"Return {\\"ok\\": true} as JSON only."}],
)
data = resp.choices[0].message.content
# validate/strip here per your pipeline
```

Client cache (optional; LiteLLM) — in‑memory fallback if Redis absent

```python
from litellm.extras import initialize_litellm_cache
initialize_litellm_cache()  # uses REDIS_HOST/PORT if available, else in‑memory

# Optional: run‑scoped namespace (advanced)
import litellm
from litellm.caching.caching import Cache, LiteLLMCacheType
litellm.cache = Cache(type=LiteLLMCacheType.REDIS, host=os.getenv("REDIS_HOST","localhost"), port=os.getenv("REDIS_PORT","6379"), namespace=f"run:{os.getenv('RUN_ID','dev')}")
litellm.enable_cache()
```

Auth for codex‑agent sidecar (when echo disabled)

- The compose sets `CODEX_SIDECAR_ECHO=1` by default (no creds required). To run with real creds:
  - Remove or set `CODEX_SIDECAR_ECHO: "0"` for codex‑sidecar in `local/docker/compose.agents.yml`.
  - Mount your auth: `- ${HOME}/.codex/auth.json:/root/.codex/auth.json:ro`.
  - Verify inside Docker: `python debug/check_codex_auth.py --container litellm-codex-agent`.

Debug probes (transport sanity)

```bash
python debug/verify_mini_agent.py           # mini‑agent /ready + /agent/run
python debug/verify_codex_agent_docker.py   # sidecar /healthz + /v1 endpoints
python debug/codex_parallel_probe.py        # Router.parallel_acompletions → codex‑agent echo
```

### Parallel Fan‑Out (Advanced)
Ordered parallel:
```python
results = await Router().parallel_acompletions(reqs, concurrency=8)
for r in results:
    print(r.index, r.content, r.error)
```
Result object fields: `index, request, response, error, content`.

Unordered experimental:
`async for r in Router().parallel_as_completed(reqs): ...` (surface may change; prefer the ordered variant for pipelines).

4) Router usage (copy/paste)

```python
from litellm import Router
r = Router()
out = r.completion(
    model="<MODEL_ID>",
    custom_llm_provider="codex-agent",
    messages=[{"role":"user","content":"Return STRICT JSON only: {\"ok\":true}"}],
    reasoning_effort="high",
    response_format={"type":"json_object"}
)
print(out.choices[0].message["content"])  # OpenAI‑format

Optional: one‑time model_list mapping for a cleaner judge call

```python
from litellm import Router
r = Router(model_list=[{"model_name":"gpt-5","litellm_params":{"model":"gpt-5","custom_llm_provider":"codex-agent","api_base":os.getenv("CODEX_AGENT_API_BASE"),"api_key":os.getenv("CODEX_AGENT_API_KEY")}}])
```

Port busy? Run on another port (e.g., 8789) and set `CODEX_AGENT_API_BASE=http://127.0.0.1:8789`.

Troubleshooting — fastest fixes
- 404 on `/v1/chat/completions`: model id is not registered → use one from `/v1/models`.
- 400/502 from sidecar: upstream provider not wired → enable echo or configure a real backend.
- `Skipping codex-agent scenario (...)`: set `CODEX_AGENT_API_BASE` and retry.
- Base includes `/v1`: remove it; the adapter expects a base without the suffix.

## 3) Run release scenarios (fast confidence)

```bash
make run-scenarios
```

This executes `scenarios/run_all.py` which currently runs:

1. `lean4_batch_demo.py` – live E2E proof batch
2. `lean4_suggest_demo.py` – single requirement flow

Each script prints the exact command, proof statistics, and normalized JSON
summary. Use `SCENARIOS_STOP_ON_FIRST_FAILURE=1 make run-scenarios` to
short-circuit after the first regression.

## 4) Run scenarios individually

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
- SCILLM_ENABLE_CODEX_AGENT=1 (alias: LITELLM_ENABLE_CODEX_AGENT=1)

## Any Project Agent: codex‑agent in 3 steps (copy/paste)

```bash
# 1) Pick a base (no /v1)
export CODEX_AGENT_API_BASE=http://127.0.0.1:8788   # or 8077 if using sidecar

# 2) Sanity checks
curl -sSf "$CODEX_AGENT_API_BASE/healthz"
curl -sS  "$CODEX_AGENT_API_BASE/v1/models" | jq .

# 3) OpenAI‑compatible call with high reasoning (HTTP clients use the exact id from /v1/models)
curl -sS "$CODEX_AGENT_API_BASE/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5","reasoning":{"effort":"high"},"messages":[{"role":"user","content":"ping"}]}' \
  | jq -r '.choices[0].message.content'
```

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
PYTHONPATH=src uvicorn codeworld.bridge.server:app --port 8888
CODEWORLD_BASE=http://127.0.0.1:8888 python scenarios/codeworld_bridge_release.py
```

Router
```bash
CODEWORLD_BASE=http://127.0.0.1:8888 python scenarios/codeworld_router_release.py
```

Bridge (Docker, no Redis required)
```bash
make codeworld-bridge-up-only
# Health probe
curl -sSf http://127.0.0.1:8888/healthz
```

### MCTS quick calls (CodeWorld)

```python
from litellm import completion
import os
os.environ["CODEWORLD_BASE"] = os.getenv("CODEWORLD_BASE", "http://127.0.0.1:8888")

items = [{"task":"t","context":{"code_variants":{"a":"def solve(ctx): return 1","b":"def solve(ctx): return 2"}}}]

# 1) Explicit strategy
resp = completion(model="codeworld", custom_llm_provider="codeworld", items=items,
                  strategy="mcts", rollouts=24, depth=5, uct_c=1.25, api_base=os.environ["CODEWORLD_BASE"])

# 2) Alias sugar
resp = completion(model="codeworld/mcts", custom_llm_provider="codeworld", items=items,
                  api_base=os.environ["CODEWORLD_BASE"])

# 3) Autogenerate N variants then MCTS
resp = completion(model="codeworld/mcts:auto", custom_llm_provider="codeworld",
                  n_variants=6, depth=6, uct_c=1.25, temperature=0.0, api_base=os.environ["CODEWORLD_BASE"])
# Env overrides supported: CODEWORLD_MCTS_AUTO_N, CODEWORLD_MCTS_AUTO_TEMPERATURE, CODEWORLD_MCTS_AUTO_MODEL, CODEWORLD_MCTS_AUTO_MAX_TOKENS
# Note: The synonym model="codeworld/mcts+auto" is accepted; responses/manifests normalize to the canonical "codeworld/mcts:auto".
```


One‑POST autogenerate (HTTP)

```bash
# Ensure the bridge can reach your codex‑agent:
#   - Local sidecar on host: export CODEX_AGENT_API_BASE=http://127.0.0.1:8089
#   - Docker bridge uses http://host.docker.internal:8089 by default (see local/docker/compose.codeworld.bridge.yml)
#     On many Linux hosts, host.docker.internal is not present by default — compose adds:
#       extra_hosts: ["host.docker.internal:host-gateway"]
#     Override with: CODEX_AGENT_API_BASE=http://<your-host-ip>:8089

BASE=${CODEWORLD_BASE:-http://127.0.0.1:8888}
curl -sS "$BASE/bridge/complete" -H 'Content-Type: application/json' -d '{
  "messages": [{"role":"user","content":"Autogenerate variants then search"}],
  "items": [{"task":"mcts-live-auto","context":{}}],
  "provider": {"name":"codeworld", "args":{
    "strategy":"mcts",
    "strategy_config": {"autogenerate": {"enabled": true, "n": 3}, "rollouts": 24, "depth": 6, "uct_c": 1.25}
  }}
}' | jq '.run_manifest.mcts_stats'

Notes:
- Timeout: default one‑POST client timeout is 60s; override with CODEWORLD_ONEPOST_TIMEOUT_S.
- Autogen defaults: n=3, temperature=0, max_tokens=2000. Override with:
  CODEWORLD_MCTS_AUTO_N, CODEWORLD_MCTS_AUTO_TEMPERATURE, CODEWORLD_MCTS_AUTO_MAX_TOKENS.
- Model discovery: “gpt‑5” in examples is illustrative; always use ids from GET $CODEX_AGENT_API_BASE/v1/models.
- Reasoning: examples use reasoning={"effort":"high"} to demonstrate capability; it is optional, not required.
```


## 5) Deterministic tests & readiness

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

### Additional References
- `docs/EXTRACTOR_INTEGRATION.md` — batch contract
- `docs/readiness/FINAL_MANUAL_CHECKLIST.md` — post‐`mvp_check` manual gate
- `feature_recipes/` — focused examples (parallel completion, MCTS, bridges)
- `FEATURES.md` — concise matrix & patterns

### Environment Summary (Cheat Sheet)
| Category | Variables | Notes |
|----------|-----------|-------|
| Enable flags | `SCILLM_ENABLE_CODEX_AGENT`, `SCILLM_ENABLE_CODEWORLD`, `SCILLM_ENABLE_LEAN4`, `SCILLM_ENABLE_MINI_AGENT` | Prefer `SCILLM_` prefix |
| Bases | `CODEX_AGENT_API_BASE`, `CODEWORLD_BASE`, `LEAN4_BRIDGE_BASE` / `CERTAINLY_BRIDGE_BASE` | No `/v1` on codex base |
| Optional tuning | `SCILLM_DETERMINISTIC_SEED`, `CODEWORLD_MCTS_AUTO_*` | Determinism & MCTS |
| Retry/logging | `SCILLM_RETRY_META`, `SCILLM_LOG_JSON`, `SCILLM_RETRY_LOG_EVERY` | Structured telemetry |
| Mini‑agent tracing | `MINI_AGENT_STORE_TRACES=1`, `MINI_AGENT_STORE_PATH` | Append JSONL traces |
| Warmups/readiness | `STRICT_WARMUPS`, `READINESS_LIVE`, `STRICT_READY`, `READINESS_EXPECT` | Gates & provider expectations |

Security note: Disable codex sidecar echo (remove `CODEX_SIDECAR_ECHO=1`) before supplying real credentials.

---
End of Multi‑Surface Quickstart.


### Retry Metadata Example
Enable:
```
export SCILLM_RETRY_META=1
```
Excerpt from response (when retries occur):
```json
"additional_kwargs": {
  "router": { "retries": { "attempts": 2, "total_sleep_s": 5.4, "last_retry_after_s": 3.0 } }
}
```

### Security & Isolation (Summary)
| Area | Note |
|------|------|
| codex‑agent echo | Disable before real credentials (remove `CODEX_SIDECAR_ECHO=1`). |
| CodeWorld sandbox | Process RLIMITs + optional network namespace; use containers for stronger guarantees. |
| Mini‑Agent outputs | Treat as untrusted until validated. |
| Model reasoning flag | Optional; omit if provider rejects it. |
