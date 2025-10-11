# SciLLM Features (Concise, Agent‑Friendly)

This file is a quick, practical map of SciLLM’s capabilities, what they do, and how to use them. It favors one clear path per feature with minimal knobs. All paths work with `uv` (see pyproject and Makefile).

> Conventions
> - Imports assume: `from litellm import Router, completion, acompletion` (or `from scillm import ...` — re‑exported).
> - “Opt‑in” means disabled by default; enable via env or simple kwargs.
> - File paths are relative; open them in your editor for details.

## Core Providers & Surfaces

| Area | Feature | What It Does | How To Use (one‑liner) | Files/Notes |
|---|---|---|---|---|
| Providers | OpenAI‑compatible | Call any OpenAI‑style model (local or remote) | `completion(model="openai/<org>/<model>", messages=...)` or `Router(...).completion(...)` | litellm/main.py |
| Providers | codex‑agent (OpenAI‑compatible shim) | Route to your codex-agent (tools, plans, MCP) via OpenAI Chat API | `completion(model="gpt-5", custom_llm_provider="codex-agent", api_base=..., api_key=...)` | litellm/llms/codex_agent.py; compare script supports `--use-codex-*` |
| Providers | Ollama | Local free LLMs via Ollama | `completion(model="ollama/qwen2.5:7b", custom_llm_provider="ollama", api_base=...)` | Normalizes `ollama/<tag>` → `<tag>` |
| Providers | CodeWorld | Code orchestration (variants, scoring, judge) over HTTP bridge | `completion(model="codeworld", custom_llm_provider="codeworld", api_base=..., items=...)` | litellm/llms/codeworld.py |
| Providers | Certainly (Lean4) | Lean4 bridge for formal proofs/checks | `completion(model="certainly", custom_llm_provider="certainly", api_base=..., items=...)` | litellm/llms/lean4.py |
| Agent | mini‑agent (experimental) | Deterministic local tool‑use loop | `completion(model="mini-agent/...", custom_llm_provider="mini-agent")` | docs/my-website/docs/experimental/mini-agent.md |

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
  - High reasoning chat: `curl -sS -H 'content-type: application/json' -d '{"model":"gpt-5","reasoning":{"effort":"high"},"messages":[{"role":"user","content":"say hello"}]}' http://127.0.0.1:8788/v1/chat/completions | jq -r '.choices[0].message.content'`
- Router usage (copy/paste, high reasoning):
  - `export LITELLM_ENABLE_CODEX_AGENT=1`
  - `export CODEX_AGENT_API_BASE=http://127.0.0.1:8788`
  - Python:
    - `from litellm import Router`
    - `r = Router()`
    - `out = r.completion(model="gpt-5", custom_llm_provider="codex-agent", messages=[{"role":"user","content":"Return STRICT JSON only: {\"ok\":true}"}], reasoning_effort="high", response_format={"type":"json_object"})`
    - `print(out.choices[0].message["content"])`

Tip: Using Docker? `docker compose -f local/docker/compose.agents.yml up --build -d` exposes the mini‑agent on `127.0.0.1:8788` and the codex sidecar on `127.0.0.1:8077`. For the Router provider, set `CODEX_AGENT_API_BASE` to the one you want (no `/v1`).

codex‑agent base rule and endpoints
- Set `CODEX_AGENT_API_BASE` WITHOUT `/v1`. The provider appends `/v1/chat/completions` internally.
- Both sidecar (8077) and mini‑agent (8788) expose:
  - `GET /healthz` (OK)
  - `GET /v1/models` (stub list)
  - `POST /v1/chat/completions` (OpenAI‑compatible; choices[0].message.content is a string)

Auth & debug
- Sidecar echo is enabled by default in `local/docker/compose.agents.yml` (`CODEX_SIDECAR_ECHO=1`). For real creds, disable echo and mount `${HOME}/.codex/auth.json:/root/.codex/auth.json:ro`, then run `python debug/check_codex_auth.py`.
- Helpful probes:
  - `python debug/verify_mini_agent.py` (Docker + optional local uvicorn)
  - `python debug/verify_codex_agent_docker.py` (compose start optional; probes /healthz,/models,/chat)
  - `python debug/codex_parallel_probe.py` (prints `content` + `scillm_router` for parallel echo)

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
| Router | parallel_acompletions | Async fan‑out; returns OpenAI‑shaped dicts in request order | `await router.parallel_acompletions([req,...], max_concurrency=16)` | litellm/router.py; returns list[dict] |
| Router | Deterministic mode | Enforce temp=0, top_p=1; serialize fan‑out | `Router(deterministic=True)` | Also zeros freq/pres penalties in deterministic contexts |
| Router | Schema‑first + fallback | Try JSON schema, fallback once to json_object; validation meta | `response_format={"type":"json_object"}` or schema path under provider | Additional meta in `additional_kwargs["router"]` |
| Router | Image policy (minimal) | Guard data:image/* sizes (reject mode) | Enabled internally; no extra knobs for MVP | litellm/router.py |

## Router — 429 Retries (Opt‑In)

| Area | Feature | What It Does | How To Enable | Files/Notes |
|---|---|---|---|---|
| Retry | Retry‑After awareness | Honor Retry‑After (seconds or HTTP‑date) | `retry_enabled=True, honor_retry_after=True` | Floors 0→0.5s; cap by budget |
| Retry | Exponential full‑jitter | Backoff when header missing | `retry_base_s, retry_max_s, retry_jitter_pct` | Defaults: base≈5, max≈120, jitter≈0.25 |
| Retry | Budgets | Cap retries by time and attempts | `retry_time_budget_s=900, retry_max_attempts=8` | Per‑call or env |
| Retry | Callbacks | on_attempt/on_success/on_giveup (for checkpoint/resume) | Pass callbacks in kwargs | Emits dict meta per attempt/success |
| Retry | JSON telemetry | Low‑noise structured logs | `SCILLM_LOG_JSON=1` and `SCILLM_RETRY_LOG_EVERY=1|N` | Stdout one‑liners |

Recommended defaults for long runs: `retry_enabled=True, honor_retry_after=True, retry_time_budget_s≈600–900, retry_max_attempts≈8, retry_base_s≈5–10, retry_max_s≈90–120, retry_jitter_pct≈0.25`.

## CodeWorld — Strategies & MCTS (Opt‑In)

| Area | Feature | What It Does | How To Use | Files/Notes |
|---|---|---|---|---|
| CodeWorld | Baseline | Run variants, judge, score | `completion(model="codeworld", custom_llm_provider="codeworld", items=...)` | litellm/llms/codeworld.py |
| CodeWorld | MCTS Strategy | Adaptive variant selection (root UCT) | `strategy="mcts"` or model alias `codeworld/mcts` | Scenarios: scenarios/mcts_codeworld_demo.py |
| CodeWorld | Seed determinism | Reproducible MCTS runs | `SCILLM_DETERMINISTIC_SEED=<int>` or per‑request | One‑time warnings on mismatches |

## Certainly (Lean4)

| Area | Feature | What It Does | How To Use | Notes |
|---|---|---|---|---|
| Lean4 | Bridge | FastAPI shim; batch checks; Router provider | `completion(model="certainly", custom_llm_provider="certainly", api_base=..., items=...)` | Scenarios under scenarios/lean4_* |

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
| Re‑exports | scillm module | Import `scillm` as alias to litellm | `from scillm import Router, completion` | scillm/__init__.py |

## Common Patterns (TL;DR)

| Pattern | Single Line |
|---|---|
| codex‑agent judge (strict JSON) | `resp = Router().completion(model="gpt-5", custom_llm_provider="codex-agent", api_base=..., api_key=..., response_format={"type":"json_object"}, retry_enabled=True, honor_retry_after=True)` |
| MCTS (CodeWorld) | `completion(model="codeworld/mcts", custom_llm_provider="codeworld", items=..., strategy_config={"rollouts":48,"depth":6})` |
| Ollama local | `completion(model="ollama/qwen2.5:7b", custom_llm_provider="ollama", api_base="http://127.0.0.1:11434", messages=...)` |
| Async fan‑out | `await Router().parallel_acompletions([req1, req2], max_concurrency=16)` |
| Retry defaults | `retry_enabled=True, honor_retry_after=True, retry_time_budget_s=900, retry_max_attempts=8, retry_base_s=5, retry_max_s=120, retry_jitter_pct=0.25` |
| Checkpoint/resume | `cp = JsonlCheckpoint(".../results.jsonl"); done = cp.processed_ids(); cp.append({...})` |
| Throttle (sync/async) | `with bucket.acquire(): ...` / `async with (await bucket.acquire()): ...` |

---

If you need a quick example for a specific provider or scenario, open the files listed and copy the minimal snippet; everything above is designed to work “as‑is” on feat/final-polish.
