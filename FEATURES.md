# SciLLM Features (Concise, Agent‑Friendly)

This file is a quick, practical map of SciLLM’s capabilities, what they do, and how to use them. It favors:
1. One paved path per feature
2. Minimal knobs (advanced flags linked, not repeated)
3. Copy/paste blocks that run after `uv sync`

New in this revision:
- Replaced placeholder model IDs (`gpt-5`) with `<MODEL_ID>` guidance
- Added retry metadata JSON example
- Documented mini‑agent trace storage + security cautions
- Clarified parallel results object shape
- Explicit environment prefix guidance (SCILLM_* over legacy LITELLM_*)
- Chutes (OpenAI‑compatible) paved path: Authorization header, Router batch, and a per‑host “doctor”
 - Automatic selection & fallback helpers: `auto_router_from_env`, `infer_with_fallback`, `find_best_chutes_model`, and utilization‑aware ranking (hysteresis by default)

> Conventions
> - Imports assume: `from litellm import Router, completion, acompletion` (or `from scillm import ...` — re‑exported).
> - “Opt‑in” means disabled by default; enable via env or simple kwargs.
> - File paths are relative; open them in your editor for details.
> - Replace `<MODEL_ID>` with a model actually returned by `GET /v1/models` or your configured provider list.
> - Preferred env variable prefix: `SCILLM_`. Legacy `LITELLM_` aliases remain supported.

## Environment Prefix (One‑liner)
Prefer: `SCILLM_ENABLE_CODEWORLD=1` etc. (All `SCILLM_ENABLE_*` have `LITELLM_ENABLE_*` fallbacks.)

## Core Providers & Surfaces

| Area | Feature | What It Does | How To Use (one‑liner) | Files/Notes |
|---|---|---|---|---|
| Providers | OpenAI‑compatible (Chutes, etc.) | Call OpenAI‑style gateways | `completion(model="<MODEL_ID>", api_base=$BASE, api_key=None, custom_llm_provider="openai_like", extra_headers={"x-api-key":$KEY}, messages=...)` | JSON=x-api-key; Streaming=Bearer; IDs from `/v1/models` |
| UX | Auto Router (Chutes) | Discover, rank (availability+util), and route | `from scillm.extras import auto_router_from_env; router = auto_router_from_env(kind='text', require_json=True)` | Numbered envs `CHUTES_API_BASE_n/CHUTES_API_KEY_n` |
| UX | Fallback with attribution | Do not fail; annotate served model + route | `from scillm.extras import infer_with_fallback` | `resp.scillm_meta` contains `served_model` |
| UX | Best single under capacity | Choose one candidate under util threshold | `from scillm.extras import find_best_chutes_model` | Advisory; uses `/chutes/utilization` when present |
| Providers | codex‑agent (OpenAI‑compatible shim) | Route to your codex‑agent (tools, plans, MCP) via OpenAI Chat API | `completion(model="<MODEL_ID>", custom_llm_provider="codex-agent", api_base=$CODEX_AGENT_API_BASE, messages=[...])` | Provider adds `/v1` |
| UX | Model‑only alias | Use “only change model” form (no provider arg) | `completion(model="codex-agent/gpt-5", api_base=$CODEX_AGENT_API_BASE, messages=[...])` | New guard ensures no OpenAI fallback |
| Deprecated | codex‑cloud (remote best‑of‑N) | No public, stable Codex Cloud Tasks API; prefer codex‑agent or gateway best‑of‑N | — | Disabled by default |
| Providers | Ollama | Local free LLMs via Ollama | `completion(model="ollama/qwen2.5:7b", custom_llm_provider="ollama", api_base=...)` | Normalizes `ollama/<tag>` → `<tag>` |
| Providers | CodeWorld | Code orchestration (variants, scoring, judge) over HTTP bridge | `completion(model="codeworld", custom_llm_provider="codeworld", api_base=..., items=...)` | litellm/llms/codeworld.py |
| Providers | Certainly (Lean4) | Lean4 bridge for formal proofs/checks | `completion(model="certainly", custom_llm_provider="certainly", api_base=..., items=...)` | litellm/llms/lean4.py |
| Agent | mini‑agent (experimental) | Deterministic local tool‑use loop (local tool invocation / MCP semantics) | `completion(model="mini-agent/loop", custom_llm_provider="mini-agent", messages=[...])` | See MINI_AGENT.md |

MCP (Model Context Protocol) — Mini‑Agent
- Start: `uvicorn litellm.experimental_mcp_client.mini_agent.agent_proxy:app --host 127.0.0.1 --port 8788`
- Probe: `curl -sSf http://127.0.0.1:8788/ready`
- In‑process example: `python examples/mini_agent_inprocess.py` (uses LocalMCPInvoker with a safe allowlist)
- Notes: The agent proxy exposes an HTTP contract for tool calls; see MINI_AGENT.md for request/response shape and limits.

## codex‑agent — Local or Docker, Exact Values

- Start the mini‑agent shim (OpenAI‑compatible):
  - `uvicorn litellm.experimental_mcp_client.mini_agent.agent_proxy:app --host 127.0.0.1 --port 8788`
  - If `:8788` is busy, pick another (e.g., `8789`).
- Environment (set before importing Router):
  - `LITELLM_ENABLE_CODEX_AGENT=1`
  - `CODEX_AGENT_API_BASE=http://127.0.0.1:8788`  (do NOT append `/v1` — the provider adds it)
  - `CODEX_AGENT_API_KEY` (usually unset for local; set only if your gateway enforces auth)
- Quick verify:
  - Health: `curl -sSf http://127.0.0.1:8788/healthz`
  - Models: `curl -sS http://127.0.0.1:8788/v1/models | jq .`
  - High reasoning chat (optional flag): \
    `curl -sS -H 'content-type: application/json' -d '{"model":"<MODEL_ID>","reasoning":{"effort":"high"},"messages":[{"role":"user","content":"hello"}]}' $CODEX_AGENT_API_BASE/v1/chat/completions | jq -r '.choices[0].message.content'`
  - Doctor (one‑shot): `make codex-agent-doctor` (checks /healthz, /v1/models, and a high‑reasoning ping)
- Router usage (copy/paste, high reasoning):
  - `export LITELLM_ENABLE_CODEX_AGENT=1`
  - `export CODEX_AGENT_API_BASE=http://127.0.0.1:8788`
  - Python:
    - `from litellm import Router`
    - `r = Router()`
    - `out = r.completion(model="<MODEL_ID>", custom_llm_provider="codex-agent", messages=[{"role":"user","content":"Return STRICT JSON only: {\"ok\":true}"}], response_format={"type":"json_object"})`
    - `print(out.choices[0].message["content"])`

Tip: Using Docker? `docker compose -f local/docker/compose.agents.yml up --build -d` exposes the mini‑agent on `127.0.0.1:8788` and the codex sidecar on `127.0.0.1:8077`. For the Router provider, set `CODEX_AGENT_API_BASE` to the one you want (no `/v1`).

codex‑agent base rule and endpoints
- Set `CODEX_AGENT_API_BASE` WITHOUT `/v1`. The provider appends `/v1/chat/completions` internally.
- Both sidecar (8077) and mini‑agent (8788) expose:
  - `GET /healthz` (OK)
  - `GET /v1/models` (stub list)
  - `POST /v1/chat/completions` (OpenAI‑compatible; choices[0].message.content is a string)

Auth, echo & debug
- Sidecar echo is enabled by default in `local/docker/compose.agents.yml` (`CODEX_SIDECAR_ECHO=1`). For real creds, disable echo and mount `${HOME}/.codex/auth.json:/root/.codex/auth.json:ro`, then run `python debug/check_codex_auth.py`.
- Helpful probes:
  - `python debug/verify_mini_agent.py` (Docker + optional local uvicorn)
  - `python debug/verify_codex_agent_docker.py` (compose start optional; probes /healthz,/models,/chat)
  - `python debug/codex_parallel_probe.py` (prints `content` + `scillm_router` for parallel echo)

Codex‑Agent vs CodeWorld: zero‑ambiguity contract
- codex‑agent: OpenAI‑compatible chat at `$CODEX_AGENT_API_BASE/v1/chat/completions`.
  - Use `model="codex-agent/<ID>"` or `custom_llm_provider="codex-agent"` with `model="<ID>"`.
  - Minimal helper: `from scillm.extras.codex import chat`.
  - Doctor: `python debug/codex_agent_doctor.py` (health, chat, judge) → `doctor: ok`.
- CodeWorld: strategy runner at `$CODEWORLD_BASE/bridge/complete` (e.g., MCTS).
  - Use `model="codeworld/mcts"` and pass `items=[...]`, `rollouts/depth/uct_c`.
  - Only touches codex‑agent if you explicitly enable autogen.

Judge examples (codex‑agent only)
- completion (parameter‑first):
  ```python
  from scillm import completion
  base = "http://127.0.0.1:8089"
  msgs=[
    {"role":"system","content":"Return STRICT JSON only: {best_id:string, rationale_short:string}."},
    {"role":"user","content":"A vs B — pick one."},
  ]
  r = completion(model="gpt-5", custom_llm_provider="codex-agent", api_base=base,
                 messages=msgs, response_format={"type":"json_object"}, temperature=1,
                 allowed_openai_params=["reasoning","reasoning_effort"], reasoning_effort="medium")
  print(r.choices[0].message["content"])  # strict JSON
  ```
- helper (direct HTTP):
  ```python
  from scillm.extras.codex import chat
  res = chat(messages=msgs, model="gpt-5", base=base,
             response_format={"type":"json_object"}, temperature=1, reasoning_effort="medium")
  print(res["choices"][0]["message"]["content"])  # strict JSON
  ```

Router judge mapping (optional)
- Per‑call (quickest):
  - `r.completion(model="gpt-5", custom_llm_provider="codex-agent", api_base=os.getenv("CODEX_AGENT_API_BASE"), api_key=os.getenv("CODEX_AGENT_API_KEY"), ...)`
- Or define once:
  - `Router(model_list=[{"model_name":"gpt-5","litellm_params":{"model":"gpt-5","custom_llm_provider":"codex-agent","api_base":os.getenv("CODEX_AGENT_API_BASE"),"api_key":os.getenv("CODEX_AGENT_API_KEY")}}])`

Retries meta (optional)
- Set `SCILLM_RETRY_META=1` to stamp `additional_kwargs["router"]["retries"] = {attempts,total_sleep_s,last_retry_after_s}`.

## Router (Batch‑Friendly)

| Area | Feature | What It Does | How To Use | Files/Notes |
|---|---|---|---|---|
| Router | Core routing | Robust sync/async completion through deployments | `from litellm import Router; Router(...).completion(...)` | litellm/router.py |
| Router | parallel_acompletions | Async fan‑out; returns list of result objects | `await router.parallel_acompletions(requests, concurrency=8)` | Each result: `.index .request .response .error .content` |
| Router | Deterministic mode | Enforce temp=0, top_p=1; serialize fan‑out | `Router(deterministic=True)` | Also zeros freq/pres penalties in deterministic contexts |
| Router | Schema‑first + fallback | Try JSON schema, fallback once to json_object; validation meta | `response_format={"type":"json_object"}` or schema path under provider | Additional meta in `additional_kwargs["router"]` |
| Router | Image policy (minimal) | Guard data:image/* sizes (reject mode) | Enabled internally; no extra knobs for MVP | litellm/router.py |

### Parallel Result Object (Shape)
```
SimpleNamespace(
  index: int,
  request: original request object (dict or RouterParallelRequest),
  response: provider response object/dict | None,
  error: Exception | None,
  content: str | None   # best-effort extraction
)
```

### Advanced (Experimental)
- `parallel_as_completed(requests)` — yields results as they finish (unordered). Not guaranteed stable; prefer `parallel_acompletions` for deterministic ordering.

## Router — 429 Retries (Opt‑In)

| Area | Feature | What It Does | How To Enable | Files/Notes |
|---|---|---|---|---|
| Retry | Retry‑After awareness | Honor Retry‑After (seconds or HTTP‑date) | `retry_enabled=True, honor_retry_after=True` | Floors 0→0.5s; cap by budget |
| Retry | Exponential full‑jitter | Backoff when header missing | `retry_base_s, retry_max_s, retry_jitter_pct` | Defaults: base≈5, max≈120, jitter≈0.25 |
| Retry | Budgets | Cap retries by time and attempts | `retry_time_budget_s=900, retry_max_attempts=8` | Per‑call or env |
| Retry | Callbacks | on_attempt/on_success/on_giveup (for checkpoint/resume) | Pass callbacks in kwargs | Emits dict meta per attempt/success |
| Retry | JSON telemetry | Low‑noise structured logs | `SCILLM_LOG_JSON=1` and `SCILLM_RETRY_LOG_EVERY=1|N` | Stdout one‑liners |

Recommended defaults (long unattended runs):
`retry_enabled=True honor_retry_after=True retry_time_budget_s=600-900 retry_max_attempts=8 retry_base_s=5-10 retry_max_s=90-120 retry_jitter_pct=0.25`

Retry metadata example (when `SCILLM_RETRY_META=1`):
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

## CodeWorld — Strategies & MCTS (Opt‑In)

| Area | Feature | What It Does | How To Use | Files/Notes |
|---|---|---|---|---|
| CodeWorld | Baseline | Run variants, judge, score | `completion(model="codeworld", custom_llm_provider="codeworld", items=...)` | litellm/llms/codeworld.py |
| CodeWorld | MCTS Strategy | Adaptive variant selection (root UCT) | `strategy="mcts"` or model alias `codeworld/mcts` | `scenarios/mcts_codeworld_demo.py` |
| CodeWorld | Autogen + MCTS alias | Generate N then search | `model="codeworld/mcts:auto"` (synonym: `codeworld/mcts+auto`) | Normalized to `codeworld/mcts:auto` in manifests |
| CodeWorld | Seed determinism | Reproducible MCTS runs | `SCILLM_DETERMINISTIC_SEED=<int>` or per‑request | One‑time warnings on mismatches |

One‑POST (HTTP) autogenerate + MCTS
- Ensure bridge can reach your codex‑agent sidecar: set `CODEX_AGENT_API_BASE` where the bridge runs.
- Local bridge:
  - `PYTHONPATH=src uvicorn codeworld.bridge.server:app --port 8888`
  - `BASE=http://127.0.0.1:8888 curl -sS "$BASE/bridge/complete" -H 'Content-Type: application/json' -d '{"messages":[{"role":"user","content":"Autogenerate"}],"items":[{"task":"t","context":{}}],"provider":{"name":"codeworld","args":{"strategy":"mcts","strategy_config":{"autogenerate":{"enabled":true,"n":3}}}}}' | jq '.run_manifest.mcts_stats'`
- Docker bridge only: `make codeworld-bridge-up-only` (defaults `CODEX_AGENT_API_BASE=http://host.docker.internal:8089`). On Linux, compose adds:
  `extra_hosts: ["host.docker.internal:host-gateway"]`. Override via `CODEX_AGENT_API_BASE=http://<host-ip>:8089`.
- Knobs:
  - `CODEWORLD_ONEPOST_TIMEOUT_S` (default 60)
  - `CODEWORLD_MCTS_AUTO_N` (default 3)
  - `CODEWORLD_MCTS_AUTO_TEMPERATURE` (default 0)
  - `CODEWORLD_MCTS_AUTO_MAX_TOKENS` (default 2000)
  - `CODEWORLD_MCTS_AUTO_MODEL` (override internal generation model)
- Autogen alias:
  - `model="codeworld/mcts:auto"` (synonym: `codeworld/mcts+auto` → normalized to `mcts:auto`)
- Determinism: set `SCILLM_DETERMINISTIC_SEED=<int>` to fix variant sampling (warns on mismatch).
- Examples use `reasoning={"effort":"high"}` to showcase the path, but it’s optional.

## Certainly (Lean4)

| Area | Feature | What It Does | How To Use | Notes |
|---|---|---|---|---|
| Lean4 | Bridge | FastAPI shim; batch checks; Router provider | `completion(model="certainly", custom_llm_provider="certainly", api_base=$CERTAINLY_BRIDGE_BASE, items=[...])` | `scenarios/lean4_*` |

Lean4 quick health:
```
curl -sSf $CERTAINLY_BRIDGE_BASE/healthz
```

Batch example (canonical envelope):
```json
{
  "messages": [{"role": "system", "content": "Check requirements"}],
  "items": [{"requirement_text": "0 + n = n"}],
  "options": {"max_seconds": 120}
}
```


## Chutes Model Resolution (Alias, Once per Session)

- Purpose: Accept human-friendly “doc-style” names (e.g., `mistral-ai/Mistral-Small-3.2-24B`) and resolve to the canonical IDs returned by your org’s `GET $CHUTES_API_BASE/v1/models`.
- One-time warm (per process):
  ```python
  from litellm.extras.preflight import preflight_models
  import os
  preflight_models(api_base=os.environ["CHUTES_API_BASE"], api_key=os.environ.get("CHUTES_API_KEY"))
  ```
- Enable guard + aliasing:
  - `export SCILLM_MODEL_PREFLIGHT=1`
  - `export SCILLM_MODEL_ALIAS=1`
  - [optional tie-break] `export SCILLM_MODEL_ALIAS_LLM=1`
- Behavior:
  - Regex prefilter → rapidfuzz ranking → pick closest canonical ID from your cached catalog.
  - Never guesses outside your org’s live list; zero network per call.
- API knobs (planned):
  - `fallback_closest=True` (default) and `fallback_closest_cutoff=0.55` on `completion()/acompletion()`; kwargs override envs.
- Observability:
  - Responses stamp `_hidden_params.requested_model_id` and `_hidden_params.resolved_model_id` when a mapping occurs.

## Scenarios (Live, Skip‑Friendly)

| Script | Purpose | How To Run |
|---|---|---|
| scenarios/run_all.py | Orchestrates live demos in a safe order | `python scenarios/run_all.py` |
| scenarios/codeworld_bridge_release.py | CodeWorld bridge health + summary | `python scenarios/codeworld_bridge_release.py` |
| scenarios/codeworld_judge_live.py | Judge/metrics demo | `python scenarios/codeworld_judge_live.py` |
| scenarios/mcts_codeworld_demo.py | MCTS demo | `python scenarios/mcts_codeworld_demo.py` |
| scenarios/lean4_bridge_release.py | Lean4 bridge demo | `python scenarios/lean4_bridge_release.py` |
| scenarios/codex_agent_router.py | Router→codex‑agent demo | `python scenarios/codex_agent_router.py` |
| scenarios/provider_warmup_probe.py | One‑off warm‑up probe | `python scenarios/provider_warmup_probe.py --provider chutes|runpod --model "$LITELLM_DEFAULT_MODEL"` |

## Contrib Helpers (Batch & Rate)

| Feature | What It Does | How To Use | Files/Notes |
|---|---|---|---|
| JsonlCheckpoint | Resume long runs; avoid re‑processing | `cp = JsonlCheckpoint(path, id_key="id"); done = cp.processed_ids(); cp.append({...})` | litellm/contrib/batch.py; docs/guide/batch_helpers.md |

## Verified Tool‑Calling Models (Chutes)

We only mark models as supporting tool‑calling when live verification returns an OpenAI‑style `tool_calls` array.

- Model: `moonshotai/Kimi‑K2‑Instruct‑0905`
  - Endpoint: `/v1/chat/completions`
  - Auth: `Authorization: Bearer <key>`
  - stream: `false`
  - tool_choice: `"auto"`
  - Verified on: 2025‑10‑25

Everything else is unverified until a probe succeeds. See `local/docs/01_guides/TOOLS_SUPPORT.md` for a curl/Python probe and how to add entries.
| TokenBucket (sync) | Gentle in‑process throttling (threads) | `bucket = TokenBucket(rate_per_sec=3.0, capacity=6); with bucket.acquire(): call()` | Same |
| AsyncTokenBucket | Async throttling (coroutines) | `bucket = AsyncTokenBucket(3.0, 6); async with (await bucket.acquire()): await call()` | Same |
| run_batch | Tiny async runner: skip→throttle→call→append | `await run_batch(items, id_key, fn, checkpoint_path, bucket=bucket, max_concurrency=12)` | Same |

## Warm‑Ups & Readiness (Opt‑In)

| Area | Feature | What It Does | How To Use | Files/Notes |
|---|---|---|---|---|
| Warm‑up | Strict composite | Enforce chutes/runpod warm‑ups | `STRICT_WARMUPS=1 make project-ready-live` | readiness.yml |
| Readiness | Strict/live gates | Full green path for deploy checks | `make project-ready-live` (see README) | PROJECT_READY.md + artifacts |

## Retry Guide & Batch Guide

| Doc | Contents |
|---|---|
| docs/guide/RATE_LIMIT_RETRIES.md | Enabling Router retries, env knobs, callback meta |
| docs/guide/batch_helpers.md | JsonlCheckpoint/TokenBucket/AsyncTokenBucket/run_batch examples |

## Packaging & Tooling

| Area | Feature | What It Does | How To Use | Files/Notes |
|---|---|---|---|---|
| Build | uv/hatch (PEP 621) | Lock‑free sync; fast dev cycles | `uv sync`; `uv run pytest` | pyproject.toml; Makefile |
| Re‑exports | scillm module | Import `scillm` as alias to litellm | `from scillm import Router, completion` | `scillm/__init__.py` |

## Mini‑Agent (Additional Details)
- Endpoints: `/ready`, `/healthz`, `/v1/chat/completions`, `/agent/run`
- Trace storage (optional):
  ```
  export MINI_AGENT_STORE_TRACES=1
  export MINI_AGENT_STORE_PATH=local/artifacts/mini_agent_traces.jsonl
  ```
  Each run appends JSONL with `final_answer_preview`.
- Limitations:
  - Images detected in content arrays but not processed beyond flagging.
  - No streaming responses; one final message.
  - Local tool sandbox is coarse; treat outputs as untrusted.

## Security & Isolation (Summary)
- codex‑agent echo mode (`CODEX_SIDECAR_ECHO=1`) is for development only—disable before real credentials.
- CodeWorld sandbox: process RLIMITs + optional network namespace; use container isolation for production.
- Mini‑Agent tool outputs: treat as untrusted; validate before executing derived code.
- Model outputs: assume untrusted; combine with deterministic verification (tests, proofs, scoring).

## Common Patterns (TL;DR)

| Pattern | Single Line |
|---|---|
| codex‑agent judge (strict JSON) | `resp = Router().completion(model="<MODEL_ID>", custom_llm_provider="codex-agent", api_base=$CODEX_AGENT_API_BASE, response_format={"type":"json_object"}, retry_enabled=True)` |
| MCTS (CodeWorld) | `completion(model="codeworld/mcts", custom_llm_provider="codeworld", items=..., strategy_config={"rollouts":48,"depth":6})` |
| Ollama local | `completion(model="ollama/qwen2.5:7b", custom_llm_provider="ollama", api_base="http://127.0.0.1:11434", messages=...)` |
| Async fan‑out | `await Router().parallel_acompletions([req1, req2], concurrency=16)` |
| Retry defaults | `retry_enabled=True, honor_retry_after=True, retry_time_budget_s=900, retry_max_attempts=8, retry_base_s=5, retry_max_s=120, retry_jitter_pct=0.25` |
| Checkpoint/resume | `cp = JsonlCheckpoint(".../results.jsonl"); done = cp.processed_ids(); cp.append({...})` |
| Throttle (sync/async) | `with bucket.acquire(): ...` / `async with (await bucket.acquire()): ...` |

## Multi‑Agent Helpers (New)

- Text (spawn N codex‑agents and judge best):
  - Python
    - `from scillm.extras.multi_agents import answer_text_multi`
    - `out = answer_text_multi(messages=[{"role":"user","content":"Explain quicksort in 3 bullets."}], model_ids=["<MODEL_ID_A>","<MODEL_ID_B>"], judge_model="openai/gpt-4o-mini", codex_api_base=os.getenv("CODEX_AGENT_API_BASE"))`
    - `print(out["best_index"], out["answers"][out["best_index"]])`

- Code (apply MCTS over variants or autogenerate):
  - Python
    - `from scillm.extras.multi_agents import answer_code_mcts`
    - Variants provided: `resp = answer_code_mcts(items, codeworld_base="http://127.0.0.1:8888", rollouts=24, depth=6)`
    - Autogenerate N then search: `resp = answer_code_mcts(items=[{"task":"t","context":{}}], codeworld_base="http://127.0.0.1:8888", autogenerate_n=3, rollouts=24, depth=6)`
    - `print(resp["results"][0]["mcts"]["best_variant"])`

### Autogen (seamless)

- Ensure codex‑agent automatically, then autogenerate variants + MCTS in one call:
  - `from scillm.extras import ensure_codex_agent, answer_code_mcts_autogen`
  - `ensure_codex_agent()`  # starts sidecar if needed or uses CODEX_AGENT_API_BASE
  - `items=[{"task":"6 improved fast inverse sqrt for gaming plugin","context":{}}]`
  - `resp = answer_code_mcts_autogen(items, n_variants=6, rollouts=48, depth=6, uct_c=1.25, temperature=0.0, codeworld_base=os.getenv("CODEWORLD_BASE","http://127.0.0.1:8888"))`
  - `payload = resp.additional_kwargs["codeworld"]`
  - `print(payload["run_manifest"]["strategy_generator"])`
  - `print(payload["results"][0]["mcts"])`

### Autogen + Judge (end‑to‑end)

- One‑shot helper that generates N variants, runs MCTS, then asks a codex‑agent judge for the best id.
  - Env:
    - `SCILLM_ENABLE_CODEWORLD=1`
    - `CODEX_AGENT_API_BASE=http://127.0.0.1:8089` (sidecar or gateway; no `/v1`)
    - Optional: `CODEWORLD_AUTOGEN_HTTP_TIMEOUT_S=120` for longer generation
  - Python:
    - `from scillm.extras import ensure_codex_agent`
    - `from scillm.extras.multi_agents import answer_code_mcts_autogen_and_judge`
    - `ensure_codex_agent()`
    - `items=[{"task":"six improved fast inverse square root for gaming (C/C++)","context":{}}]`
    - `res = answer_code_mcts_autogen_and_judge(items, n_variants=6, rollouts=12, depth=4, uct_c=1.3, temperature=0.0, codeworld_base=os.getenv("CODEWORLD_BASE","http://127.0.0.1:8888"), judge_model="gpt-5", timeout=120)`
    - `print(res["codeworld"]["results"][0]["mcts"]["best_value"])`
    - `print(res["judge"])  # {best_id: ..., rationale_short: ...}`

Notes
- Judge uses the codex-agent sidecar and requests strict JSON (`response_format={"type":"json_object"}`) with reasoning enabled; if the model returns nearly‑JSON text, the helper attempts a safe salvage and logs a short preview when `SCILLM_DEBUG=1`.

Fallbacks
- If the embedded sidecar cannot start or fails probes, ensure_codex_agent() checks these env bases (no `/v1`):
  - `SCILLM_AUTOGEN_FALLBACK_BASE`, `OPENAI_BASE_URL`, `CHUTES_API_BASE`, `RUNPOD_API_BASE`
- It sets `CODEX_AGENT_API_BASE` to the first healthy endpoint.

---

If you need a quick example for a specific provider or scenario, open the files listed and copy the minimal snippet; everything above is designed to work “as‑is” on this branch. Report missing or broken paths via a documentation issue.


## Strict JSON (Auto Sanitize)
- Enable globally: `export SCILLM_JSON_SANITIZE=1`
- Or per-call: `auto_json_sanitize=True`
- Triggers when `response_format={"type":"json_object"}` or `response_mime_type="application/json"`.
- Uses `litellm.extras.clean_json_string()` to repair and re-validate; stamps `_hidden_params.json_sanitized=true`.
## Chutes (OpenAI‑compatible) — Paved Path

- Shared base (recommended default)
  - Headers: use `Authorization: Bearer $CHUTES_API_KEY` for `/v1/chat/completions`. `/v1/models` also accepts Bearer (and often `x-api-key`).
  - Single call (Python):
    ```python
    from scillm import completion, os
    out = completion(
      model=os.environ["CHUTES_MODEL_ID"],
      api_base=os.environ["CHUTES_API_BASE"],
      api_key=os.environ["CHUTES_API_KEY"],  # Bearer
      custom_llm_provider="openai_like",
      messages=[{"role":"user","content":"Return only {\"ok\":true} as JSON."}],
      response_format={"type":"json_object"}, temperature=0, max_tokens=16)
    ```
  - Batch (Router): set `api_key` in `model_list` so Bearer is sent:
    ```python
    from scillm import Router, os
    r = Router(model_list=[{"model_name":"chutes","litellm_params":{
      "model": os.environ["CHUTES_MODEL_ID"],
      "api_base": os.environ["CHUTES_API_BASE"],
      "api_key": os.environ["CHUTES_API_KEY"],
      "custom_llm_provider": "openai_like"}}])
    ```

- Per‑host chute (opt‑in lifecycle)
  - Deploy via `uv run chutes deploy <module>:chute --accept-fee`.
  - Readiness gate: `GET https://<slug>.chutes.ai/v1/models == 200`.
  - Verify + batch: `scripts/chutes_host_doctor.py --slug <slug> --model <id>` prints a one‑line summary and saves a JSON artifact.

Notes
- Prefer the shared base for day‑to‑day batch work; use per‑host when you need isolation/pinning.
- Some models return fenced JSON (```json ... ```); strip fences in your client if you require raw JSON.
