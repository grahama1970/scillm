<p align="center">
 <!-- Use exported assets that exist in local/artifacts/logo/ -->
 <!-- Use PNG for GitHub rendering reliability -->
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="local/artifacts/logo/SciLLM_balanced.animated.dark.svg" />
   <img src="local/artifacts/logo/SciLLM_balanced.animated.light.svg" alt="SciLLM" width="140" />
 </picture>
  <br/>
 <img src="local/artifacts/logo/scillm_mark_64.png" alt="SciLLM Icon" width="44" />
  <br/>
  <em>Balanced wordmark (default) + icon (logo‑only). The favicon (.ico) should use the icon only, no text.</em>
 </p>
<h1 align="center">🔬 SciLLM — Scientific/Engineering fork of LiteLLM</h1>
<h4 align="center"><a href="https://docs.litellm.ai/docs/simple_proxy" target="_blank">Proxy Server (LLM Gateway)</a> | <a href="https://docs.litellm.ai/docs/hosted" target="_blank"> Hosted Proxy (Preview)</a> | <a href="https://docs.litellm.ai/docs/enterprise"target="_blank">Enterprise Tier</a></h4>

<p align="center">
  <a href="https://github.com/grahama1970/scillm/actions/workflows/nightly-parity-stress.yml"><img src="https://github.com/grahama1970/scillm/actions/workflows/nightly-parity-stress.yml/badge.svg" alt="SciLLM: Nightly Parity & Stress"></a>
  <a href="https://github.com/grahama1970/scillm/actions/workflows/weekly-streaming-stress.yml"><img src="https://github.com/grahama1970/scillm/actions/workflows/weekly-streaming-stress.yml/badge.svg" alt="SciLLM: Weekly Streaming Stress"></a>
  <a href="https://github.com/grahama1970/scillm/actions/workflows/manual-stress.yml"><img src="https://img.shields.io/badge/SciLLM%20Manual%20Stress-%E2%86%92-blue" alt="SciLLM: Manual Stress"></a>
</p>

<h4 align="center">
    <a href="https://pypi.org/project/litellm/" target="_blank">
        <img src="https://img.shields.io/pypi/v/litellm.svg" alt="PyPI Version">
    </a>
    <a href="https://www.ycombinator.com/companies/berriai">
        <img src="https://img.shields.io/badge/Y%20Combinator-W23-orange?style=flat-square" alt="Y Combinator W23">
    </a>
    <a href="https://wa.link/huol9n">
        <img src="https://img.shields.io/static/v1?label=Chat%20on&message=WhatsApp&color=success&logo=WhatsApp&style=flat-square" alt="Whatsapp">
    </a>
    <a href="https://discord.gg/wuPM9dRgDw">
        <img src="https://img.shields.io/static/v1?label=Chat%20on&message=Discord&color=blue&logo=Discord&style=flat-square" alt="Discord">
    </a>
    <a href="https://www.litellm.ai/support">
        <img src="https://img.shields.io/static/v1?label=Chat%20on&message=Slack&color=black&logo=Slack&style=flat-square" alt="Slack">
    </a>
</h4>

<p align="center"><i>API‑compatible with LiteLLM, with pragmatic additions scientists and engineers asked for: strict JSON and reproducibility, clear budgets/costs, real observability, and two simple surfaces (direct and a tiny budget gateway). Plus CodeWorld and Certainly for real research workflows.</i></p>

**What Is SciLLM**
- A practical fork of LiteLLM for scientific/engineering work. Keep OpenAI‑compatible ergonomics, add the reproducibility, budgets/costs, and observability researchers need for experiments, batches, and papers.

**Why This Fork (vs. LiteLLM)**
- Reproducible by default: strict JSON helpers, seeds, per‑run manifests.
- Budget/cost you can trust: normalized headers + budget JSON on every call; optional pay‑as‑you‑go guard.
- Ops‑ready dashboards: Prometheus + Grafana that work even when providers don’t expose usage.
- Research workflows built‑in: CodeWorld for strategy search; Certainly (Lean4) for proofs/diagnostics.

**Core Features (Why They Matter)**
- CodeWorld — strategy orchestration + MCTS
  - Safely run multiple algorithm/LLM strategies, measure with your metric, and select winners; add MCTS to explore large spaces systematically.
- Certainly — Lean4 proof bridge
  - Turn requirements/specs into Lean4 obligations; get proofs and human‑readable diagnostics you can version, rerun, and discuss.
- Chutes/OpenAI‑compatible calls — two paved paths
  - Direct: minimal friction, pure OpenAI‑compatible.
  - Budget Gateway Lite (stateless): normalized budget headers + budget JSON + /metrics without any database.
- Integrated Dashboard — Prometheus + Grafana
  - Ready panels for requests, 429s, latency, budget remaining, and cost (when configured); imports in one command.
- Deterministic JSON + reproducibility
  - Strict JSON helpers, seed controls, per‑run manifests/artifacts so your figures and tables are repeatable.
- Optional full proxy (central policy)
  - When you need multi‑provider routing, uniform retries, budgets, PAYG guard, and a single OpenAI‑compatible surface.

**Why It Fits Scientists & Engineers**
- Measurable (bring your metric), reproducible (seeds + manifests), governed (clear budget signals + optional hard stop), and observable (standard /metrics + working Grafana board).

**Which Agent Should I Use?**
- `codex-agent` — single‑repo "write code → run checks → iterate to green." Best for adding a feature, fixing tests, or hardening a build. Keeps diffs small and respects your repo style. See Quickstart: Auto Code → Review → Green.
- `codeworld` — orchestrates multi‑step, multi‑repo scenarios (e.g., strategy search/MCTS, long pipelines, auto‑release). Use when you need a scenario harness and artifacts across tools.

Minimal pick‑and‑go
- If your goal is "make this repo pass its gate": start with `codex-agent`.
- If your goal is "run a multi‑step experiment and compare strategies": use `codeworld`.

**Choose Your Path**
- Budget + dashboards (recommended for batches) → Budget Gateway Lite
- Minimal friction (first run) → Direct to provider

**60‑Second Quickstart**
- Budget Gateway Lite (recommended for batches)
  - `make budget-lite-build && METRICS_ENV=dev make budget-lite-run`
  - POST your OpenAI‑compatible payload to `http://127.0.0.1:4011/v1/chat/completions`.
  - Responses include normalized budget headers and `additional_kwargs.scillm.budget` when the gateway handles the call.
    - Headers: `x-ratelimit-limit`, `x-ratelimit-remaining-requests`, `x-budget-reset-at`.
    - 429s normalized to `{type:"budget_exhausted"}` and include `Retry-After` when provided by upstream (best‑effort).
  - Prometheus at `/metrics`; import `dashboards/scillm_budget_lite_grafana.json`.
  - Budget status: `GET /v1/budget` returns `{limit, remaining, reset_at, price_per_call_usd}` (price may be 0.0 unless configured).
- Direct to provider (fastest)
  - Copy the provider snippet in `docs/scillm/QUICKSTART.md` and run.

### Budget headers & endpoint (what to expect)
- On 200 and 4xx responses the proxy injects:
  - `x-ratelimit-limit`, `x-ratelimit-remaining-requests`, and `x-budget-reset-at`.
  - When rate‑limited/exhausted, 429 is normalized to `{ "type": "budget_exhausted" }` and includes `Retry-After` when available.
- Minimal budget status endpoint:
  - `GET /v1/budget → { "limit", "remaining", "reset_at", "price_per_call_usd" }`.
- Availability: this endpoint and the normalized headers are present when the proxy/gateway is in the path. If `GET /v1/budget` returns 404, your client is bypassing the proxy; fall back to reading the budget headers from a normal chat response.
- Quick probe (local defaults):
  - `curl -sS http://127.0.0.1:4010/v1/budget | jq`
  - A successful JSON chat with `curl -i` will show the headers above.

<details><summary><b>What Each Piece Is (One‑liners)</b></summary>

- CodeWorld: strategy orchestrator with optional MCTS; run N variants, score, and pick winners.
- Certainly (Lean4): formal methods bridge; convert requirements to proofs/diagnostics.
- Chutes calls: OpenAI‑compatible JSON/streaming; use direct or via the budget gateway.
- Budget Gateway Lite: tiny FastAPI sidecar; adds budget headers/JSON and /metrics; no DB.
- Grafana dashboard: imports in one command; watch requests/429s/latency/budget/cost.
- Full proxy (optional): centralized routing/retry/policy when you outgrow the gateway.

</details>

**Animated logo note**
- The animated SVG referenced by this branch did not resolve reliably in the GitHub README viewer. PNG ensures consistent rendering. If you share the working SVG path, I’ll restore it with a PNG fallback here (and keep the animation on the docs site).

<p><b>Why SciLLM?</b> SciLLM adds specialized infrastructure for theorem proving (Lean4 / “Certainly”), code orchestration (CodeWorld + MCTS), and agent/tool experimentation (mini‑agent, codex‑agent) — while retaining LiteLLM API compatibility.</p>

<blockquote>
<b>Environment Prefix Preference</b><br/>
Use <code>SCILLM_</code> variables (e.g. <code>SCILLM_ENABLE_CODEX_AGENT=1</code>). Legacy <code>LITELLM_</code> aliases remain for backward compatibility.<br/>
<b>Model IDs</b>: Replace <code><MODEL_ID></code> placeholders with a real ID from <code>GET $CODEX_AGENT_API_BASE/v1/models</code> (older examples used <code>gpt-5</code> illustratively).
</blockquote>

## TL;DR (30 seconds)

```bash
# Bring up bridges + proxy + deps
docker compose -f deploy/docker/compose.scillm.stack.yml up --build -d

# Two live scenarios (skip‑friendly)
python scenarios/codeworld_judge_live.py
LITELLM_ENABLE_CERTAINLY=1 CERTAINLY_BRIDGE_BASE=http://127.0.0.1:8787 \
  python scenarios/certainly_router_release.py
```

### Happy Path for Project Agents (codex‑agent)
- Zero‑ambiguity checklist (copy/paste)
  - Choose ONE runtime:
    - Local: `uvicorn litellm.experimental_mcp_client.mini_agent.agent_proxy:app --host 127.0.0.1 --port 8788`
    - Docker: `docker compose -f local/docker/compose.agents.yml up --build -d codex-sidecar`
  - Set base (no `/v1`): `export CODEX_AGENT_API_BASE=http://127.0.0.1:8788` (or `:8077` for Docker)
  - Map OpenAI envs (for HTTP clients):
    - `export OPENAI_BASE_URL="$CODEX_AGENT_API_BASE"`
    - `export OPENAI_API_KEY="${CODEX_AGENT_API_KEY:-none}"`
  - Discover a model id: `curl -sS "$CODEX_AGENT_API_BASE/v1/models" | jq -r '.data[].id'`
  - Quick HTTP (high reasoning):
    `curl -sS "$CODEX_AGENT_API_BASE/v1/chat/completions" -H 'Content-Type: application/json' -d '{"model":"gpt-5","reasoning":{"effort":"high"},"messages":[{"role":"user","content":"ping"}]}' | jq -r '.choices[0].message.content'`
- Router call: `completion(model="<MODEL_ID>", custom_llm_provider="codex-agent", api_base=$CODEX_AGENT_API_BASE, messages=[...], reasoning_effort="high")`
- Model-only UX: `completion(model="codex-agent/gpt-5", api_base=$CODEX_AGENT_API_BASE, messages=[...], response_format={"type":"json_object"}, temperature=1)`
- Optional cache: `from litellm.extras import initialize_litellm_cache; initialize_litellm_cache()`

MCTS (CodeWorld) one‑POST live check
- Start bridge: `PYTHONPATH=src uvicorn codeworld.bridge.server:app --port 8888 --log-level warning`
- One‑POST autogen+MCTS: `CODEWORLD_BASE=http://127.0.0.1:8888 curl -sS "$CODEWORLD_BASE/bridge/complete" -H 'Content-Type: application/json' -d '{"messages":[{"role":"user","content":"Autogenerate then search"}],"items":[{"task":"t","context":{}}],"provider":{"name":"codeworld","args":{"strategy":"mcts","strategy_config":{"autogenerate":{"enabled":true,"n":3},"rollouts":24,"depth":6,"uct_c":1.25}}}}' | jq '.run_manifest.mcts_stats'`
- Or just run: `CODEWORLD_BASE=http://127.0.0.1:8888 make mcts-live`

Troubleshooting
- 404 → wrong model id; use one from `/v1/models`.
- 400/502 (sidecar) → upstream provider not wired; enable echo or add credentials.
- Base includes `/v1` → remove it; use the base only.

Doctor (one-shot): `make codex-agent-doctor`

---
## Quick Links
| Topic | File |
|-------|------|
| Feature matrix & patterns | [docs/scillm/FEATURES.md](docs/scillm/FEATURES.md) |
| Multi‑Surface Quickstart | [docs/scillm/QUICKSTART.md](docs/scillm/QUICKSTART.md) |
| Auto Code → Review → Green (codex‑agent) | [docs/scillm/QUICKSTART.md](docs/scillm/QUICKSTART.md#scenario-auto-code-review-green-codex-agent) |
| Lean4 / Certainly specifics | `scenarios/lean4_*`, `scenarios/certainly_*` |
| Parallel fan‑out example | `feature_recipes/parallel_acompletions.py` |
| MCTS & autogen | `scenarios/mcts_codeworld_demo.py` |
| Retry guide | `docs/guide/RATE_LIMIT_RETRIES.md` |
| Batch helpers | `docs/guide/batch_helpers.md` |

## Verification (2 commands)
- Start proxy with demo pricing so cost metrics move:
  - `make proxy-run-uv-demo-pricing`
- Run local smokes (headers, /v1/budget, demo pricing):
  - `make smokes`
  - Expect: ok:true for each JSON report; `sc_cost_usd_total` delta > 0 in demo pricing.

## Security & Isolation (Central Reference)
| Area | Guidance |
|------|----------|
| codex‑agent echo mode | Development only (<code>CODEX_SIDECAR_ECHO=1</code>). Disable before real credentials. |
| CodeWorld sandbox | Process RLIMITs + optional network namespace; containerize for production isolation. |
| Mini‑Agent images | Detected but not processed (text‑only semantics). |
| Mini‑Agent traces | Enable with <code>MINI_AGENT_STORE_TRACES=1</code> + <code>MINI_AGENT_STORE_PATH</code> to capture JSONL transcripts. |
| Output trust | Treat LLM outputs as untrusted; verify via proofs, scoring, deterministic tests. |
| Parallel fan‑out | Prefer ordered <code>parallel_acompletions</code>; <code>parallel_as_completed</code> is experimental/unordered. |

---

## codex‑agent (OpenAI‑compatible) — Local or Docker

Use either local (mini‑agent) or Docker (sidecar). Both expose the same API; do NOT append `/v1` to the base.

- Start shim (default 127.0.0.1:8788):
  - `uvicorn litellm.experimental_mcp_client.mini_agent.agent_proxy:app --host 127.0.0.1 --port 8788`
- Env (set *before* importing Router):
  - `export SCILLM_ENABLE_CODEX_AGENT=1` (alias: `LITELLM_ENABLE_CODEX_AGENT=1`)
  - `export CODEX_AGENT_API_BASE=http://127.0.0.1:8788`  # no `/v1`
  - `# export CODEX_AGENT_API_KEY=...` (usually unset for local)
- Verify:
  - `curl -sSf http://127.0.0.1:8788/healthz`
  - `curl -sS  http://127.0.0.1:8788/v1/models | jq .`
  - (Optional) High reasoning chat:
    ```bash
    MODEL_ID=$(curl -sS $CODEX_AGENT_API_BASE/v1/models | jq -r '.data[0].id')
    curl -sS -H 'content-type: application/json' \
      -d "{\"model\":\"$MODEL_ID\",\"reasoning\":{\"effort\":\"high\"},\"messages\":[{\"role\":\"user\",\"content\":\"say hello\"}]}" \
      $CODEX_AGENT_API_BASE/v1/chat/completions | jq -r '.choices[0].message.content'
    ```
- Router usage (strict JSON):
  ```python
  from litellm import Router
  import os
  router = Router()
  MODEL_ID = os.getenv("CODEX_MODEL_ID","<MODEL_ID>")
  out = router.completion(
      model=MODEL_ID,
      custom_llm_provider="codex-agent",
      messages=[{"role":"user","content":"Return STRICT JSON only: {\"ok\":true}"}],
      response_format={"type":"json_object"},
      retry_enabled=True,
      honor_retry_after=True
  )
  print(out.choices[0].message["content"])  # JSON
  ```

Docker option: `docker compose -f local/docker/compose.agents.yml up --build -d` exposes mini‑agent on `127.0.0.1:8788` and the codex sidecar on `127.0.0.1:8077`. Point `CODEX_AGENT_API_BASE` at the one you want (no `/v1`).

codex‑agent base rule and endpoints
- Set `CODEX_AGENT_API_BASE` WITHOUT `/v1`; the provider appends `/v1/chat/completions`.
- Sidecar (8077) and mini‑agent (8788) expose:
  - `GET /healthz`, `GET /v1/models` (stub), `POST /v1/chat/completions` (content is always a string).

Auth for sidecar (non‑echo mode)
- The compose file enables echo by default (`CODEX_SIDECAR_ECHO=1`). To use real credentials, disable echo and mount your auth file:
  - Edit `local/docker/compose.agents.yml` codex‑sidecar service and remove `CODEX_SIDECAR_ECHO` or set to `"0"`.
  - Mount your credentials: `- ${HOME}/.codex/auth.json:/root/.codex/auth.json:ro`.
  - Verify: `python debug/check_codex_auth.py --container litellm-codex-agent` (exit 0 means present).

Debug probes (copy/paste)
- Mini‑agent: `python debug/verify_mini_agent.py` (use `--local` to spawn a local uvicorn on 8789)
- Codex sidecar: `python debug/verify_codex_agent_docker.py` (adds `--start` to compose‑up)
- Parallel Router → codex‑agent echo: `python debug/codex_parallel_probe.py` (prints `content` and `scillm_router`)

Optional Router mapping for judge
```python
from litellm import Router
r = Router(model_list=[{"model_name":"gpt-5","litellm_params":{"model":"gpt-5","custom_llm_provider":"codex-agent","api_base":os.getenv("CODEX_AGENT_API_BASE"),"api_key":os.getenv("CODEX_AGENT_API_KEY")}}])
```

### Judge (parameter‑first; completion and helper forms)

- Completion (no envs required besides base):
  ```python
  from scillm import completion
  base = "http://127.0.0.1:8089"
  msgs=[
    {"role":"system","content":"Return STRICT JSON only: {best_id:string, rationale_short:string}."},
    {"role":"user","content":"A vs B — pick one and say why (short)."},
  ]
  r = completion(model="gpt-5", custom_llm_provider="codex-agent", api_base=base,
                 messages=msgs, response_format={"type":"json_object"},
                 temperature=1, allowed_openai_params=["reasoning","reasoning_effort"], reasoning_effort="medium")
  print(r.choices[0].message["content"])  # strict JSON
  ```

- Minimal helper (direct HTTP, no Router):
  ```python
  from scillm.extras.codex import chat
  res = chat(messages=msgs, model="gpt-5", base=base,
             response_format={"type":"json_object"}, temperature=1, reasoning_effort="medium")
  print(res["choices"][0]["message"]["content"])  # strict JSON
  ```

### Codex‑Cloud (Experimental)

Use codex-ts-sdk to run best‑of‑N remote code tasks as an alternative to local codex‑agent:

- One‑time install: `cd scillm/extras/js && npm install`
- Enable + run smoke:
  ```bash
  export SCILLM_EXPERIMENTAL_CODEX_CLOUD=1
  export CODEX_CLOUD_API_KEY=...   # or OPENAI_API_KEY
  # Optional: export CODEX_CLOUD_BASE_URL=... CODEX_CLOUD_ENV=prod
  python debug/live_best_of_n_and_judge.py
  ```
- Notes:
  - Produces a simple variants dict with a diff (PoC). We can expand to per‑attempt variants.
  - Chat/completions for `codex-cloud` are intentionally not implemented yet; use helpers only.

### Retry metadata (429 backoff)
Set:
```
SCILLM_RETRY_META=1
SCILLM_LOG_JSON=1
```
Result snippet:
```json
"additional_kwargs": {
  "router": {
    "retries": {
      "attempts": 2,
      "total_sleep_s": 5.4,
      "last_retry_after_s": 3.0
    }
  }
}
```

## Rate Limiting & Retries (429)

### Chutes mode (QPS pacing)
If you target an OpenAI‑compatible gateway with ~180 RPM limits (e.g., Chutes), you can enable gentle, process‑local pacing:

```bash
export SCILLM_RATE_LIMIT_QPS=2.5   # ~150 RPM average
export SCILLM_COOLDOWN_429_S=120   # cool‑down window after a 429
```

Pair this with a low runner concurrency (e.g., MAX_WORKERS=2) to avoid bursts.


For long unattended runs that encounter provider 429s, SciLLM’s Router supports opt‑in retrier logic (Retry‑After awareness, exponential jitter backoff, budgets, callbacks). See docs/guide/RATE_LIMIT_RETRIES.md for env and per‑call examples.

## Warm‑ups in CI (Chutes/Runpod)

Some OpenAI‑compatible providers benefit from a short daily warm‑up to avoid first‑token latency spikes. This fork ships skip‑friendly warm‑up scripts and a strict composite gate you can opt into. To enable strict warm‑ups in GitHub Actions, add a job step like:

```yaml
jobs:
  readiness-live:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - name: Install deps
        run: pip install -U -r requirements.txt
      - name: Live readiness (strict warm-ups)
        env:
          READINESS_LIVE: '1'
          STRICT_READY: '1'
          STRICT_WARMUPS: '1'
          CHUTES_API_KEY: ${{ secrets.CHUTES_API_KEY }}
          RUNPOD_API_KEY: ${{ secrets.RUNPOD_API_KEY }}
        run: |
          make project-ready-live
```

- Strict gate details are defined in `readiness.yml` as `chutes_warmup_strict`, `runpod_warmup_strict`, and `warmups_strict_all`. For a quick manual probe, you can also run:

```bash
python scenarios/provider_warmup_probe.py --provider chutes --model "$LITELLM_DEFAULT_MODEL"
python scenarios/provider_warmup_probe.py --provider runpod --model "$LITELLM_DEFAULT_MODEL"
```

If `STRICT_WARMUPS` is not set, warm‑ups remain optional and will not fail the job.

## Why SciLLM

SciLLM exists as an experimental playground for scientists, mathematicians, and engineers who need a reproducible, inspectable way to combine LLMs, program synthesis/evaluation, and theorem proving.

- Who it’s for
  - Researchers building proof‑of‑concepts around formal methods (Lean4 today), code scoring/ranking, and agent loops.
  - Engineers who want an OpenAI‑compatible surface with a local/free stack (Docker) and strong “one way to green” readiness gates.
  - Educators and tinkerers who prefer runnable scenarios over slideware.

- What it gives you
  - Any LLM model that LiteLLM supports: keep your familiar OpenAI‑style interface and plug in local/cloud models as needed.
  - Certainly (Lean4 umbrella, beta): take natural language + structured requirements and verify them under Lean4; returns proofs or structured guidance/diagnostics when a requirement doesn’t compile/prove.
  - CodeWorld: run multiple concurrent algorithmic approaches safely; apply custom, dynamic scoring; rank winners with a built‑in judge.
  - codex‑agent: code‑centric agent surface (OpenAI‑compatible) that can run multi‑iteration plans and call MCP tools via your own sidecar/gateway.
  - mini‑agent: a small, deterministic local agent for quick tool‑use experiments (Python/Rust/Go/JS profiles).
  - Reproducibility by design: deterministic tests (offline) vs live scenarios (skip‑friendly), strict readiness gates, per‑run artifacts (run_id, request_id, item_ids, session/track).
  - Observability basics: per‑request IDs, minimal `/metrics`, machine‑readable manifests for replay.

- What it is not (yet)
  - A hardened production prover/execution sandbox. CodeWorld uses process‑level isolation (RLIMITs + optional `unshare -n`) on Linux; containerized workers are recommended for GA.
  - A drop‑in replacement for fully featured theorem proving platforms—this is a lightweight bridge for experiments.

Design philosophy
- One way to green: a single readiness flow with strict/live gates for deploy checks.
- Deterministic vs live separation: everything in `tests/` is offline; everything in `scenarios/` can touch the world.
- Local‑first: the whole stack can run on your laptop with Docker; cloud providers are optional.

<!-- Removed duplicated TL;DR block to reduce ambiguity; canonical TL;DR lives near top. -->

### What you can do in 5 minutes
- Prove‑aware pipeline: `scenarios/certainly_router_release.py`
- Strategy benchmark: `scenarios/codeworld_judge_live.py`
- Agent loop experiment: mini‑agent or codex‑agent via Router with strict JSON + retries

## Certainly (Lean4 umbrella, beta)

Certainly is the umbrella surface for theorem provers; today it routes to Lean4 only.

- Enable provider: `LITELLM_ENABLE_LEAN4=1` (or `LITELLM_ENABLE_CERTAINLY=1`)
- Bridge: `LEAN4_BRIDGE_BASE` (or `CERTAINLY_BRIDGE_BASE`) defaults to `http://127.0.0.1:8787`
  - Router: use `custom_llm_provider="lean4"` or the alias `"certainly"`
  - Scenarios: `scenarios/lean4_*` and `scenarios/certainly_*` demonstrate both paths

Future backends (e.g., Coq) will plug into the same surface, but are out of scope for this alpha.

<details>
  <summary>Logo variants</summary>
  <p>
    <img src="local/artifacts/logo/SciLLM_balanced.light@2x.png" alt="SciLLM Balanced (PNG)" height="36" />
    &nbsp;&nbsp;
    <img src="local/artifacts/logo/SciLLM_balanced.light@2x.png" alt="SciLLM Balanced (light, PNG)" height="36" />
    &nbsp;&nbsp;
    <img src="local/artifacts/logo/scillm_mark_64.png" alt="SciLLM Icon (PNG)" height="36" />
    &nbsp;&nbsp;
    <img src="local/artifacts/logo/SciLLM_balanced.dark@2x.png" alt="SciLLM Balanced (dark, PNG)" height="36" />
    &nbsp;&nbsp;
    <img src="local/artifacts/logo/scillm_mark_dark_64.png" alt="SciLLM Icon (dark, PNG)" height="36" />
  </p>
  <p>Use <code>make logo-export</code> to produce outlined SVGs and favicons in <code>local/artifacts/logo/</code>. The generated <code>favicon.ico</code> uses the icon only (no text).</p>
</details>

## Scenarios vs Tests (Determinism Boundary)

- `tests/` are strictly deterministic and offline (no network). Example: Lean4 CLI contract tests in `tests/lean4/`.
- `scenarios/` are live end-to-end demos that may call HTTP bridges or external services. They are skip-friendly when deps aren’t running.
- New: `scenarios/mcts_codeworld_auto_release.py` prefers a single POST to `/bridge/complete` with `strategy_config.autogenerate`; falls back to a two‑step flow if generation isn’t available.

## mini‑agent & codex‑agent (One‑liners)

- mini‑agent (local tools, deterministic): fast, reproducible tool‑use loop for experiments. Expect final answer + metrics + parsed tool calls.
- codex‑agent (code‑centric provider): OpenAI‑compatible sidecar with MCP tools and multi‑iteration plans; health‑checkable and Router‑native.

Mini‑Agent (MCP)
- Start the MCP‑style mini‑agent locally:
  - `uvicorn litellm.experimental_mcp_client.mini_agent.agent_proxy:app --host 127.0.0.1 --port 8788`
- Probe: `curl -sSf http://127.0.0.1:8788/ready`
- In‑process sample: `python examples/mini_agent_inprocess.py` (uses LocalMCPInvoker)
- See also: feature_recipes/MINI_AGENT.md and docs/scillm/CONTEXT.md runbook pointers.

## When To Use CodeWorld

Use CodeWorld when you want to evaluate and rank code strategies under your own metrics, with a reproducible manifest and simple HTTP/Router calls.

- MCTS quick calls
  - Alias sugar: `model="codeworld/mcts"` injects `strategy="mcts"`.
  - Autogenerate + MCTS: `model="codeworld/mcts:auto"` (synonym `mcts+auto`) to generate N approaches, then run MCTS. Env overrides: `CODEWORLD_MCTS_AUTO_N`, `CODEWORLD_MCTS_AUTO_TEMPERATURE`, `CODEWORLD_MCTS_AUTO_MODEL`, `CODEWORLD_MCTS_AUTO_MAX_TOKENS`.
  - See [MCTS_CODEWORLD.md](feature_recipes/MCTS_CODEWORLD.md) for details and determinism notes.

- Typical problems
  - Compare competing algorithms (e.g., heuristics vs DP) with identical inputs.
  - Validate repair loops (generate → run → score → keep best) using a deterministic judge.
  - Track improvement plateaus during optimization (optional Redis‑backed session history).
- What you get
  - Sandbox runner (alpha): executes Python strategies with RLIMITs + AST allow/deny; optional no‑net namespace on Linux.
  - Dynamic scoring: inject `score(task, context, outputs, timings)` to compute domain‑specific metrics.
  - Judge ranking: built‑in weighted or lexicographic ranking across correctness/speed/brevity.
  - Artifacts: run_manifest with run_id, item_ids, options (session/track), and request_id.
- Try it quickly
  - Bridge: `CODEWORLD_BASE=http://127.0.0.1:8887 python scenarios/codeworld_bridge_release.py`
  - Judge demo (shows speed effect): `python scenarios/codeworld_judge_live.py`

## When To Use Certainly (Lean4)

Use Certainly when you need a light, HTTP‑friendly bridge to a theorem prover inside LLM/agent workflows.

- Typical problems
  - Batch‑check a set of obligations (lemmas) produced by an agent or pipeline.
  - Capture provenance (session/track/run_id) for reproducibility and audit.
  - Keep client code stable while changing the proving backend (Lean4 today).
- What you get
  - Lean4 bridge with canonical `{messages, items, options}` envelope; back‑compat `lean4_requirements`.
  - Router provider + alias (`custom_llm_provider="lean4"` or `"certainly"`).
  - Artifacts: run_manifest with run_id, item_ids, options (session/track), provider info, and request_id.
- Try it quickly
  - Bridge: `LEAN4_BRIDGE_BASE=http://127.0.0.1:8787 python scenarios/lean4_bridge_release.py`
  - Router alias: `LITELLM_ENABLE_CERTAINLY=1 CERTAINLY_BRIDGE_BASE=http://127.0.0.1:8787 python scenarios/certainly_router_release.py`

Lean4 examples:
- Deterministic tests: `tests/lean4/test_cli_batch.py`, `tests/lean4/test_cli_run.py`.
- Live scenarios: `scenarios/lean4_bridge_release.py`, `scenarios/lean4_bridge_eval_live.py`.
- Optional health test (env-guarded): `tests/lean4/test_bridge_health_optional.py` (runs only if `LEAN4_BRIDGE_BASE` is set).

LiteLLM manages:

## Rename & Compatibility Notice

This repository has been renamed to **scillm** and branded as **SciLLM — a scientific/engineering-focused fork of LiteLLM**.

- Core LiteLLM usage remains compatible; Lean4/CodeWorld are optional modules.
- Preferred env flags: `SCILLM_ENABLE_*` (aliases: `LITELLM_ENABLE_*`).
- CLI aliases: `scillm`, `scillm-proxy` (equivalent to litellm commands).
- Deployment profiles: see `docs/deploy/SCILLM_DEPLOY.md` and `deploy/docker/compose.scillm.*.yml`.

- Translate inputs to provider's `completion`, `embedding`, and `image_generation` endpoints
- [Consistent output](https://docs.litellm.ai/docs/completion/output), text responses will always be available at `['choices'][0]['message']['content']`
- Retry/fallback logic across multiple deployments (e.g. Azure/OpenAI) - [Router](https://docs.litellm.ai/docs/routing)
- Set Budgets & Rate limits per project, api key, model [LiteLLM Proxy Server (LLM Gateway)](https://docs.litellm.ai/docs/simple_proxy)

## Budget & Cost Visibility (Chutes/OpenAI‑compatible)

- Consistent headers on success and errors:
  - `x-ratelimit-limit`, `x-ratelimit-remaining-requests`, `x-budget-reset-at`.
  - PAYG extras when enabled: `x-payg-active`, `x-spend-usd-today`.
- Error normalization when daily calls are exhausted:
  - HTTP 429 with `{ "type": "budget_exhausted", "retry_at": <iso8601>, "remaining": 0 }`.
- Lightweight endpoint for dashboards/ops:
  - `GET /v1/budget` → `{ limit, remaining, reset_at, price_per_call_usd }`.
- Optional guards and behaviors (env):
  - `CHUTES_PREFLIGHT=1` to probe usage at startup; fallback limit via `CHUTES_DAILY_LIMIT=5000`.
  - `SCILLM_TENACIOUS=1` and `SCILLM_TENACIOUS_UNTIL_RESET=1` to auto‑wait until `reset_at`.
  - `SCILLM_DO_NOT_CHARGE=1` to hard‑stop at 429 even if PAYG is available.

Quick check (proxy at :4010):
```bash
curl -s http://127.0.0.1:4010/v1/budget | jq
```

Metrics + dashboards (Prometheus + Grafana):
- Start Prometheus: `make prom-run-docker` (scrapes proxy `/metrics`).
- Start proxy: `make proxy-run-uv` (uses `local/proxy_server_config.yaml`).
- Import dashboards: `GRAFANA_URL=http://127.0.0.1:3000 GRAFANA_TOKEN=$GRAFANA_SCILLM_SERVICE_TOKEN make grafana-import`.
  - Alternatively, enable anonymous view in local provisioning (already configured in `local/grafana/provisioning/`).

[**Jump to LiteLLM Proxy (LLM Gateway) Docs**](https://github.com/BerriAI/litellm?tab=readme-ov-file#openai-proxy---docs) <br>
[**Jump to Supported LLM Providers**](https://github.com/BerriAI/litellm?tab=readme-ov-file#supported-providers-docs)

Fork Status (our fork)
- See `docs/archive/STATE_OF_PROJECT.md` for a concise, operator‑friendly status of this fork, including the router_core seam (opt‑in), extras, mini‑agent helper, validation steps, CI, and roadmap.

Fork Quick Start (Recap)
1. `make run-scenarios`
2. (Optional) `docker compose -f local/docker/compose.agents.yml up --build -d` for mini + codex endpoints
3. See [docs/scillm/QUICKSTART.md](docs/scillm/QUICKSTART.md) for scenario commands
4. Mini‑Agent docs: `docs/my-website/docs/experimental/mini-agent.md`
5. Project status: `docs/archive/STATE_OF_PROJECT.md`


🚨 **Stable Release:** Use docker images with the `-stable` tag. These have undergone 12 hour load tests, before being published. [More information about the release cycle here](https://docs.litellm.ai/docs/proxy/release_cycle)

Support for more providers. Missing a provider or LLM Platform, raise a [feature request](https://github.com/BerriAI/litellm/issues/new?assignees=&labels=enhancement&projects=&template=feature_request.yml&title=%5BFeature%5D%3A+).

# Usage ([**Docs**](https://docs.litellm.ai/docs/))

> [!IMPORTANT]
> LiteLLM v1.0.0 now requires `openai>=1.0.0`. Migration guide [here](https://docs.litellm.ai/docs/migration)
> LiteLLM v1.40.14+ now requires `pydantic>=2.0.0`. No changes required.

<a target="_blank" href="https://colab.research.google.com/github/BerriAI/litellm/blob/main/cookbook/liteLLM_Getting_Started.ipynb">
  <img src="https://colab.research.google.com/assets/colab-badge.svg" alt="Open In Colab"/>
</a>

```shell
pip install litellm
```

```python
from litellm import completion
import os

## set ENV variables
os.environ["OPENAI_API_KEY"] = "your-openai-key"
os.environ["ANTHROPIC_API_KEY"] = "your-anthropic-key"

messages = [{ "content": "Hello, how are you?","role": "user"}]

# openai call
response = completion(model="openai/gpt-4o", messages=messages)

# anthropic call
response = completion(model="anthropic/claude-sonnet-4-20250514", messages=messages)
print(response)
```

### Response (OpenAI Format)

```json
{
    "id": "chatcmpl-1214900a-6cdd-4148-b663-b5e2f642b4de",
    "created": 1751494488,
    "model": "claude-sonnet-4-20250514",
    "object": "chat.completion",
    "system_fingerprint": null,
    "choices": [
        {
            "finish_reason": "stop",
            "index": 0,
            "message": {
                "content": "Hello! I'm doing well, thank you for asking. I'm here and ready to help with whatever you'd like to discuss or work on. How are you doing today?",
                "role": "assistant",
                "tool_calls": null,
                "function_call": null
            }
        }
    ],
    "usage": {
        "completion_tokens": 39,
        "prompt_tokens": 13,
        "total_tokens": 52,
        "completion_tokens_details": null,
        "prompt_tokens_details": {
            "audio_tokens": null,
            "cached_tokens": 0
        },
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0
    }
}
```

Call any model supported by a provider, with `model=<provider_name>/<model_name>`. There might be provider-specific details here, so refer to [provider docs for more information](https://docs.litellm.ai/docs/providers)

OpenAI‑compatible endpoints (e.g., Chutes) — model IDs
- Use vendor‑first IDs returned by `/v1/models` (e.g., `moonshotai/Kimi-K2-Instruct-0905`).
- You may pass `custom_llm_provider="openai"`, but scillm now defaults to 'openai'
  when `api_base` points at an OpenAI‑compatible gateway (e.g., `CHUTES_API_BASE`).
- Avoid `openai/` prefixes for model names; if present, they are stripped automatically
  for OpenAI‑compatible providers.


Strict JSON (auto sanitize)
- Set `SCILLM_JSON_SANITIZE=1` or pass `auto_json_sanitize=True` to normalize JSON-mode responses (removes fences/prose, validates JSON).
- Recommended with `response_format={"type":"json_object"}` or `response_mime_type="application/json"`.
Session preflight (optional)
- Cache model IDs once and enable guard to validate IDs locally for the session (no extra network per call).
```python
from litellm.extras.preflight import preflight_models
import os
preflight_models(api_base=os.environ["CHUTES_API_BASE"], api_key=os.environ.get("CHUTES_API_KEY"))
# export SCILLM_MODEL_PREFLIGHT=1
# Optional: map doc-style names to live canonical IDs
# export SCILLM_MODEL_ALIAS=1  # fail fast on unknown IDs using cached catalog
```

Python example (no prefix required):

With aliasing (default) and cutoff override:
```python
from scillm import completion; import os
print(completion(model='mistral-ai/Mistral-Small-3.2-24B', messages=[{'role':'user','content':'{}'}], response_format={'type':'json_object'}, api_base=os.environ['CHUTES_API_BASE'], api_key=os.environ.get('CHUTES_API_KEY',''), fallback_closest=True, fallback_closest_cutoff=0.58).choices[0].message.content)
```
```python
from scillm import completion; import os
r = completion(model='moonshotai/Kimi-K2-Instruct-0905',
              messages=[{'role':'user','content':'{}'}],
              response_format={'type':'json_object'},
              api_base=os.environ['CHUTES_API_BASE'],
              api_key=os.environ.get('CHUTES_API_KEY',''))
print(r.choices[0].message.content)
```

## Async ([Docs](https://docs.litellm.ai/docs/completion/stream#async-completion))

```python
from litellm import acompletion
import asyncio

async def test_get_response():
    user_message = "Hello, how are you?"
    messages = [{"content": user_message, "role": "user"}]
    response = await acompletion(model="openai/gpt-4o", messages=messages)
    return response

response = asyncio.run(test_get_response())
print(response)
```

## Streaming ([Docs](https://docs.litellm.ai/docs/completion/stream))

liteLLM supports streaming the model response back, pass `stream=True` to get a streaming iterator in response.
Streaming is supported for all models (Bedrock, Huggingface, TogetherAI, Azure, OpenAI, etc.)

```python
from litellm import completion
response = completion(model="openai/gpt-4o", messages=messages, stream=True)
for part in response:
    print(part.choices[0].delta.content or "")

# claude sonnet 4
response = completion('anthropic/claude-sonnet-4-20250514', messages, stream=True)
for part in response:
    print(part)
```

### Response chunk (OpenAI Format)

```json
{
    "id": "chatcmpl-fe575c37-5004-4926-ae5e-bfbc31f356ca",
    "created": 1751494808,
    "model": "claude-sonnet-4-20250514",
    "object": "chat.completion.chunk",
    "system_fingerprint": null,
    "choices": [
        {
            "finish_reason": null,
            "index": 0,
            "delta": {
                "provider_specific_fields": null,
                "content": "Hello",
                "role": "assistant",
                "function_call": null,
                "tool_calls": null,
                "audio": null
            },
            "logprobs": null
        }
    ],
    "provider_specific_fields": null,
    "stream_options": null,
    "citations": null
}
```

## Logging Observability ([Docs](https://docs.litellm.ai/docs/observability/callbacks))

LiteLLM exposes pre defined callbacks to send data to Lunary, MLflow, Langfuse, DynamoDB, s3 Buckets, Helicone, Promptlayer, Traceloop, Athina, Slack

```python
from litellm import completion

## set env variables for logging tools (when using MLflow, no API key set up is required)
os.environ["LUNARY_PUBLIC_KEY"] = "your-lunary-public-key"
os.environ["HELICONE_API_KEY"] = "your-helicone-auth-key"
os.environ["LANGFUSE_PUBLIC_KEY"] = ""
os.environ["LANGFUSE_SECRET_KEY"] = ""
os.environ["ATHINA_API_KEY"] = "your-athina-api-key"

os.environ["OPENAI_API_KEY"] = "your-openai-key"

# set callbacks
litellm.success_callback = ["lunary", "mlflow", "langfuse", "athina", "helicone"] # log input/output to lunary, langfuse, supabase, athina, helicone etc

#openai call
response = completion(model="openai/gpt-4o", messages=[{"role": "user", "content": "Hi 👋 - i'm openai"}])
```

# LiteLLM Proxy Server (LLM Gateway) - ([Docs](https://docs.litellm.ai/docs/simple_proxy))

Track spend + Load Balance across multiple projects

[Hosted Proxy (Preview)](https://docs.litellm.ai/docs/hosted)

The proxy provides:

1. [Hooks for auth](https://docs.litellm.ai/docs/proxy/virtual_keys#custom-auth)
2. [Hooks for logging](https://docs.litellm.ai/docs/proxy/logging#step-1---create-your-custom-litellm-callback-class)
3. [Cost tracking](https://docs.litellm.ai/docs/proxy/virtual_keys#tracking-spend)
4. [Rate Limiting](https://docs.litellm.ai/docs/proxy/users#set-rate-limits)

## 📖 Proxy Endpoints - [Swagger Docs](https://litellm-api.up.railway.app/)


## Quick Start Proxy - CLI

```shell
pip install 'litellm[proxy]'
```

### Step 1: Start litellm proxy

```shell
$ litellm --model huggingface/bigcode/starcoder

#INFO: Proxy running on http://0.0.0.0:4000
```

### Step 2: Make ChatCompletions Request to Proxy


> [!IMPORTANT]
> 💡 [Use LiteLLM Proxy with Langchain (Python, JS), OpenAI SDK (Python, JS) Anthropic SDK, Mistral SDK, LlamaIndex, Instructor, Curl](https://docs.litellm.ai/docs/proxy/user_keys)

```python
import openai # openai v1.0.0+
client = openai.OpenAI(api_key="anything",base_url="http://0.0.0.0:4000") # set proxy to base_url
# request sent to model set on litellm proxy, `litellm --model`
response = client.chat.completions.create(model="gpt-3.5-turbo", messages = [
    {
        "role": "user",
        "content": "this is a test request, write a short poem"
    }
])

print(response)
```

## Proxy Key Management ([Docs](https://docs.litellm.ai/docs/proxy/virtual_keys))

Connect the proxy with a Postgres DB to create proxy keys

```bash
# Get the code
git clone https://github.com/BerriAI/litellm

# Go to folder
cd litellm

# Add the master key - you can change this after setup
echo 'LITELLM_MASTER_KEY="sk-1234"' > .env

# Add the litellm salt key - you cannot change this after adding a model
# It is used to encrypt / decrypt your LLM API Key credentials
# We recommend - https://1password.com/password-generator/
# password generator to get a random hash for litellm salt key
echo 'LITELLM_SALT_KEY="sk-1234"' >> .env

source .env

# Start
docker-compose up
```


UI on `/ui` on your proxy server
![ui_3](https://github.com/BerriAI/litellm/assets/29436595/47c97d5e-b9be-4839-b28c-43d7f4f10033)

Set budgets and rate limits across multiple projects
`POST /key/generate`

### Request

```shell
curl 'http://0.0.0.0:4000/key/generate' \
--header 'Authorization: Bearer sk-1234' \
--header 'Content-Type: application/json' \
--data-raw '{"models": ["gpt-3.5-turbo", "gpt-4", "claude-2"], "duration": "20m","metadata": {"user": "ishaan@berri.ai", "team": "core-infra"}}'
```

### Expected Response

```shell
{
    "key": "sk-kdEXbIqZRwEeEiHwdg7sFA", # Bearer token
    "expires": "2023-11-19T01:38:25.838000+00:00" # datetime object
}
```

## Supported Providers ([Docs](https://docs.litellm.ai/docs/providers))

| Provider                                                                            | [Completion](https://docs.litellm.ai/docs/#basic-usage) | [Streaming](https://docs.litellm.ai/docs/completion/stream#streaming-responses) | [Async Completion](https://docs.litellm.ai/docs/completion/stream#async-completion) | [Async Streaming](https://docs.litellm.ai/docs/completion/stream#async-streaming) | [Async Embedding](https://docs.litellm.ai/docs/embedding/supported_embedding) | [Async Image Generation](https://docs.litellm.ai/docs/image_generation) |
|-------------------------------------------------------------------------------------|---------------------------------------------------------|---------------------------------------------------------------------------------|-------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------|-------------------------------------------------------------------------------|-------------------------------------------------------------------------|
| [openai](https://docs.litellm.ai/docs/providers/openai)                             | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 | ✅                                                                             | ✅                                                                       |
| [Meta - Llama API](https://docs.litellm.ai/docs/providers/meta_llama)                               | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                              |                                                                        |
| [azure](https://docs.litellm.ai/docs/providers/azure)                               | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 | ✅                                                                             | ✅                                                                       |
| [AI/ML API](https://docs.litellm.ai/docs/providers/aiml)                               | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 | ✅                                                                             | ✅                                                                       |
| [aws - sagemaker](https://docs.litellm.ai/docs/providers/aws_sagemaker)             | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 | ✅                                                                             |                                                                         |
| [aws - bedrock](https://docs.litellm.ai/docs/providers/bedrock)                     | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 | ✅                                                                             |                                                                         |
| [google - vertex_ai](https://docs.litellm.ai/docs/providers/vertex)                 | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 | ✅                                                                             | ✅                                                                       |
| [google - palm](https://docs.litellm.ai/docs/providers/palm)                        | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                               |                                                                         |
| [google AI Studio - gemini](https://docs.litellm.ai/docs/providers/gemini)          | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                               |                                                                         |
| [mistral ai api](https://docs.litellm.ai/docs/providers/mistral)                    | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 | ✅                                                                             |                                                                         |
| [cloudflare AI Workers](https://docs.litellm.ai/docs/providers/cloudflare_workers)  | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                               |                                                                         |
| [CompactifAI](https://docs.litellm.ai/docs/providers/compactifai)                   | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                               |                                                                         |
| [cohere](https://docs.litellm.ai/docs/providers/cohere)                             | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 | ✅                                                                             |                                                                         |
| [anthropic](https://docs.litellm.ai/docs/providers/anthropic)                       | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                               |                                                                         |
| [empower](https://docs.litellm.ai/docs/providers/empower)                    | ✅                                                      | ✅                                                                              | ✅                                                                                  | ✅                                                                                |
| [huggingface](https://docs.litellm.ai/docs/providers/huggingface)                   | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 | ✅                                                                             |                                                                         |
| [replicate](https://docs.litellm.ai/docs/providers/replicate)                       | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                               |                                                                         |
| [together_ai](https://docs.litellm.ai/docs/providers/togetherai)                    | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                               |                                                                         |
| [openrouter](https://docs.litellm.ai/docs/providers/openrouter)                     | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                               |                                                                         |
| [ai21](https://docs.litellm.ai/docs/providers/ai21)                                 | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                               |                                                                         |
| [baseten](https://docs.litellm.ai/docs/providers/baseten)                           | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                               |                                                                         |
| [vllm](https://docs.litellm.ai/docs/providers/vllm)                                 | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                               |                                                                         |
| [nlp_cloud](https://docs.litellm.ai/docs/providers/nlp_cloud)                       | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                               |                                                                         |
| [aleph alpha](https://docs.litellm.ai/docs/providers/aleph_alpha)                   | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                               |                                                                         |
| [petals](https://docs.litellm.ai/docs/providers/petals)                             | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                               |                                                                         |
| [ollama](https://docs.litellm.ai/docs/providers/ollama)                             | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 | ✅                                                                             |                                                                         |
| [deepinfra](https://docs.litellm.ai/docs/providers/deepinfra)                       | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                               |                                                                         |
| [perplexity-ai](https://docs.litellm.ai/docs/providers/perplexity)                  | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                               |                                                                         |
| [Groq AI](https://docs.litellm.ai/docs/providers/groq)                              | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                               |                                                                         |
| [Deepseek](https://docs.litellm.ai/docs/providers/deepseek)                         | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                               |                                                                         |
| [anyscale](https://docs.litellm.ai/docs/providers/anyscale)                         | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                               |                                                                         |
| [IBM - watsonx.ai](https://docs.litellm.ai/docs/providers/watsonx)                  | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 | ✅                                                                             |                                                                         |
| [voyage ai](https://docs.litellm.ai/docs/providers/voyage)                          |                                                         |                                                                                 |                                                                                     |                                                                                   | ✅                                                                             |                                                                         |
| [xinference [Xorbits Inference]](https://docs.litellm.ai/docs/providers/xinference) |                                                         |                                                                                 |                                                                                     |                                                                                   | ✅                                                                             |                                                                         |
| [FriendliAI](https://docs.litellm.ai/docs/providers/friendliai)                              | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                               |                                                                         |
| [Galadriel](https://docs.litellm.ai/docs/providers/galadriel)                              | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                               |                                                                         |
| [GradientAI](https://docs.litellm.ai/docs/providers/gradient_ai)                              | ✅                                                       | ✅                                                                               |                                                                                   |                                                                                  |                                                                               |                                                                         |
| [Novita AI](https://novita.ai/models/llm?utm_source=github_litellm&utm_medium=github_readme&utm_campaign=github_link)                     | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                               |                                                                         |
| [Featherless AI](https://docs.litellm.ai/docs/providers/featherless_ai)                              | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 |                                                                               |                                                                         |
| [Nebius AI Studio](https://docs.litellm.ai/docs/providers/nebius)                             | ✅                                                       | ✅                                                                               | ✅                                                                                   | ✅                                                                                 | ✅                                                                             |                                                                         |
| [Heroku](https://docs.litellm.ai/docs/providers/heroku)                             | ✅                                                       | ✅                                                                               |                                                                                    |                                                                                  |                                                                              |                                                                         |
| [OVHCloud AI Endpoints](https://docs.litellm.ai/docs/providers/ovhcloud)                             | ✅                                                       | ✅                                                                               |                                                                                    |                                                                                  |                                                                              |                                                                         |

[**Read the Docs**](https://docs.litellm.ai/docs/)

## Contributing

Interested in contributing? Contributions to LiteLLM Python SDK, Proxy Server, and LLM integrations are both accepted and highly encouraged!

**Quick start:** `git clone` → `make install-dev` → `make format` → `make lint` → `make test-unit`

See our comprehensive [Contributing Guide (CONTRIBUTING.md)](CONTRIBUTING.md) for detailed instructions.

# Enterprise
For companies that need better security, user management and professional support

[Talk to founders](https://calendly.com/d/4mp-gd3-k5k/litellm-1-1-onboarding-chat)

This covers:
- ✅ **Features under the [LiteLLM Commercial License](https://docs.litellm.ai/docs/proxy/enterprise):**
- ✅ **Feature Prioritization**
- ✅ **Custom Integrations**
- ✅ **Professional Support - Dedicated discord + slack**
- ✅ **Custom SLAs**
- ✅ **Secure access with Single Sign-On**

# Contributing

We welcome contributions to LiteLLM! Whether you're fixing bugs, adding features, or improving documentation, we appreciate your help.

## Quick Start for Contributors

This requires poetry to be installed.

```bash
git clone https://github.com/BerriAI/litellm.git
cd litellm
make install-dev    # Install development dependencies
make format         # Format your code
make lint           # Run all linting checks
make test-unit      # Run unit tests
make format-check   # Check formatting only
```

For detailed contributing guidelines, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Code Quality / Linting

LiteLLM follows the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html).

Our automated checks include:
- **Black** for code formatting
- **Ruff** for linting and code quality
- **MyPy** for type checking
- **Circular import detection**
- **Import safety checks**


All these checks must pass before your PR can be merged.


# Support / talk with founders

- [Schedule Demo 👋](https://calendly.com/d/4mp-gd3-k5k/berriai-1-1-onboarding-litellm-hosted-version)
- [Community Discord 💭](https://discord.gg/wuPM9dRgDw)
- [Community Slack 💭](https://www.litellm.ai/support)
- Our numbers 📞 +1 (770) 8783-106 / ‭+1 (412) 618-6238‬
- Our emails ✉️ ishaan@berri.ai / krrish@berri.ai

# Why did we build this

- **Need for simplicity**: Our code started to get extremely complicated managing & translating calls between Azure, OpenAI and Cohere.

# Contributors

<!-- ALL-CONTRIBUTORS-LIST:START - Do not remove or modify this section -->
<!-- prettier-ignore-start -->
<!-- markdownlint-disable -->

<!-- markdownlint-restore -->
<!-- prettier-ignore-end -->

<!-- ALL-CONTRIBUTORS-LIST:END -->

<a href="https://github.com/BerriAI/litellm/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=BerriAI/litellm" />
</a>


## Run in Developer mode
### Services
1. Setup .env file in root
2. Run dependant services `docker-compose up db prometheus`

### Backend
1. (In root) create virtual environment `python -m venv .venv`
2. Activate virtual environment `source .venv/bin/activate`
3. Install dependencies `pip install -e ".[all]"`
4. Start proxy backend `python3 /path/to/litellm/proxy_cli.py`

### Frontend
1. Navigate to `ui/litellm-dashboard`
2. Install dependencies `npm install`
3. Run `npm run dev` to start the dashboard


## Operations
- Canary runbook: local/docs/02_operational/CANARY_PARITY_PLAN.md
- Stress testing: local/docs/02_operational/STRESS_TESTING.md
- Parity scripts: local/scripts/router_core_parity.py, local/scripts/parity_summarize.py (use `uv run`)
- CI (live, secrets-gated): Nightly Parity & Stress, Weekly Streaming Stress, and Manual Stress workflows (see badges above).
## Reviews

See docs/reviews/ for current review briefs:
- docs/reviews/REVIEW_REQUEST_SCILLM.md
- docs/reviews/REVIEW_REQUEST_CERTAINLY.md

## Architecture (one picture)

See docs/architecture/overview.mmd for a Mermaid diagram of the main flow:

Router → Bridges (CodeWorld, Certainly) → Runners/Prover → Manifests/Artifacts → Health/Metrics

## Repo Layout

- `deploy/docker/` — Tracked Dockerfiles and compose profiles (core, modules, full, stack).
- `docs/` — Project documentation (deploy guides, reviews, archive status, assets/screenshots).
- `litellm/` — Providers and Router integration (adds `codeworld`, `lean4`, and `certainly` alias).
- `src/` — Bridges and engines:
  - `codeworld/bridge` and `codeworld/engine` (strategy/scoring runners, judge).
  - `lean4_prover/bridge` (Certainly/Lean4 bridge).
- `scenarios/` — Live end‑to‑end demos (skip‑friendly) for bridges and Router.
- `tests/` — Deterministic, offline unit tests; `tests/_archive/` for legacy root tests.
- `local/artifacts/` — Generated artifacts (logo, MVP reports, run JSON). CI uploads artifacts from here.
- `scripts/` — Top‑level utility scripts; provider parity/report helpers.

## Competitive Positioning (quick read)

| Use SciLLM when… | Use other tools when… |
| --- | --- |
| You need a prover‑in‑the‑loop stack with reproducible artifacts, strict readiness, and an OpenAI‑compatible surface. | You only need a provider gateway (LiteLLM upstream) or agent composition without proofs (LangChain/LlamaIndex/AutoGen). |
| You want to execute & rank code strategies under custom metrics with a safety wrapper and built‑in judge. | You only need LLM output scoring (DeepEval/Langfuse/OpenAI Evals) without running arbitrary code variants. |
| You want local‑first bring‑up (Docker), health/metrics endpoints, and per‑run manifests for audit. | You prefer hosted agents or cloud‑only flows and don’t need strict reproducibility gates. |

## Beta Limits & Policy

- Platforms: Linux recommended. macOS/Windows run with reduced isolation (no `unshare -n`).
- CodeWorld isolation: process‑level (RLIMITs + AST allow/deny). For GA, use containerized workers (seccomp/AppArmor).
- Certainly (Lean4) status: umbrella provider in beta; future Coq/Isabelle backends are out of scope for this milestone.
- Deprecation: `additional_kwargs['certainly']` is canonical; mirroring to `['lean4']` defaults to 1 for this release and flips to 0 next release.

## LLMs Are Fallible — Verify Deterministically

Scientists and engineers are rightly skeptical of hallucinations. SciLLM is designed to “trust, but verify”:
- Treat LLM outputs like untrusted suggestions; ask them to produce artifacts that can be checked.
- Verify with compiler‑like determinism: CodeWorld executes strategies under limits and scores them; Certainly compiles and proves Lean4 obligations.
- Keep a paper trail: every run emits a manifest (run_id, request_id, item_ids, session/track, provider info) for replay and audit.
- Separate concerns: deterministic unit tests live in `tests/`; live scenarios in `scenarios/` can touch the world and are skip‑friendly.

## Operator Checklist

- Set `READINESS_LIVE=1` and `STRICT_READY=1` for strict runs; choose `READINESS_EXPECT=codeworld,certainly` (or `codeworld,lean4`).
- Check `/healthz` and `/metrics` on both bridges.
- Find run artifacts under `local/artifacts/runs/` (includes `run_id`, `request_id`, `item_id`s, session/track, provider info).
- Gate CI on judge thresholds (% proved, correctness/speed) and return artifacts for review.
  - Model discovery: example ids like “gpt‑5” may not be present; always choose an id from `GET $CODEX_AGENT_API_BASE/v1/models`.
  - The reasoning flag (`reasoning={"effort":"high"}`) is optional; examples include it to demonstrate high‑reasoning flows.

## Security & Isolation Notes
- CodeWorld: process RLIMITs + optional `unshare -n`. Use containers for stronger isolation.
- codex‑agent echo mode: for development only; disable before adding real credentials.
- Validate outputs (proof artifacts, code variants) before trusting or executing downstream.

## Parallel Fan‑Out (Advanced)
Use `parallel_acompletions` (ordered) for stable batch results. Experimental `parallel_as_completed` yields as each finishes (unordered); prefer first in pipelines requiring deterministic mapping.

```python
from litellm import Router
from litellm.router_utils.parallel_acompletion import RouterParallelRequest
import os

router = Router(model_list=[{
  "model_name":"demo",
  "litellm_params":{
    "model":"<MODEL_ID>",
    "custom_llm_provider":"codex-agent",
    "api_base":os.getenv("CODEX_AGENT_API_BASE")
  }
}])

reqs = [
  RouterParallelRequest("demo", [{"role":"user","content":"List 3 primes"}]),
  RouterParallelRequest("demo", [{"role":"user","content":"Say hi"}])
]

results = await router.parallel_acompletions(reqs, concurrency=2)
for r in results:
  print(r.index, bool(r.error), r.content)
```

---
End of README refinements (duplicate TL;DR removed, placeholders clarified, security & parallel sections added).

## Automatic Chutes Selection, Fallbacks, and Attribution (opt‑in)

SciLLM ships friction‑free helpers for OpenAI‑compatible gateways like Chutes:
- `auto_router_from_env(kind="text", require_json=True)` → builds a Router from numbered chutes (`CHUTES_API_BASE_n/CHUTES_API_KEY_n`) and orders by availability (`/v1/models`) and utilization (`/chutes/utilization`, advisory).
- `infer_with_fallback(messages, kind="text", require_json=True)` → returns the first successful response and attaches `resp.scillm_meta` with `served_model`, `routing`, and `attempts`.
- `find_best_chutes_model(kind="text", require_json=True, util_threshold=0.85)` → picks a single Router‑ready entry under the threshold when possible.
- `warm_chutes_caches(model_list)` → one‑shot preflight to warm `/v1/models` and utilization caches (TTL).

Environment (no code edits)
- Provide numbered chutes: `CHUTES_API_BASE_1`, `CHUTES_API_KEY_1`, `CHUTES_API_BASE_2`, `CHUTES_API_KEY_2`, …
- Optional per‑kind model pins: `CHUTES_TEXT_MODEL_1`, `CHUTES_VLM_MODEL_1`, `CHUTES_TOOLS_MODEL_1`
- Ranker knobs: `SCILLM_UTIL_TTL_S=45`, `SCILLM_UTIL_HI=0.85`, `SCILLM_UTIL_LO=0.50`, `SCILLM_UTIL_K=2`

Live notebooks (generated by `scripts/notebooks_build.py`)
- `09_fallback_infer_with_meta.ipynb` — fallback + attribution
- `10_auto_router_one_liner.ipynb` — one‑liner Router
- `11_provider_perplexity.ipynb` — Litellm‑native provider
- `12_provider_openai.ipynb` / `13_provider_anthropic.ipynb` — normal providers
- `14_provider_matrix.ipynb` — OpenAI, Anthropic, Perplexity, and Chutes (with attribution) side‑by‑side
\n+## Notebooks Quick Guide

Each notebook below is self‑contained and runnable. Use the “executed” versions to see verified outputs; use the source versions to modify and re‑run locally.

- 01 Chutes — OpenAI‑Compatible
  - Source: `notebooks/01_chutes_openai_compatible.ipynb` — When you want a single JSON response via Chutes (Bearer header), quick sanity for pipelines.
  - Executed: `notebooks/executed/01_chutes_openai_compatible_executed.ipynb`

- 02 Router.parallel_acompletions
  - Source: `notebooks/02_router_parallel_batch.ipynb` — When you need fast, concurrent batch calls; shows per‑item timeouts and loop‑safe async.
  - Executed: `notebooks/executed/02_router_parallel_batch_executed.ipynb`

- 03 Model List — First Success
  - Source: `notebooks/03_model_list_first_success.ipynb` — Prefer primary then auto‑fallback; per‑deployment headers included.
  - Executed: `notebooks/executed/03_model_list_first_success_executed.ipynb`

- 04a Advanced — Tools (smoke‑safe)
  - Source: `notebooks/04a_tools_only.ipynb` — When you need function/tool calling with OpenAI‑format tools; includes rate‑limit tips.
  - Executed: `notebooks/04a_tools_only_executed.ipynb`

- 04b Advanced — Streaming (manual)
  - Source: `notebooks/04b_streaming_demo.ipynb` — Live token streaming demo; set `SCILLM_ALLOW_STREAM_SMOKE=1` to execute.

- 05 Codex‑Agent — Doctor
  - Source: `notebooks/05_codex_agent_doctor.ipynb` — Verifies health and a minimal JSON chat through the Codex agent.
  - Executed: `notebooks/executed/05_codex_agent_doctor_executed.ipynb`

- 06 Mini‑Agent — Doctor
  - Source: `notebooks/06_mini_agent_doctor.ipynb` — Validates mini‑agent surfaces and prints a minimal JSON chat.
  - Executed: `notebooks/executed/06_mini_agent_doctor_executed.ipynb`

- 07 CodeWorld — MCTS
  - Source: `notebooks/07_codeworld_mcts.ipynb` — Starts the bridge and runs an MCTS scenario; expect `run_manifest.mcts_stats`.
  - Executed: `notebooks/executed/07_codeworld_mcts_executed.ipynb`

- 08 Certainly Bridge — Doctor
  - Source: `notebooks/08_certainly_bridge.ipynb` — Confirms the bridge responds and proves ≥1 item; includes quick triage.
  - Executed: `notebooks/executed/08_certainly_bridge_executed.ipynb`

- 09 Fallback Inference + Attribution
  - Source: `notebooks/09_fallback_infer_with_meta.ipynb` — Reliability path; returns first success and `served_model` in meta.
  - Executed: `notebooks/executed/09_fallback_infer_with_meta_executed.ipynb`

- 10 Auto Router — One Liner
  - Source: `notebooks/10_auto_router_one_liner.ipynb` — Builds a Router from env (availability + utilization) and routes JSON.
  - Executed: `notebooks/executed/10_auto_router_one_liner_executed.ipynb`

- 11 Perplexity — Sonar Family
  - Source: `notebooks/11_provider_perplexity.ipynb` — Sonar model guide + live model list; examples for sync/async/batch; default `sonar`.
  - Executed: `notebooks/executed/11_provider_perplexity_executed.ipynb`

- 12 Provider — OpenAI
  - Source: `notebooks/12_provider_openai.ipynb` — Baseline OpenAI usage via SciLLM; simple “OK” sanity.
  - Executed: `notebooks/executed/12_provider_openai_executed.ipynb`

- 13 Provider — Anthropic
  - Source: `notebooks/13_provider_anthropic.ipynb` — Baseline Claude usage via SciLLM; error‑safe when key missing.
  - Executed: `notebooks/executed/13_provider_anthropic_executed.ipynb`

- 14 Provider Matrix — OpenAI, Anthropic, Perplexity, Chutes
  - Source: `notebooks/14_provider_matrix.ipynb` — First‑time environment triage; each section skips when its key is missing.
  - Executed: `notebooks/executed/14_provider_matrix_executed.ipynb`
