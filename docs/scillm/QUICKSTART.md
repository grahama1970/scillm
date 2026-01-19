<p align="center">
  <!-- Use PNG for GitHub rendering reliability -->
  <img src="local/artifacts/logo/SciLLM_balanced.light@2x.png" alt="SciLLM" width="100" />
  <br/>
  <img src="local/artifacts/logo/scillm_mark_32.png" alt="SciLLM Icon" width="32" />
</p>

# SciLLM Multi‑Surface Quickstart

Before you start, verify the Chutes front door:

```bash
make chutes-front-door
```

## SCILLM Agent Cheat Sheet (one page)

- **Surfaces (pick one)**
  - Chutes / OpenAI‑compatible: `custom_llm_provider="openai_like"`, `api_base=$CHUTES_API_BASE` (includes `/v1`), `api_key=$CHUTES_API_KEY`.
  - Local Ollama: `custom_llm_provider="ollama"`, `api_base=${OLLAMA_API_BASE:-http://127.0.0.1:11434}`; model is the Ollama tag (e.g., `qwen3:1.7b`). Prefixing `ollama/` is optional; LiteLLM strips it.
  - Codex‑agent sidecar: set `CODEX_AGENT_API_BASE` (no `/v1`), model from `/v1/models`, provider `custom_llm_provider="codex-agent"` or model prefix `codex-agent/…`.

- **Minimal examples**
  - Paved JSON (Chutes):
    ```python
    from scillm import completion
    r = completion(
        model=os.environ["CHUTES_MODEL_ID"],
        api_base=os.environ["CHUTES_API_BASE"],
        api_key=os.environ["CHUTES_API_KEY"],
        custom_llm_provider="openai_like",
        messages=[{"role":"user","content":"Return only {\"ok\":true} as JSON."}],
        response_format={"type":"json_object"},
        temperature=0, timeout=20,
    )
    print(r.choices[0].message.content)
    ```
  - Local Ollama JSON:
    ```python
    import os
    from scillm import completion
    os.environ.setdefault("OLLAMA_API_BASE", "http://127.0.0.1:11434")
    r = completion(
        model=os.environ.get("OLLAMA_MODEL","qwen3:1.7b"),
        api_base=os.environ["OLLAMA_API_BASE"],
        custom_llm_provider="ollama",
        messages=[{"role":"user","content":"ping"}],
        temperature=0, max_tokens=64, timeout=15,
    )
    print(r.choices[0].message.content)
    ```
  - Batch (paved, Chutes):
    ```python
    import asyncio, os
    from scillm import batch_acompletions

    async def main():
        reqs = [
            {"model": os.environ["CHUTES_MODEL_ID"], "messages": [{"role": "user", "content": "hi"}], "response_format": {"type": "json_object"}},
            {"model": os.environ["CHUTES_MODEL_ID"], "messages": [{"role": "user", "content": "hi"}], "response_format": {"type": "json_object"}},
            {"model": os.environ["CHUTES_MODEL_ID"], "messages": [{"role": "user", "content": "hi"}], "response_format": {"type": "json_object"}},
        ]
        resps = await batch_acompletions(
            reqs,
            api_base=os.environ["CHUTES_API_BASE"],
            api_key=os.environ["CHUTES_API_KEY"],
            custom_llm_provider="openai_like",
            concurrency=4,
            timeout=20,
            wall_time_s=120,
        )
        print([r.get("content") if isinstance(r, dict) and not r.get("error") else r for r in resps])

    asyncio.run(main())
    ```

- **Discover models**
  - Chutes: `python - <<'PY'\nfrom scillm.paved import list_models_openai_like; import os; print(list_models_openai_like(os.environ["CHUTES_API_BASE"], os.environ["CHUTES_API_KEY"]))\nPY`
  - Ollama: `curl -s ${OLLAMA_API_BASE:-http://127.0.0.1:11434}/api/tags | jq -r '..|.name? // empty'`
  - Codex‑agent: `curl -s ${CODEX_AGENT_API_BASE}/v1/models | jq -r '.data[].id'`

- **Troubleshooting quick hits**
  - 404 model not found on Chutes → pick a model from `/v1/models` or set `CHUTES_MODEL_ID`.
  - Local model not found → ensure `ollama list` shows the tag and `OLLAMA_API_BASE` points to the host‑reachable port.
  - Bearer vs x-api-key → let paved helpers set headers; don’t add `extra_headers`.
  - Shut down shared clients: `from scillm.paved import shutdown; shutdown()`.

- **Environment defaults (suggested)**
  - `CHUTES_API_BASE=https://llm.chutes.ai/v1`, `CHUTES_API_KEY=...`, `CHUTES_MODEL_ID=<from /v1/models>`
  - `OLLAMA_API_BASE=http://127.0.0.1:11434`, `OLLAMA_MODEL=qwen3:1.7b`
  - `CODEX_AGENT_API_BASE=http://127.0.0.1:8788`

## Core Paved Paths (Most‑Used)

Use these three patterns by default:

1) **Single model (simple calls)**  
   `completion(...)` / `acompletion(...)` — same as LiteLLM.

2) **LLM batches (progress or ordered results)**  
   - Ordered: `batch_acompletions(...)`  
   - Progress/as‑completed: `batch_acompletions_iter(...)`

3) **Formal proofs (Lean4/Certainly)**
   - Direct mode (preferred): `from scillm.integrations.certainly import prove_requirement`
   - Or via provider: `certainly_prove(items=[...])` / `completion(... custom_llm_provider="certainly" ...)`
   - Results live in `resp.additional_kwargs["certainly"]["results"]`

Multi‑model fallback/routing: **use `Router(model_list=...)`** as the standard path (avoid custom fallback loops).

## Sanity Checks (Text + VLM, Batch)

Use the built-in 5-call sanity script (text + VLM) to verify your tenant/models end-to-end. It prints a single JSON summary and exits 0/1. The calls cover strict-JSON echo, France/Paris, HTTPS VLM, local file-path VLM, and an inline HTML classification fixture (token `luminous-harvest`) so html/file-path extraction regressions show up immediately.

Before running large batches, run the paved preflight helper (Python or skill CLI) so Step 07 fails fast when the target model is missing. For VLM workloads, the pi-mono skill exposes `--dry-run` and `--inline-remote-images` so you can validate local file paths, HTTPS URLs, and inline conversions before making live calls:

```bash
.pi/skills/scillm/run.sh preflight preflight --model "$CHUTES_MODEL_ID" --json
```

```bash
# Required env: CHUTES_API_BASE, CHUTES_API_KEY
# Preferred: CHUTES_TEXT_MODEL (or CHUTES_MODEL_ID)
# For VLM prompts: CHUTES_VLM_MODEL

PYTHONPATH=$(pwd) \
python scripts/sanity/chutes_batch_sanity.py

# or via Makefile
make scillm-sanity

# As-completed (progress) mode
# Use --verbose to print per-scenario lines as each finishes via parallel_acompletions_iter.
# Add --inline-remote-images (sets SCILLM_INLINE_REMOTE_IMAGES=1) to download HTTPS image URLs and inline them (useful if the gateway cannot fetch your URLs directly).
# The final JSON summary keeps the original order even though execution is unordered internally.
# Example:
# pi-mono skill dry-runs (no creds required)
.pi/skills/scillm/run.sh vlm describe tests/fixtures/checkerboard.png --json --dry-run
.pi/skills/scillm/run.sh vlm describe https://picsum.photos/seed/scillm/64/64 --json --dry-run --inline-remote-images
.pi/skills/scillm/run.sh vlm batch --input tests/fixtures/vlm_batch.jsonl --json --dry-run --inline-remote-images
# python scripts/sanity/chutes_batch_sanity.py --execute --verbose --inline-remote-images
```

## Project Defaults (.env)

These are the repo‑wide defaults we ship in `.env` for convenience. They are examples; always verify your tenant actually serves them via `GET $CHUTES_API_BASE/models` (or run `make chutes-front-door`).

```bash
# New default models
export CHUTES_MODEL_ID="${CHUTES_MODEL_ID:-moonshotai/Kimi-K2-Thinking}"      # text (recommended)
export CHUTES_VLM_MODEL="${CHUTES_VLM_MODEL:-Qwen/Qwen3-VL-235B-A22B-Instruct}" # vision
export CHUTES_TOOLS_MODEL="${CHUTES_TOOLS_MODEL:-moonshotai/Kimi-K2-Instruct-0905}"

# Optional: Router fallbacks (used only when you opt into Router alternates)
export CHUTES_TEXT_MODEL_ALT1="moonshotai/Kimi-K2-Instruct-0905"
export CHUTES_TEXT_MODEL_ALT2="deepseek-ai/DeepSeek-R1-0528"
export CHUTES_TEXT_MODEL_ALT3="Qwen/Qwen3-235B-A22B-Instruct-2507"
export CHUTES_VLM_MODEL_ALT1="Qwen/Qwen2.5-VL-72B-Instruct"
export CHUTES_VLM_MODEL_ALT2="OpenGVLab/InternVL3-78B"
```

Notes
- These defaults may not be routable on all tenants. If `make chutes-front-door` fails or `/models` doesn’t list them, pick any listed model and export it to override.
- “One way” still applies: the endpoint and Bearer auth are identical for text and vision; only the model id and `messages` body differ. Fallbacks apply only when you explicitly use the Router alternates.
## SciLLM Paved Path (Chutes) — Copy/Paste Recipe (Unified Bearer Auth)

Use this single, verified path for Chutes (OpenAI‑compatible) JSON chat. It hides provider quirks and uses Authorization: Bearer for both JSON and streaming.

- Env
  - `CHUTES_API_BASE=https://llm.chutes.ai/v1`
  - `CHUTES_API_KEY=cpk_…`
  - `CHUTES_TEXT_MODEL=moonshotai/Kimi-K2-Instruct-0905`

- Code (deterministic JSON, closes Router internally)

```python
from scillm.paved import chutes_chat_json

out = chutes_chat_json(
    messages=[
        {"role": "system", "content": 'Return only {"ok":true} as JSON.'},
        {"role": "user", "content": "ping"},
    ],
    max_tokens=64,
    temperature=0.0,
    timeout=20.0,
    tenacious=False,
)
print(out.choices[0].message.get("content"))  # → {"ok":true}
```

- Router variant (batching / alternates via env):

```python
from scillm.paved import chutes_router_json

out = chutes_router_json(
    messages=[{"role": "user", "content": "ping"}],
    kind="text",        # or "vlm"
    timeout=20.0,
    tenacious=False,
)
print(out.choices[0].message.get("content"))
```

### Budget & Metrics quick path

- Budget status endpoint (via proxy):
```bash
curl -s http://127.0.0.1:4010/v1/budget | jq
```
- Start Prometheus and the proxy locally:
```bash
make prom-run-docker
LITELLM_MASTER_KEY=sk-dev-proxy-123 METRICS_ENV=dev make proxy-run-uv
```
- Import Grafana dashboards (use a Service Account token):
```bash
GRAFANA_URL=http://127.0.0.1:3000 \
GRAFANA_TOKEN=$GRAFANA_SCILLM_SERVICE_TOKEN \
make grafana-import
```
- Generate one request to light up metrics and print headers:
```bash
curl -si -H 'Authorization: Bearer sk-dev-proxy-123' \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-chutes","messages":[{"role":"user","content":"ping"}]}' \
  http://127.0.0.1:4010/v1/chat/completions | tee /dev/stderr | jq
# Expect headers (when gateway handles the call):
#   x-ratelimit-limit, x-ratelimit-remaining-requests, x-budget-reset-at
# And /metrics to show sc_calls_total and sc_request_seconds; sc_cost_usd_total increments if pricing is configured.
```

- Sanity probes
  - `curl -H "Authorization: Bearer $CHUTES_API_KEY" "$CHUTES_API_BASE/models"` → 200
  - `curl -H "Authorization: Bearer $CHUTES_API_KEY" -H "content-type: application/json" \`
    `-d '{"model":"'$CHUTES_TEXT_MODEL'","messages":[{"role":"user","content":"ping"}],"response_format":{"type":"json_object"}}' \`
    `"$CHUTES_API_BASE/chat/completions"` → 200

- Notes
  - Use Authorization: Bearer for both non‑stream JSON and streaming. Some gateways accept `x-api-key` for non‑stream JSON; this path standardizes on Bearer.
  - JSON mode content may be string or dict; normalize both if you post‑process.
  - If you construct Router yourself, call `await router.aclose()` when done.
  - Pin exactly one `CHUTES_TEXT_MODEL`/`CHUTES_VLM_MODEL` (alternates are disabled in locked bearer mode).

> Environment Prefix Preference: Use `SCILLM_` (e.g. `SCILLM_ENABLE_CODEX_AGENT=1`). Legacy `LITELLM_` variables still work.
> Model IDs: Replace `<MODEL_ID>` placeholders with real IDs from `GET $CODEX_AGENT_API_BASE/v1/models`.

Former references to `gpt-5` were illustrative; they are now replaced with `<MODEL_ID>`.

This unified quickstart covers:
1. codex‑agent (OpenAI‑compatible shim / sidecar)
2. mini‑agent (local deterministic MCP‑style loop)
3. CodeWorld (strategy orchestration + MCTS)
4. Certainly / Lean4 bridge

If you only need Lean4 specifics, see `LEAN4_QUICKSTART.md` (can be factored separately).
Paved‑path example: see `SCILLM_PAVED_PATH_CONTRACT.md#certainly--lean4-paved-path`.
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



OpenAI‑compatible (Chutes) — Paved Path

Happy Path (shared base)
- Discover a model id via `GET $CHUTES_API_BASE/v1/models`.
- Use `Authorization: Bearer` for both JSON and streaming.
- Single JSON call (Python):
  ```python
from scillm import completion, os
r = completion(
  model=os.environ["CHUTES_MODEL_ID"],
  api_base=os.environ["CHUTES_API_BASE"],
  api_key=os.environ["CHUTES_API_KEY"],  # becomes Authorization: Bearer
  custom_llm_provider="openai_like",
  messages=[{"role":"user","content":"Return only {\"ok\":true} as JSON."}],
  response_format={"type":"json_object"},
  temperature=0, max_tokens=16)
print(r.choices[0].message.get("content",""))
  ```
  
  Strict JSON helper (preferred name, optional):
  ```python
  from scillm.extras import strict_json_completion, os
  r = strict_json_completion(
    api_base=os.environ["CHUTES_API_BASE"], api_key=None,
    extra_headers={"Authorization": f"Bearer {os.environ['CHUTES_API_KEY']}"},
    model=os.environ["CHUTES_TEXT_MODEL"],
    messages=[{"role":"user","content":"Return only {\"ok\":true} as JSON."}],
    timeout=30)
  print(r["choices"][0]["message"]["content"])
  ```

  Hedge‑once helper (optional; races 2–3 header variants then caches winner):
  ```python
  import asyncio, os
  from scillm.extras import hedged_json_completion
  async def _run():
    r = await hedged_json_completion(
      api_base=os.environ["CHUTES_API_BASE"], key=os.environ["CHUTES_API_KEY"],
      model=os.environ["CHUTES_TEXT_MODEL"],
      messages=[{"role":"user","content":"Return only {\"ok\":true} as JSON."}],
      timeout=30)
    print(r.choices[0].message.get('content',''))
  asyncio.run(_run())
  ```
- Streaming (text):
  ```python
  from scillm import acompletion, os, asyncio
  async def main():
    s = await acompletion(
      model=os.environ["CHUTES_TEXT_MODEL"], api_base=os.environ["CHUTES_API_BASE"],
      api_key=None, custom_llm_provider="openai_like",
      extra_headers={"Authorization": f"Bearer {os.environ['CHUTES_API_KEY']}"},
      messages=[{"role":"user","content":"Tell me a 50 word story."}], stream=True,
      temperature=0)
    async for ev in s: print(ev)
    await s.aclose()
  asyncio.run(main())
  ```
- Tools (Kimi‑K2, non‑stream):
  ```python
  tools=[{"type":"function","function":{"name":"get_weather","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}]
  r = completion(model=os.environ["CHUTES_TOOLS_MODEL"], api_base=os.environ["CHUTES_API_BASE"],
    api_key=None, custom_llm_provider="openai_like",
    extra_headers={"Authorization": f"Bearer {os.environ['CHUTES_API_KEY']}"},
    messages=[{"role":"user","content":"What is the weather in Tokyo right now?"}],
    tools=tools, tool_choice="auto", temperature=0, stream=False)
  print(r.choices[0].finish_reason, r.choices[0].tool_calls)
  ```
- Batch (Router JSON): set `x-api-key` in `model_list`:
  ```python
  from scillm import Router, os
  router = Router(model_list=[{"model_name":"chutes","litellm_params":{
    "model": os.environ["CHUTES_MODEL_ID"],
    "api_base": os.environ["CHUTES_API_BASE"],
    "api_key": None,
    "custom_llm_provider": "openai_like",
    "extra_headers": {"Authorization": f"Bearer {os.environ['CHUTES_API_KEY']}"}}}])
  # then: await router.parallel_acompletions([...], concurrency=K)
  ```

Auto-pick peer alternates (CLI)

- When you don’t know which vendor IDs on your tenant are peer‑tier to your primary, use the helper to probe `/models`, pick size/quality peers, and optionally verify each with a minimal JSON chat. It prints a `.env` snippet and a ready‑to‑paste Router example:
  ```bash
  # Text models
  python scripts/chutes_auto_peers.py --kind text --verify \
    --base "$CHUTES_API_BASE" --key "$CHUTES_API_KEY" 

  # Vision models
  python scripts/chutes_auto_peers.py --kind vlm --verify \
    --base "$CHUTES_API_BASE" --key "$CHUTES_API_KEY"
  ```
  Paste the suggested `CHUTES_*_MODEL[_ALT*]=...` lines into your `.env` and use the Router example as‑is.

Tenacious single‑model (dataset‑safe)

- For reproducible datasets where you must use exactly one vendor model (no alternates), enable a long‑running, capacity‑aware retry with a single flag. This keeps retrying on 429/5xx/timeouts until the model is available, honoring Retry‑After when present.

  Python (3 lines):
  ```python
  from scillm.extras.chutes_simple import chutes_chat_json
  r = chutes_chat_json(messages=[{"role":"user","content":'Return only {"ok":true} as JSON.'}], tenacious=True, max_wall_time_s=6*3600)
  print(r.choices[0].message.content)
  ```

  - Required env: `CHUTES_API_BASE`, `CHUTES_API_KEY`, `CHUTES_TEXT_MODEL` (your single vendor id)
  - Retry classes: 429/“capacity”, 503/502, timeouts, transient connection errors.
  - Not retried: 401/403, 404/unmapped, 400/422.
  - Env toggle (no code change): `SCILLM_TENACIOUS=1`

Automatic selection, fallbacks, and attribution (opt‑in)

- One‑liner Router from env (discovers, ranks by availability + utilization). Enable it explicitly for local triage by setting `SCILLM_AUTO_ROUTER=1`:
  ```python
  import os
  os.environ.setdefault("SCILLM_AUTO_ROUTER", "1")
  from scillm.extras import auto_router_from_env

  router = auto_router_from_env(kind="text", require_json=True)
  out = router.completion(
      model=router.model_list[0]["model_name"],
      messages=[{"role":"user","content":'Return only {"ok":true} as JSON.'}],
      response_format={"type":"json_object"},
      max_retries=3, retry_after=1, timeout=45,
  )
  print(out.choices[0].message.get("content",""))
  ```
- “Do not fail; attribute who served”:
  ```python
  from scillm.extras import infer_with_fallback
  resp, meta = infer_with_fallback(
      messages=[{"role":"user","content":'Return only {"ok":true} as JSON.'}],
      kind="text", require_json=True,
      max_retries=3, retry_after=1, timeout=45,
  )
  print(resp.choices[0].message.get('content',''))
  print(meta)  # includes served_model and route
  ```
- Pick a single “not‑near‑capacity” candidate:
  ```python
  from scillm.extras import find_best_chutes_model
  entry = find_best_chutes_model(kind="text", require_json=True, util_threshold=0.85)
  print(entry)
  ```
- Warm caches at process start (optional):
  ```python
  from scillm.extras import warm_chutes_caches
  warm_chutes_caches(router.model_list)
  ```

Environment (multiple chutes, no code edits)
- `CHUTES_API_BASE_1`, `CHUTES_API_KEY_1`, `CHUTES_API_BASE_2`, `CHUTES_API_KEY_2`, ...
- Optional per‑kind model pins: `CHUTES_TEXT_MODEL_1`, `CHUTES_VLM_MODEL_1`, `CHUTES_TOOLS_MODEL_1`
- Tuning (optional): `SCILLM_UTIL_TTL_S=45`, `SCILLM_UTIL_HI=0.85`, `SCILLM_UTIL_LO=0.50`, `SCILLM_UTIL_K=2`

Per‑host chute (opt‑in; uv deploy)
- Deploy from your template module (standard image; no Docker build required):
  ```bash
  uv run chutes build path/to/template.py:chute --wait
  yes | uv run chutes deploy path/to/template.py:chute --accept-fee
  ```
- Readiness gate: `GET https://<slug>.chutes.ai/v1/models == 200`.
- One‑shot doctor:
  ```bash
  PYTHONPATH=src:. CHUTES_API_KEY=... \
  python scripts/chutes_host_doctor.py --slug <slug> --model <MODEL_ID>
  # prints { overall_ok: true|false, readiness, single, batch }
  ```

Notes
- Some models return fenced JSON (```json ... ```); use `litellm.extras.json_utils.clean_json_string()` to normalize.
- Prefer the shared base for day‑to‑day batch jobs; use a per‑host chute when you need isolation/pinning.
- Streaming transport: to force httpx (and avoid aiohttp shutdown noise), set `SCILLM_FORCE_HTTPX_STREAM=1` before running smokes/notebooks.

Codex‑Agent Zero‑Guess Quickstart (no CodeWorld)
- Start sidecar: `docker compose -f local/docker/compose.agents.yml up -d codex-sidecar`
- One‑shot doctor: `python debug/codex_agent_doctor.py` → expect `doctor: ok`
- Minimal API (recommended):
  - `from scillm.extras.codex import chat`
  - `chat([{\"role\":\"user\",\"content\":\"ping\"}], model=\"gpt-5\", temperature=0, max_tokens=16)`
- Codex‑only Option A (generate N + judge; no CodeWorld):
  - `python debug/smoke_option_a_codex_only.py`
  - Or programmatic:
    - `from scillm.extras.multi_agents import answer_code_autogen_and_judge_codex_only`
    - `answer_code_autogen_and_judge_codex_only(items, n_variants=6, generator_model=\"gpt-5\", judge_model=\"codex-agent/gpt-5\")`
- scillm completion form (explicit provider):
  - `export LITELLM_ENABLE_CODEX_AGENT=1 CODEX_AGENT_API_BASE=http://127.0.0.1:8089`
  - `from scillm import completion`
  - `completion(model=\"gpt-5\", custom_llm_provider=\"codex-agent\", messages=[...], max_tokens=128)`
  - Or “only change model” UX (no provider arg):
    ```python
    from scillm import completion
    base = "http://127.0.0.1:8089"
    r = completion(
      model="codex-agent/gpt-5",  # no custom_llm_provider needed
      messages=[{"role":"user","content":"Return strict JSON {ok:true}"}],
      api_base=base,
      response_format={"type":"json_object"},
      temperature=1,
    )
    print(r.choices[0].message["content"])  # {"ok": true}
    ```
- Judge (parameter‑first, codex‑agent only):
  - Python:
    ```python
    from scillm import completion
    base = "http://127.0.0.1:8089"
    msgs=[
      {"role":"system","content":"Return STRICT JSON only: {best_id:string, rationale_short:string}."},
      {"role":"user","content":"A vs B; pick one and say why (short)."},
    ]
    resp = completion(
      model="gpt-5", custom_llm_provider="codex-agent", api_base=base,
      messages=msgs, response_format={"type":"json_object"},
      temperature=1, allowed_openai_params=["reasoning","reasoning_effort"], reasoning_effort="medium"
    )
    print(resp.choices[0].message["content"])  # strict JSON
    ```
  - Or with the minimal helper (no Router):
    ```python
    from scillm.extras.codex import chat
    res = chat(messages=msgs, model="gpt-5", base=base,
               response_format={"type":"json_object"}, temperature=1, reasoning_effort="medium")
    print(res["choices"][0]["message"]["content"])  # strict JSON
    ```

Important: a codex‑agent call is NOT a CodeWorld call. CodeWorld is only for strategy runs (e.g., MCTS). Keep them separate unless you explicitly enable CodeWorld autogen.
Extractor pipeline (HTTP client) env mapping (single place; reused later)

```bash
# If your client expects OpenAI envs, map them to codex‑agent
export OPENAI_BASE_URL="$CODEX_AGENT_API_BASE"     # do NOT append /v1
export OPENAI_API_KEY="${CODEX_AGENT_API_KEY:-none}"
```

### Codex‑Cloud (Deprecated / Disabled by default)

There is no public, stable Codex Cloud tasks API. To avoid confusion with normal
OpenAI‑compatible gateways, the `codex_cloud` helpers are disabled by default. Prefer
`codex-agent` or your existing gateway for best‑of‑N and judging. If you explicitly
opt in, set both `SCILLM_ENABLE_CODEX_CLOUD=1` and `SCILLM_EXPERIMENTAL_CODEX_CLOUD=1`
and proceed at your own risk.

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

### Chutes rate limiting (recommended defaults)

When targeting an OpenAI‑compatible gateway such as Chutes (sliding window ≈180 RPM; 120‑second cool‑down after any 429), pace calls to avoid bursts:

```bash
# Soft caps (process‑local) — keep below 180 RPM with headroom
export SCILLM_RATE_LIMIT_QPS=2.5         # ~150 RPM average
export SCILLM_COOLDOWN_429_S=120         # honor 120s cool‑down after a 429

# Runner‑side (suggested) — cap concurrency in your batch/async code
export SCILLM_MAX_CONCURRENCY=2          # keep simultaneous calls small
```

Notes:
- Model discovery: run `preflight_models()` once per session and enable `SCILLM_MODEL_PREFLIGHT=1` to avoid per‑call `/v1/models` hits.
- Caching: enable `initialize_litellm_cache()` to dedupe identical prompts on retries.
- 429 handling: capacity and rate‑limit 429s look the same from clients; the helper pacer inserts a global cool‑down window after a 429 if you enable it.

### Chutes: Router + Fallbacks (Recommended)

For Chutes’ OpenAI‑compatible `/v1` endpoints, standardize on Router with primary + alternates and enable sensible retry/backoff. This avoids capacity flaps (429/503) and header quirks.

- Environment
  - `export SCILLM_CHUTES_CANONICALIZE_OPENAI_AUTH=1`
  - `export LITELLM_MAX_RETRIES=3 LITELLM_RETRY_AFTER=2`
  - `export SCILLM_COOLDOWN_429_S=120 SCILLM_RATE_LIMIT_QPS=2`
  - Optional async stability: `export SCILLM_DISABLE_AIOHTTP=1 LITELLM_TIMEOUT=45`

- Text Router (primary first, then alternates)
  ```python
  import os
  from litellm import Router

  router_text = Router(model_list=[
    {"model_name": "chutes/text",
     "litellm_params": {"custom_llm_provider": "openai_like",
                         "model": os.environ["CHUTES_TEXT_MODEL"],
                         "api_base": os.environ["CHUTES_API_BASE"],
                         "api_key": os.environ["CHUTES_API_KEY"],
                         "order": 1}},
    {"model_name": "chutes/text",
     "litellm_params": {"custom_llm_provider": "openai_like",
                         "model": os.environ.get("CHUTES_TEXT_MODEL_ALT1", ""),
                         "api_base": os.environ["CHUTES_API_BASE"],
                         "api_key": os.environ["CHUTES_API_KEY"],
                         "order": 2}},
  ])

  out = router_text.completion(
    model="chutes/text",
    messages=[{"role":"user","content":"Return only {\"ok\": true} as JSON."}],
    response_format={"type":"json_object"},
  )
  print(out.choices[0].message.get("content",""))
  ```

- VLM Router (same pattern)
  ```python
  router_vlm = Router(model_list=[
    {"model_name": "chutes/vlm",
     "litellm_params": {"custom_llm_provider": "openai_like",
                         "model": os.environ.get("CHUTES_VLM_MODEL", ""),
                         "api_base": os.environ["CHUTES_API_BASE"],
                         "api_key": os.environ["CHUTES_API_KEY"],
                         "order": 1}},
    {"model_name": "chutes/vlm",
     "litellm_params": {"custom_llm_provider": "openai_like",
                         "model": os.environ.get("CHUTES_VLM_MODEL_ALT1", ""),
                         "api_base": os.environ["CHUTES_API_BASE"],
                         "api_key": os.environ["CHUTES_API_KEY"],
                         "order": 2}},
  ])

  out = router_vlm.completion(
    model="chutes/vlm",
    messages=[{"role":"user","content":[{"type":"text","text":"Return only {\"ok\": true} as JSON."}]}],
    response_format={"type":"json_object"},
  )
  print(out.choices[0].message.get("content",""))
  ```

Notes
- Alternates are additional deployments sharing the same `model_name`; `order` clarifies human intent. Router cools down a throttled deployment and tries the next automatically.
- Capacity responses (429/503/strings containing “capacity”) are treated as retryable; backoff uses `LITELLM_MAX_RETRIES`/`LITELLM_RETRY_AFTER`.
- Auth is canonicalized once per base; a 401 triggers a one‑time switch to `x-api-key` and is cached. No header drift at call sites.


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

## Bridge API (HTTP Fallback)

**Note**: The preferred approach is direct mode via `scillm.integrations.certainly`. HTTP bridge is only needed for deployments that cannot install the certainly package.

If HTTP bridge is needed, start it from the **lean4 repo** (not litellm):
```bash
cd /path/to/lean4
PYTHONPATH=src uvicorn lean4_prover.bridge.server:app --port 8787
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

## Verified Tool‑Calling Models (Chutes)

Tool‑calling support depends on the model and the gateway route. We only claim support for models we have live‑verified (tool_calls present) with exact endpoint, headers, and parameters.

- moonshotai/Kimi‑K2‑Instruct‑0905
  - Endpoint: `/v1/chat/completions`
  - Auth: `Authorization: Bearer <key>`
  - stream: `false`
  - tool_choice: `"auto"`
  - Verified: Oct 25, 2025 (ok_tools=true in `.artifacts/nb_advanced_streaming_tools.json`)

All other models are “unverified” until a live check passes. Use the probe in local/docs/01_guides/TOOLS_SUPPORT.md to submit a working example; we’ll add it to the table.

Notes
- Alternate endpoints: some gateways expose tool‑calling on `/v1/responses`. Set `CHUTES_TOOLS_ENDPOINT` and re‑run smokes to verify.
- Paved path recap: JSON = `x-api-key`; Streaming = `Authorization: Bearer`; Tools (Kimi‑K2) = non‑stream + Bearer + realistic function (e.g., `get_weather`).

The response mirrors the scenario JSON (summary, statistics, proof_results, stdout, stderr, duration).
Use `feature_recipes/lean4_bridge_client.py` to call the bridge directly.
For Router-style usage:

```bash
export LITELLM_ENABLE_LEAN4=1
export LEAN4_BRIDGE_BASE=http://127.0.0.1:8787
python scenarios/lean4_router_release.py
```

This mirrors the CodeWorld Router pattern for a consistent developer experience.

### Certainly / Lean4 (Two Modes)

**Direct Mode (Preferred)** — Install with `pip install scillm[certainly]`:

```python
from scillm.integrations.certainly import prove_requirement, is_available

if is_available():
    result = await prove_requirement(
        requirement="Prove that n + 0 = n",
        tactics=["simp"],
    )
    if result["ok"]:
        print(result["best"]["lean4"])
```

**Provider Mode** — Via LiteLLM completion API:

```bash
export LITELLM_ENABLE_CERTAINLY=1
export CERTAINLY_BRIDGE_BASE=http://127.0.0.1:8787  # only needed if certainly not installed
python scenarios/certainly_adapter_demo.py
```

Results attach under `additional_kwargs['certainly']` (optionally mirrored to `['lean4']` while migrating).

When `certainly` package is installed, the provider automatically uses direct mode (no HTTP overhead).

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


### Autogen + Judge (one‑shot helper)

```python
from scillm.extras import ensure_codex_agent
from scillm.extras.multi_agents import answer_code_mcts_autogen_and_judge
import os

ensure_codex_agent()  # uses CODEX_AGENT_API_BASE or starts sidecar
items=[{"task":"six improved fast inverse square root for gaming (C/C++)","context":{}}]
res = answer_code_mcts_autogen_and_judge(
    items,
    n_variants=6, rollouts=12, depth=4, uct_c=1.3, temperature=0.0,
    codeworld_base=os.getenv("CODEWORLD_BASE","http://127.0.0.1:8888"),
    judge_model="gpt-5", timeout=120.0)
print(res["codeworld"]["results"][0]["mcts"]["best_value"])  # float
print(res["judge"])  # {'best_id': 'variant_5', 'rationale_short': '...'}
```

Environment hints
- `SCILLM_ENABLE_CODEWORLD=1` registers the provider early.
- `CODEX_AGENT_API_BASE=http://127.0.0.1:8089` points to the codex-agent sidecar (no `/v1`).
- `CODEWORLD_AUTOGEN_HTTP_TIMEOUT_S=120` can help slower generators.

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

<a id="scenario-auto-code-review-green-codex-agent"></a>
## Scenario: Auto Code -> Review -> Green (codex-agent)

Use this when the goal is “make the code run” in a single repository: generate code, run your gate (tests/build), optionally request a review, and iterate until green.

Why codex‑agent (vs. CodeWorld)
- codex‑agent keeps the loop repo‑local with small diffs and a single success criterion.
- CodeWorld is for multi‑step scenario orchestration across tools/repos; use it when you need MCTS/strategy search or auto‑release pipelines.

Prerequisites
- A “green gate” shell command that proves success:
  - Examples: `pytest -q`, `npm test`, or `make build && ./bin/app --help`.
- A clean working tree on a feature branch.
- `CODEX_AGENT_API_BASE` (no `/v1`) and a valid model id from `$CODEX_AGENT_API_BASE/v1/models`.

Option A — Human‑guided loop (robust, no extra tooling)
```bash
# 0) Define your gate
GATE="pytest -q"    # replace with your real gate

# 1) Run the gate; capture failure
$GATE > /tmp/gate.log 2>&1 || true

# 2) Ask codex‑agent for a minimal unified diff that fixes the failure
MODEL_ID=$(curl -sS "$CODEX_AGENT_API_BASE/v1/models" | jq -r '.data[0].id')
PROMPT=$(cat <<'EOF'
You are an engineer. Read the failing gate log and propose a minimal unified diff
that fixes the failure. Keep diffs surgical and in‑style. No commentary, only a unified diff.
EOF
)

curl -sS "$CODEX_AGENT_API_BASE/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL_ID\",\"messages\":[{\"role\":\"user\",\"content\":\"$PROMPT\n\n==== gate.log ====\n$(sed 's/\\/\\\\/g' /tmp/gate.log)\"}]}" \
  | jq -r '.choices[0].message.content' > /tmp/patch.diff

# 3) Review & apply the patch; re‑run the gate
git apply --index /tmp/patch.diff && git commit -m "agent: minimal fix" || true
$GATE && echo "GREEN" || echo "Still failing; repeat"
```

Notes
- Works everywhere; you stay in control of diffs and commits.
- Replace `pytest -q` with your real gate. Iterate until green.

Option B — Automated loop (experimental)
- If you already run the local mini‑agent/sidecar and have wired it to apply patches and execute a shell gate, you can automate the loop.
- Stop conditions: exit code 0 from the gate, or max iterations/wall time.
- Guardrails: small patch radius, ≤5 iterations, ≤45‑minute wall time.
- See provider doc: `docs/my-website/docs/providers/codex_agent.md`.

When to switch to CodeWorld
- You need multi‑step orchestration (e.g., generate N variants, evaluate, select best, publish artifact). See `scenarios/mcts_codeworld_demo.py`.

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
