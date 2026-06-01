<p align="center">
  <img src="docs/assets/scillm-header.webp" alt="scillm local multi-provider LLM proxy console" width="100%" />
</p>

<h3 align="center">One proxy. Any provider. Zero glue code.</h3>
<p align="center">Agents and humans call <code>/scillm</code> — it routes, retries, and repairs.</p>

---

Single-tenant LLM proxy that unifies your **paid subscriptions** (Claude Max, Codex Pro) and **API keys** (Gemini, Chutes, DeepSeek, GLM) behind one OpenAI-compatible endpoint. No provider-specific code — just `httpx.post("http://localhost:4001/v1/chat/completions", json={"model": "claude-sonnet-4-6", ...})` and scillm handles OAuth token refresh, format translation, SSE streaming, retries, and failover automatically. Works with any model name: `claude-*`, `gpt-*`, `gemini-*`, `Org/Model`, or `model:tag`.

## Quick Start

### Option A: Standalone (Recommended for new users)

Self-contained deployment with all services included (ArangoDB, memory service, embedding service).

```bash
# 1. Configure API keys
cp .env.example .env
# Edit .env — set at minimum: CHUTES_API_KEY, CHUTES_API_BASE

# 2. Build and start (first run takes ~5 min to download models)
docker compose -p scillm -f deploy/docker/compose.scillm.standalone.yml up -d --build

# 3. Verify
curl -s http://localhost:4001/health/liveliness
# → {"status": "ok"}

# 4. Call it
curl http://localhost:4001/v1/chat/completions \
  -H "Authorization: Bearer sk-dev-proxy-123" \
  -H "Content-Type: application/json" \
  -d '{"model":"text","messages":[{"role":"user","content":"hi"}]}'
```

**Services started:**
- `scillm-proxy` (:4001) — Main LLM gateway
- `scillm-memory` (:8601) — Logging, batch resume, latency stats
- `scillm-embedding` (:8602) — Sentence embeddings
- `scillm-arangodb` (:8529) — Database
- `scillm-utls-proxy` (:8444) — TLS fingerprint for Codex

### Option B: Core Only (For existing infrastructure)

Minimal deployment — assumes memory service (:8601) is already running on host.

```bash
docker compose -p scillm -f deploy/docker/compose.scillm.core.yml up -d --build
```

## Provider Setup

Most providers need zero code — just credentials on disk or in `.env`.

| Provider | What to do | Model names |
|----------|-----------|-------------|
| **Claude** | Nothing (if using Claude Code — token is already in `~/.claude/.credentials.json`) | `claude-sonnet-4-6`, `claude-haiku-4-5` |
| **Codex** | One-time: `npm install -g @openai/codex && codex login` | `gpt-5.5` |
| **Gemini** | Add `GEMINI_API_KEY=...` to `.env` ([get key](https://aistudio.google.com/apikey)) | `gemini-2.5-flash`, `gemini-3-flash-preview` |
| **GLM** | Add `GLM_API_Key=...` to `.env` ([z.ai](https://z.ai)) | `text-glm` (glm-5.1) |
| **Chutes** | Add `CHUTES_API_KEY` + `CHUTES_API_BASE` to `.env` | `text` (default), or any `Org/Model` from Chutes catalog |
| **DeepSeek** | Add `DEEPSEEK_API=...` to `.env` | `text-deepseek` |
| **OpenCode Go** | Add `OPENCODE_GO_API_KEY=...` to `.env` | `opencode-go/kimi-k2.6`, `opencode-go/deepseek-v4-pro`, `opencode-go/minimax-m2.7` |
| **OpenCode serve** | `SCILLM_OPENCODE_SERVE_ENABLED=1` in compose; `OPENCODE_SERVER_PASSWORD` when starting serve (mirror in `.env`) | Agent profiles via `POST /v1/scillm/opencode/runs` — **not** chat model names |
| **Ollama** | `ollama pull model:tag` (local, no auth) | Any `model:tag` |

After adding credentials, rebuild: `docker compose -p scillm -f deploy/docker/compose.scillm.core.yml up -d --build`

**Check what's working:** `curl http://localhost:4001/v1/scillm/auth -H "Authorization: Bearer sk-dev-proxy-123"`

**List OpenCode Go models:** `curl http://localhost:4001/v1/scillm/opencode-go/models -H "Authorization: Bearer sk-dev-proxy-123"` refreshes via the Docker-installed `opencode models --refresh opencode-go` CLI by default, then falls back to `opencode serve` and a built-in registry. The Docker compose mounts the host OpenCode auth/config/cache so the container sees the same connected `opencode-go` provider as your host CLI.

Makefile shortcuts: `make proxy-rebuild` (build+start), `make proxy-up`, `make proxy-down`, `make proxy-logs`

**Container details:** Single Python process (uvicorn, :4001). Separate `utls-proxy` sidecar (:8444) for Codex TLS fingerprinting. `network_mode: host` (for local Ollama access), config mounted from `local/proxy_server_config.yaml`, health check every 15s. Ollama available via `--profile local`.

## Invocation surfaces

scillm is not only a chat proxy. Pick the surface by **job**, not by “strongest model”:

| Surface | Endpoint | Use when | Project-agent collaboration |
|---------|----------|----------|----------------------------|
| **Chat** | `POST /v1/chat/completions` | One-shot reasoning, critique, classification, VLM | None — text/JSON back |
| **Exec** | `scillm exec …` or `POST /v1/scillm/exec` | Deterministic pipelines; LLM at gates; bounded headless CLI | Single artifact per node; not an interactive coding loop |
| **OpenCode serve** | `POST /v1/scillm/opencode/runs` | Bounded **coding/patch delegate**: read/grep/tools/skills in one session | **Yes** — project agent launches, validates diff/text, merges or forks retry |
| **OpenCode transport** | `POST /v1/scillm/opencode/transport/*` | Same family as serve, plus DAG parent/child, **SSE** reasoning/permissions, steer | **Yes** — course correction on long investigations |
| **Standing agents** | `/v1/scillm/agents/*` | Multi-turn **Codex** authorship in a leased worktree | **Yes** — handoff → lease → turn → result; memory stays in `/memory` |

### Why OpenCode serve (between chat and exec)

**Chat** returns one completion — no repo tool loop. **`scillm exec`** runs a **single** bounded headless worker (`codex exec`, Pi, one-shot `opencode run` with skills/shell denied in generated config) — good for pipeline **gates**, not for “investigate this repo and propose a patch.”

**OpenCode serve** is the **tier‑1.5 coding delegate**: a bounded OpenCode session with an **agent profile** (`build`, `scillm-debugger`, …), native tools, and an optional Agent Skills allowlist. The **project agent** still owns the goal, `/memory` recall, deterministic validation, and **merge authority**. The serve worker returns **evidence** (`assistant_text`, `events.jsonl`, optional `diff` under `.scillm/opencode-serve/`); harness validators and the project agent decide pass/fail — OpenCode output is not auto-merged.

Use **standing `/v1/scillm/agents/*`** when you need a **long-lived Codex** worker across many turns in an isolated worktree. Use **transport** when the harness or `/agent-debugger` needs **streaming** supervision and fork/steer (see [OpenCode transport v1](docs/SCILLM_OPENCODE_TRANSPORT_V1.md)).

**Enable and verify:**

```bash
# compose: SCILLM_OPENCODE_SERVE_ENABLED=1 (see deploy/docker/compose.scillm.core.yml)
docker compose -p scillm -f deploy/docker/compose.scillm.core.yml up -d --build opencode-serve scillm-proxy
bash scripts/sanity_opencode_serve.sh
```

**Minimal serve run** (agent profile — not `opencode-go/kimi-k2.6`):

```bash
curl -s -X POST http://localhost:4001/v1/scillm/opencode/runs \
  -H "Authorization: Bearer sk-dev-proxy-123" \
  -H "X-Caller-Skill: my-project" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"Inspect tests/test_foo.py and explain the failure. Do not edit.","agent":"build","skills":["memory","scillm"],"timeout_s":600}'
```

Full contract: [`docs/SCILLM_OPENCODE_SERVE.md`](docs/SCILLM_OPENCODE_SERVE.md). Agent/skill reference: [`skills/scillm/SKILL.md`](skills/scillm/SKILL.md).

## Exec Workers

`scillm exec` is the bounded worker layer for agentic tasks. It is separate from
ordinary one-shot `/v1/chat/completions` calls:

| Command/profile | Runner | Backing model | Notes |
|-----------------|--------|---------------|-------|
| `scillm exec pi-chutes-kimi` | `pi_exec` | Pi CLI over Chutes `moonshotai/Kimi-K2.6-TEE` | Preferred low-overhead Chutes Kimi exec lane. Uses the local Pi fork via `/home/graham/bin/pi`, which points at `/home/graham/workspace/experiments/pi-mono`, and runs with `--thinking off`. Override binary/model with `SCILLM_PI_BINARY` and `SCILLM_PI_CHUTES_KIMI_MODEL`. |
| `scillm exec pi-opencode-kimi` | `pi_exec` | Pi CLI over OpenCode Go `kimi-k2.5` | Preferred Pi route when Chutes Kimi produces empty/no-write exec output. Uses Pi's native `opencode-go` provider support and `OPENCODE_API_KEY`; override with `SCILLM_PI_OPENCODE_KIMI_MODEL`. |
| `scillm exec oc-chutes-deepseek` | `opencode_exec` | OpenCode CLI over Chutes `chutes/moonshotai/Kimi-K2.6-TEE` by default | OpenCode worker lane. Override with `SCILLM_OPENCODE_CHUTES_DEEPSEEK_MODEL`. |
| `scillm exec codex-gpt-5.5` | `codex_exec` | Codex CLI `gpt-5.5` | Bounded `codex exec --json` worker. Not the same as chat `model: "gpt-5.5"`. API accepts deprecated alias `gpt-5.5` on `codex_exec`. Override with `SCILLM_CODEX_EXEC_MODEL`, `--codex-model`, `--reasoning-effort`. |
| `scillm exec codex-vision` | `codex_exec` | Codex CLI `gpt-5.3-codex` (default) | Vision/heavy Codex exec lane. Override with `SCILLM_CODEX_EXEC_MODEL_VISION`. |
| `scillm exec cursor-auto` | `cursor_exec` | Cursor CLI `auto` | Bounded writes with `--cursor-force` and `--allow-write`. |
| `scillm exec cursor-plan` | `cursor_exec` | Cursor CLI plan mode | Read-only diagnose (`--mode plan`, no `--force`). |
| `scillm exec cursor-composer-2.5` | `cursor_exec` | Cursor CLI composer model | Profile-only; do not pass through chat completions. |
| `oc-*` and `opencode-go/*` | HTTP chat/batch routes | OpenCode Go API | These are one-shot Scillm model routes, not exec workers. |


### Exec monitoring and timeouts (chat ≠ cursor)

**Chat / batch** (`/v1/chat/completions`, batch stream): SSE heartbeat comments, caller
`timeout` budget, and automatic timeout estimation from `llm_call_log` p95 — see features
below.

**`cursor_exec`** (`scillm exec cursor-auto`, `cursor-plan`, …): progress is **stream-json
NDJSON**, not chat SSE. scillm parses stdout line-by-line, appends
`.scillm/cursor-headless/<run_ctx>/cursor-events.jsonl` during the run, resets idle on
semantic events (`tool_call`, `assistant`, `thinking`, …), and completes on a terminal
`{"type":"result",...}` (process may be terminated without waiting for exit). Monitor:

- `/tmp/scillm-exec/<run_id>/events.jsonl` or `GET /v1/scillm/exec/{run_id}/events`
- Terminal fields: `stream_completed`, `result_event`, `tool_call_count`, `text`

Use `timeout_s` / `idle_timeout_s` on the exec payload as **fail-closed backstops only** —
not as the primary scheduling signal. Full contract: [`docs/SCILLM_EXEC.md`](docs/SCILLM_EXEC.md).

Exec profiles are profile-only. Do not pass raw chat aliases such as
`chutes-kimi`, direct `chutes/...` model ids, or `opencode-go/*` ids to
`scillm exec`. For workspace mutation, use `--sandbox workspace-write` plus one
or more `--allow-write` paths; the runtime snapshots the workspace and fails the
node if Pi/OpenCode writes outside the allowlist.

Docker mounts `/home/graham/.pi/agent` into the proxy container so the local Pi
fork sees the same provider registry and auth as the host CLI.

```bash
scillm exec pi-chutes-kimi \
  --cwd /home/graham/workspace/project \
  --sandbox read-only \
  --prompt 'Inspect the bounded failure and return JSON only.'

scillm exec pi-opencode-kimi \
  --cwd /home/graham/workspace/project \
  --sandbox read-only \
  --prompt 'Inspect the bounded failure and return JSON only.'

scillm exec pi-chutes-kimi \
  --cwd /tmp/canary \
  --sandbox workspace-write \
  --allow-write allowed/ \
  --prompt 'Create allowed/result.json and return JSON proof.'
```

## Security

scillm is designed for **local development and trusted networks**. The default configuration uses:

- A static bearer token (`sk-dev-proxy-123`) — set `SCILLM_MASTER_KEY` in `.env` to override.
- `network_mode: host` — required for local Ollama access. In production, switch to port mapping (`-p 4001:4001`) behind a reverse proxy with TLS.
- API keys in `.env` — acceptable for dev. In production, use Docker Secrets, Vault, or your cloud's secrets manager.

scillm does not implement multi-tenant auth, key rotation, or per-client access control. It's designed for single-user research and engineering workflows.

For local blast-radius control, scillm can enforce optional `caller_profiles`
keyed by `X-Caller-Skill`. These are not users, teams, or virtual keys; they
are local safety rules for known callers. Profiles can restrict model
aliases/pools, deny dangerous model patterns, require `scillm_metadata` keys
such as `batch_id` and `item_id`, block tools/files/images/PDFs/streaming, and
cap provider timeout. See `registry/caller_profiles.example.yaml` for a minimal
copyable profile set.

## What You Get

Every provider scillm targets speaks OpenAI-compatible API (`/v1/chat/completions`). Provider-specific handler code is unnecessary for this use case. Adding a provider is 5 lines of YAML. The proxy handles format translation (OpenAI tools → Anthropic/Codex/Gemini native), OAuth token management, streaming SSE normalization, and fallback cascading — not a framework, just the code that does the work.

- **Tool use across all providers** — Send OpenAI-format `tools` and `tool_choice`. The proxy translates to each provider's native format: Anthropic tool blocks for Claude, flattened Responses API for Codex, native function calling for Gemini. Streaming tool call deltas work everywhere.
- **Wall-time retry budget** — Providers with hot/cold cycling return 503 now but may be back in 20 seconds. Fixed retry counts fail here. scillm retries on a clock — keep trying for 90 seconds, not 3 attempts.
- **Batch iterator with request-response pairing** — Fire 200 parallel requests, results arrive out of order. scillm's `as_completed` iterator pairs every response with its original request.
- **Opaque metadata round-trip** — Send `scillm_metadata` (any dict) in the request. The proxy strips it before the LLM sees it, then staples it back onto the response. The LLM cannot fabricate these values — use it for ArangoDB `_key` correlation in batch pipelines.
- **JSON repair inside retry loop** — LLM responses with trailing commas, missing braces, or markdown fences wrapping JSON are repaired automatically instead of wasting the call.
- **Auto multimodal routing** — Pass an image and scillm figures it out. No model selection needed — the proxy detects images in messages and reroutes to a vision model automatically.
- **VLM routing** — Images can go directly to `gpt-5.5` through Codex OAuth, to `vlm-chutes` for higher-throughput VLM work, or through the legacy `vlm` cascade. Avoid the generic `vlm` alias when Gemini quota limits matter because it starts with Gemini.
- **Gemini free-to-paid key rotation** — Gemini free and paid keys are separate groups. A 429 on the free key cascades immediately to the paid key — no wasted retries on an exhausted quota.
- **Cold-start warmup** — Chutes models that return 503 (cold) trigger a background warmup API call that posts a bounty for miners. The proxy falls through to the next deployment immediately. On startup, configured Chutes models are pre-warmed.
- **Bounded concurrency queue** — Chutes.ai has a 5-connection limit. Exceed it and you get a 429 with a 90-second penalty. scillm queues overflow instead of rejecting it. Queue timeout is 600s (10 min) — large batches drain rather than fail.
- **Batch-friendly error semantics** — Queue exhaustion returns 503 (service unavailable), not 429 (rate limit). 429s come only from upstream providers. Abuse guard is disabled for authenticated callers — no cascade failures from transient errors.
- **Automatic timeout estimation (chat/batch)** — For `/v1/chat/completions` and batch routes, scillm queries historical latency data (p95 from `llm_call_log`) and sets per-call provider budgets. For long streaming chat calls, use a short connect timeout, SSE heartbeat/idle liveness, and an explicit overall budget. Does **not** replace `cursor_exec` stream-json supervision — see [Exec monitoring](#exec-monitoring-and-timeouts-chat--cursor) above.
- **Source grounding verification** — Pass source text, scillm verifies the response is grounded using fuzzy matching, retries with progressive prompts if not.
- **Dynamic fallback chains** — For Chutes models, the ENTIRE fallback chain is built from real-time utilization data. All available models are scored by utilization + rate-limit ratio, sorted best-first, and tried in order. 429s never reach the client — the router cascades through the utilization-sorted chain automatically.
- **Fallback cascade with circuit breaker** — `text` uses Chutes DeepSeek-family fallbacks. VLM direct targets are preferred for quota-sensitive image work: `gpt-5.5` for Codex OAuth or `vlm-chutes` for Chutes VLM. The legacy `vlm` alias still starts with Gemini. 3 failures trigger a 20-second cooldown per group.
- **Non-TEE fast-fail routing** — Multi-model Chutes groups try non-TEE first (better throughput) with 1 retry, then fall through to TEE. No 8-retry stall on cold non-TEE models.
- **Native system prompts** — Claude gets an array of system blocks (matches Claude Code CLI). Codex gets the `instructions` field. No fake user-message hacks.
- **Claude PDF support** — Send PDFs via `data:application/pdf;base64,...` in `image_url` or as Anthropic-native `type:document` blocks. Both formats auto-translate.
- **5xx-specific backoff** — Server errors (503) get different retry timing than rate limits (429).
- **Gemini native file support** — Send PDFs, images, and ZIP archives via `inlineData` parts when targeting Gemini. ZIP files are auto-exploded into individual parts (text as text, binaries as native `inlineData`).
- **Ollama auto-routing** — Any locally-pulled Ollama model works without a config entry. The proxy auto-detects unknown model names and routes them to the local Ollama instance.
- **SSE streaming for all providers** — `"stream": true` works everywhere, including Claude and Codex OAuth. The proxy translates provider-specific SSE formats into OpenAI-compatible delta chunks, emits heartbeat comments while providers are silent, and enforces the caller’s overall `timeout` as the stream budget.

## Why Docker, Not `pip install`

scillm runs as a **persistent proxy service**, not a library you import. This is deliberate:

- **One process, many callers.** Every project agent, skill, and script on the machine hits the same `localhost:4001` endpoint. A pip package would mean each caller imports scillm, manages its own connections, and duplicates retry/circuit-breaker state. The proxy centralizes all of that.
- **OAuth token sharing.** Claude and Codex OAuth credentials live in `~/.claude/` and `~/.codex/`. The Docker container mounts these read-only — one token refresh serves every caller. A library would need each process to handle token management independently.
- **Provider isolation.** If Chutes goes down, the circuit breaker opens *once* in the proxy and all callers immediately cascade to DeepSeek. With a library, each process discovers the failure independently and wastes retries.
- **Config changes without restarts.** Update `proxy_server_config.yaml`, rebuild the container, done. Every caller sees the new model list immediately. No code changes, no redeployments, no version bumps.
- **No dependency conflicts.** The proxy's dependencies (httpx, openai SDK, json_repair) live inside the container. Callers only need `httpx` — they don't inherit scillm's dependency tree.

The `src/scillm/batch.py` and `src/scillm/paved/chat.py` modules are also importable as a library for advanced use cases (parallel batch iteration, source grounding) that need tighter integration than HTTP. But the standard path is `httpx.post("http://localhost:4001/v1/chat/completions")`.

## Why scillm

scillm started because cheap LLM providers (Chutes, DeepSeek) are unreliable — 503s, timeouts, rate limits with 90-second penalties. Fixed retry counts don't work when a provider might be back in 20 seconds or down for 5 minutes. scillm was built to make flaky providers reliable: wall-time retry budgets, circuit breakers, fallback cascades. The multi-provider unification came later as a natural extension.

If you already have Claude Max and Codex Pro subscriptions plus a few API keys, scillm turns them into one endpoint with zero glue code. Here's what you'd need without it:

| Capability | Without scillm | With scillm |
|------------|---------------|-------------|
| **Call Claude** | Anthropic SDK, manage OAuth tokens, translate message format, handle `system` prompt constraints | `model: "claude-sonnet-4-6"` — done |
| **Call Codex** | Custom SSE parser for chatgpt.com backend, `chatgpt-account-id` header, strip unsupported params | `model: "gpt-5.5"` — done |
| **Tool use** | Different tool format per provider (Anthropic input_schema, Codex flattened, Gemini native) | Send OpenAI-format `tools` — proxy translates |
| **Call Gemini with files** | Gemini REST API, `inlineData` parts, ZIP explosion logic, MIME detection | Attach files in messages — auto-routed |
| **Call any Ollama model** | Configure each model, manage base URL | Pull and call — auto-detected |
| **Streaming** | Different SSE format per provider, custom parsers for each | `"stream": true` — same format for all |
| **Failover** | Client-side retry logic per provider, circuit breaker state per process | Proxy handles cascade + circuit breaker |
| **Concurrency** | Per-process semaphores, risk of 429 penalties | Proxy queues globally — one semaphore |
| **JSON repair** | Retry the whole call on broken JSON | Proxy repairs and returns — no wasted call |
| **Metadata round-trip** | Track request-response correlation manually | `scillm_metadata` — stripped before LLM, returned unchanged |
| **Provider switch** | Change code in every caller | Change YAML config — callers unchanged |
| **OAuth refresh** | Each process manages token lifecycle | Proxy mounts credentials — one refresh |

**Compared to multi-provider proxies** (LiteLLM, Portkey, Helicone, OpenRouter):

| | scillm | Multi-provider proxies |
|---|---|---|
| **Subscription bridging** | Uses your Claude Max + Codex Pro subscriptions directly (OAuth) | API keys only — can't use Max/Pro subscriptions |
| **Tool use translation** | OpenAI tools → Anthropic/Codex/Gemini native format, streaming tool deltas | Pass-through only (tools must match provider format) |
| **Tenant model** | Single-user, runs locally, data stays on your machine | Multi-tenant SaaS or complex self-host |
| **File handling** | ZIP explosion, Gemini `inlineData`, Claude PDF blocks, VLM auto-routing | Pass-through only (OpenRouter has a PDF plugin) |
| **JSON repair** | Multi-stage repair loop (`json_repair` + brace trim + prose rejection) | OpenRouter has response-healing; others reject |
| **Provider count** | ~8 built-in + any OpenAI-compatible via YAML | 100–1,600 integrations |
| **Observability** | Prometheus metrics + budget headers | Full tracing dashboards, cost forecasting |
| **Performance overhead** | ~20ms (Python) — irrelevant when LLM calls take 2–30s | 0.01–8ms (Go/Rust) — matters at 5K+ RPS, not single-user |
| **Cost** | Free (your subscriptions + API keys) | Free tier + paid for volume |
| **Customization** | Full source, add middleware in Python | Configuration only |

**What scillm is not:** A general-purpose proxy platform. It has 8 providers, not 1,600. No SSO, no RBAC, no dashboards. It's a single-user tool for engineers who already pay for Claude Max and Codex Pro and want one `httpx.post()` call that works with everything — including the parts (OAuth bridging, JSON repair, file handling, VLM routing) that no multi-provider proxy offers.

## How to Call It

### `/scillm` skill

Agents and humans invoke `/scillm` the same way — no API calls, no key management, no provider selection:

```
/scillm "Explain quantum computing in one sentence"
/scillm "Analyze results/chart.png and explain the trends"
/scillm --model moonshot-text "Explain quantum computing in one sentence"
```

Reference file paths inline and scillm handles the rest — reads the file, base64-encodes it, picks a vision model, routes the request. Works across any IDE or agent that supports slash commands. Use `--model` only when you need to force a specific proxy alias such as `moonshot-text` or `text-kimi`.

### OpenAI SDK

The proxy speaks standard OpenAI. Any existing code or SDK works — just point it at `localhost:4001`:

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:4001/v1", api_key="sk-dev-proxy-123")
r = client.chat.completions.create(model="text", messages=[{"role": "user", "content": "hi"}])
```

Both paths hit the same Docker proxy at `localhost:4001`.

## Parallel Batch Completions

For high-throughput QRA/default DeepSeek work, prefer the server-side model pool endpoint. It lets scillm split work across independent Chutes and OpenCode Go lanes, enforce provider-specific concurrency centrally, and return results as each item completes.

**Discover pools:**

```bash
curl http://localhost:4001/v1/scillm/model-pools \
  -H "Authorization: Bearer sk-dev-proxy-123"
```

**Dashboard/live status:**

```bash
curl http://localhost:4001/v1/scillm/model-pools/qra-deepseek-pool/status \
  -H "Authorization: Bearer sk-dev-proxy-123"
```

The status response is the dashboard contract for pool concurrency. It returns
top-level `live_in_flight`, `actual_in_flight`, `limit`, `queued`, `available`,
and per-lane state with `live_in_flight`, `semaphore_in_flight`,
`stale_active_calls`, and `registry_drift`. Dashboard progress should use
`live_in_flight`; stale registry rows are diagnostics only. Timeout errors
include structured details such as `caller`, `batch_id`, `item_id`, `provider`,
`model`, `elapsed_ms`, `timeout_s`, `cascade_attempts`, and
`final_provider_error`.

**Submit a batch:**

```python
import httpx

resp = httpx.post(
    "http://localhost:4001/v1/scillm/batch/completions",
    headers={
        "Authorization": "Bearer sk-dev-proxy-123",
        "Content-Type": "application/json",
        "X-Caller-Skill": "create-qras",
    },
    json={
        "model_pool": "qra-deepseek-pool",
        "items": [
            {
                "item_id": "qra-001",
                "messages": [{"role": "user", "content": "Return one valid QRA JSON object."}],
                "scillm_metadata": {"batch_id": "batch-20260425", "item_id": "qra-001"},
            }
        ],
    },
    timeout=900.0,
)

for result in resp.json()["results"]:
    item_id = result["item_id"]  # join by item_id; results are completion-ordered
    served_by = result["model_served"]
```

Built-in pool:

| Pool | Strategy | Lanes |
|------|----------|-------|
| `qra-deepseek-pool` | Weighted round-robin | Chutes `deepseek-ai/DeepSeek-V3-0324-TEE` + OpenCode Go `opencode-go/deepseek-v4-flash` |

Use client-side batching only when you need a custom model set or local post-processing between calls. In that case, use `asyncio.create_task` + `asyncio.as_completed` so slow provider calls do not block completed results.

`parallel_acompletions_iter` — an async iterator that yields results as they complete, with every result carrying its original request.

```python
from scillm.batch import parallel_acompletions_iter

requests = [
    {"messages": [{"role": "user", "content": f"Summarize document {doc_id}"}],
     "metadata": {"doc_id": doc_id}}  # your data rides along
    for doc_id in document_ids
]

async for result in parallel_acompletions_iter(requests, concurrency=6):
    doc_id = result["request"]["metadata"]["doc_id"]  # paired back to your request
    if result["ok"]:
        save(doc_id, result["response"])
    else:
        retry_queue.append(doc_id)
```

Each yielded `result`:

| Field | What it is |
|-------|-----------|
| `index` | Position in original list (for ordering) |
| `request` | Your original request dict, including any metadata you attached |
| `ok` | Success boolean |
| `response` / `error` | The OpenAI response or error message |
| `attempts` | Retry count |
| `elapsed_s` | Wall-clock time |

## Tool Use (Function Calling)

Send OpenAI-format `tools` and `tool_choice` — the proxy translates to each provider's native format automatically:

```python
resp = httpx.post(
    "http://localhost:4001/v1/chat/completions",
    headers={"Authorization": "Bearer sk-dev-proxy-123"},
    json={
        "model": "claude-sonnet-4-6",  # or gpt-5.5, text-gemini, text
        "messages": [{"role": "user", "content": "What's the weather in SF?"}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather for a location",
                "parameters": {"type": "object", "properties": {"location": {"type": "string"}}}
            }
        }],
        "tool_choice": "auto",
    },
    timeout=60.0,
)
# response.choices[0].message.tool_calls → standard OpenAI tool_calls format
```

**What the proxy handles per provider:**
- **Claude**: Translates `parameters` → `input_schema`, maps `tool_choice` (auto/required/none/specific), converts tool_use blocks back to OpenAI format
- **Codex**: Flattens to Responses API format (name at top level), forces `tool_choice: "auto"` (Codex doesn't support `"required"`), adds `parallel_tool_calls: true` and reasoning fields
- **Gemini**: Native function calling — passed through

Streaming tool calls work on all providers (`"stream": true`). Tool call deltas arrive as standard OpenAI delta chunks with `tool_calls[].function.arguments` fragments.

## Opaque Metadata Round-Trip

Send `scillm_metadata` (any dict) in the request body. The proxy strips it before the LLM sees it, staples it back onto the response unchanged. The LLM cannot fabricate or hallucinate these values.

```python
resp = httpx.post(url, json={
    "model": "text",
    "messages": [{"role": "user", "content": "Assess this control..."}],
    "scillm_metadata": {"_key": "sparta_controls/12345", "stage": "S12"},
}, headers=headers)

data = resp.json()
data["scillm_metadata"]["_key"]  # → "sparta_controls/12345" — guaranteed untouched
```

Works with all providers. Use it for ArangoDB `_key` correlation in async batch pipelines where results arrive out of order.

## Source Grounding Verification

Pass a `source` field and scillm verifies the response is grounded using fuzzy token matching. If the response doesn't meet the threshold, scillm retries with progressive prompts that push the model to stick to the source.

Use cases: compliance summaries against regulatory text, legal citation checks against statutes or case law, research summaries against source papers.

```python
requests = [
    {
        "messages": [{"role": "user", "content": "Summarize AC-17 requirements"}],
        "source": "findings/ac17_control.txt",  # file path or inline text
        "grounding_threshold": 0.7,              # default 0.7
        "grounding_retries": 2,                  # default 2
    }
]

results = await parallel_acompletions(requests, source="global_source.txt")
# result["grounding_score"] → 0.85
# result["grounding_attempts"] → 1
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `source` | `str \| list[str]` | None | File path(s) or inline text. Per-request overrides batch-level. |
| `grounding_threshold` | `float` | 0.7 | Minimum fuzzy match score (0.0–1.0) to accept. |
| `grounding_retries` | `int` | 2 | Max retry attempts with progressive grounding prompts. |

Progressive retry strategy:
1. **Try 1:** Normal completion
2. **Try 2:** Appends "Base your answer strictly on the provided source text."
3. **Try 3:** Appends "Base your answer strictly on the provided source text. Quote directly from the source."

Results carry `grounding_score` (float) and `grounding_attempts` (int). Uses `rapidfuzz.fuzz.token_set_ratio` (~1ms per check).

## Architecture

**Request flow:**
```
Client → scillm :4001 (Python) → Provider API
                ↓
       utls-proxy :8444 (Codex only)
                ↓
       chatgpt.com (Chrome TLS fingerprint)
```

**Components:**
- **scillm** (Python, port 4001): Public API, request validation, JSON guard, VLM auto-routing, OAuth token injection, SSE streaming normalization, retries, circuit breakers, fallback cascades. Routes directly to providers via openai SDK.
- **utls-proxy** (Go, port 8444): TLS fingerprint proxy for Cloudflare-protected endpoints. Uses [utls](https://github.com/refraction-networking/utls) to present Chrome's TLS fingerprint, bypassing Cloudflare's JA3 blocking on `chatgpt.com`.

**Config:** `local/proxy_server_config.yaml` — single source of truth for models, providers, and fallback chains.

## Model Groups

Callers say `model: "text"` — the proxy picks the provider. When models change, update the config. Callers never change.

| Group | Provider | Model | Fallback chain |
|-------|----------|-------|----------------|
| `text` | Chutes | DeepSeek-V3 (non-TEE → V3.1-TEE) | → text-gemini → text-gemini-paid → text-deepseek |
| `text-gemini` | Google | Gemini 2.5 Flash (free key) | → text-gemini-paid → text-deepseek |
| `text-gemini-paid` | Google | Gemini 2.5 Flash (paid key) | (none) |
| `text-gemini-3` | Google | Gemini 3 Flash Preview (free) | → text-gemini-3-paid |
| `vlm` | Google | Gemini 2.5 Flash (free key) | Legacy cascade; avoid for quota-sensitive VLM work |
| `vlm-claude` | Anthropic (OAuth) | Claude Sonnet | Images + PDFs |
| `vlm-codex` | OpenAI (OAuth) | GPT-5.3 Codex | Images + PDFs |
| `vlm-chutes` | Chutes | GLM-4.6V | Higher-throughput image calls |
| `gpt-5.5` | OpenAI (OAuth) | Codex high-reasoning model via `~/.codex` | Direct text + image calls |
| `opencode-go/deepseek-v4-flash` | OpenCode Go | DeepSeek V4 Flash through OpenCode Go/Fireworks | (none) |
| `opencode-go/deepseek-v4-pro` | OpenCode Go | DeepSeek V4 Pro through OpenCode Go/Fireworks | (none) |
| `local-text` | Ollama | qwen2.5:0.5b | (none) |
| `moonshot-text` | Moonshot | Kimi K2 | (none) |
| `text-glm` | Z.AI GLM | glm-5.1 | (none) |
| Any `claude-*` | Anthropic | Auto-routed via Claude Code OAuth | (none) |
| Any `gpt-*`/`codex-*` | OpenAI | Auto-routed via Codex CLI OAuth | (none) |
| Any `gemini-*` | Google | Auto-routed to Gemini API | (none) |
| Any `Org/Model` | Chutes | Auto-routed to Chutes API | (none) |
| Any `model:tag` | Ollama | Auto-routed to local Ollama | (none) |

Auto-routing handles most model names without config entries. Claude and Codex use OAuth from `~/.claude/` and `~/.codex/` respectively. Gemini free/paid are separate groups — 429 on free cascades immediately to paid.

## Adding Your Own Models

Most providers are auto-routed by model name — see the Provider Setup table above. No config entry needed for Claude, Codex, Gemini, Chutes `Org/Model`, or Ollama `model:tag`.

For providers that need a custom API base or key, add 5 lines of YAML:

For providers that need a custom API base or key, add an entry to `local/proxy_server_config.yaml`:

```yaml
# Example: add Together.ai
- model_name: together-llama
  scillm_params:
    model: meta-llama/Llama-4-Scout-17B-16E-Instruct
    api_base: https://api.together.xyz/v1
    api_key: os.environ/TOGETHER_API_KEY
    timeout: 45
```

Then add `TOGETHER_API_KEY=...` to `.env` and rebuild:

```bash
make proxy-rebuild  # or: docker compose -p scillm -f deploy/docker/compose.scillm.core.yml up -d --build
```

Now `{"model": "together-llama", "messages": [...]}` works. Callers don't know or care which provider serves it.

**Add to fallback cascade** (optional): if Together goes down, fall back to Chutes:

```yaml
scillm_settings:
  fallbacks:
    - together-llama: [text]  # fall back to the "text" group (Chutes DeepSeek-V3)
```

**Constraint:** The provider must speak OpenAI's `/v1/chat/completions` format. Most do (Together, Groq, Fireworks, Perplexity, Mistral, etc.).

## Testing

```bash
make smokes-cli-fast    # Quality gate: proxy-only smokes
make smokes             # Full suite including Ollama preflight
make test-e2e           # 24 pytest e2e tests against live proxy
make test-adversarial   # 44 adversarial tests (auth, streaming, grounding, edge cases)
```

Test files: `tests/test_proxy_e2e.py` (contract tests), `tests/test_proxy_adversarial.py` (edge cases), `tests/test_grounding.py` (unit tests for grounding helpers).

## The `/scillm` Skill

`/scillm` is also available as a slash command (skill) in AI coding agents. Any agent skill that needs an LLM completion calls `/scillm` — one endpoint, any provider.

**Auto-routing by model name** — no config entries needed for most providers:

| Pattern | Provider | Auth |
|---------|----------|------|
| `claude-*` | Anthropic | Claude Code Max OAuth |
| `gpt-*` / `codex-*` | OpenAI Codex | ChatGPT OAuth |
| `gemini-*` | Google Gemini | API key |
| `Org/Model` | Chutes | API key |
| `model:tag` | Ollama (local) | none |

**Discover available models:** `GET /v1/scillm/providers`

**Discover capability facts:** `GET /v1/scillm/capabilities`

**Composes with:** `/task-monitor`, `/create-evidence-case`, `/analytics`, `/create-figure`, `/llm-eval-lab`

**Agent reference:** [`skills/scillm/SKILL.md`](skills/scillm/SKILL.md) — workflow map and surface picker. **Deep detail:** [`skills/scillm/references/`](skills/scillm/references/) (batch, OAuth, serve, transport, agents).

## Ops Endpoints

| Endpoint | What it tells you |
|----------|------------------|
| `GET /health/liveliness` | Is the proxy alive? |
| `GET /v1/scillm/health` | Model groups, fallback chains, retry policy, concurrency slots |
| `GET /v1/scillm/models` | Deployed models with group membership |
| `GET /v1/scillm/capabilities` | Read-only model, adapter, pool, and caller policy capability facts |
| `GET /v1/scillm/model-pools` | Server-side batch pools and lane definitions |
| `GET /v1/scillm/model-pools/{pool}/status` | Live aggregate and per-lane pool concurrency for dashboards |
| `GET /v1/scillm/opencode-go/models?refresh=true` | Live OpenCode Go model discovery |
| `GET /v1/scillm/active-calls` | Raw active-call rows for debugging; not the dashboard pool source of truth |
| `POST /v1/scillm/active-calls/purge` | Purge stale in-memory active-call rows |
| `POST /v1/scillm/batch/completions` | Server-side batch completions using `model_pool` |
| `GET /v1/scillm/opencode/health` | OpenCode serve connectivity |
| `GET /v1/scillm/opencode/agents` | OpenCode agent profiles on serve |
| `POST /v1/scillm/opencode/runs` | Bounded OpenCode coding/patch run (main serve entry) |
| `POST /v1/scillm/opencode/serve/debugger/run` | Serve run with default debugger agent |
| `GET /v1/scillm/opencode/events` | Live OpenCode SSE bus (`curl -N`) |
| `GET /v1/scillm/agents/registry` | Standing Codex workers (check `workers` length) |
| `POST /v1/scillm/agents/{worker_id}/turn` | Deliver handoff to standing worker |
| `GET /v1/models` | OpenAI-compatible model list |
| `GET /v1/budget` | Current daily spend and remaining budget |
| `GET /v1/scillm/logs` | Cost summary by model for a given date |
| `GET /metrics` | Prometheus counters (requests, errors, latency by group) |

## Logging

All LLM calls are logged to ArangoDB (`llm_call_log` collection) via the memory service. **No Redis for logging** — Redis is only for optional caching.

**JSONL backup** — every call is also appended to `/mnt/storage12tb/scillm-logs/` (or `$SCILLM_LOG_BACKUP_DIR`). This backup is independent of ArangoDB, append-only, and survives database wipes. Files are organized by month (`YYYY-MM/calls-YYYY-MM-DD.jsonl`). The Docker compose mounts this path.

Each log entry includes:
- `ts`, `date` — timestamp and date partition
- `model_requested`, `model_served` — what caller asked for vs what served
- `provider` — inferred provider (chutes, gemini, anthropic, etc.)
- `duration_ms`, `prompt_tokens`, `completion_tokens`, `cost_usd` — performance and cost
- `request_prompt` — last user message (truncated to 4000 chars)
- `response_content` — raw LLM response for debugging
- `status`, `error` — ok/error with error type if failed

Query logs via memory service:
```bash
curl -X POST http://localhost:8601/query -H "Content-Type: application/json" -d '{
  "aql": "FOR doc IN llm_call_log FILTER doc.date == \"2026-04-13\" SORT doc.ts DESC LIMIT 10 RETURN doc"
}'
```

**Silent batch failures are forbidden.** If a batch reports "0 stored" without explanation, check `llm_call_log` for raw `response_content` — it will reveal schema mismatches (e.g., LLM returns `abstain_reason` but code checks `reason`).

## License

MIT License.

## Documentation

| Doc | Audience | Contents |
|-----|----------|----------|
| [Invocation surfaces](#invocation-surfaces) (this README) | Humans onboarding | When to use chat vs exec vs OpenCode serve vs transport vs standing agents |
| [`skills/scillm/SKILL.md`](skills/scillm/SKILL.md) | Project agents / slash `/scillm` | Workflow map, surface picker, minimal examples |
| [`skills/scillm/references/`](skills/scillm/references/) | Agents (on demand) | Batch, OAuth, files, serve, transport, standing agents |
| [`docs/SCILLM_OPENCODE_SERVE.md`](docs/SCILLM_OPENCODE_SERVE.md) | OpenCode serve operators | `POST /v1/scillm/opencode/runs`, env, Docker sidecar, fork, skills allowlist, artifacts |
| [`docs/SCILLM_OPENCODE_TRANSPORT_V1.md`](docs/SCILLM_OPENCODE_TRANSPORT_V1.md) | Harness / agent-debugger | DAG parent/child, **SSE** reasoning/permissions, transport `events.jsonl` |
| [`docs/SCILLM_OPENCODE_INTEGRATION.md`](docs/SCILLM_OPENCODE_INTEGRATION.md) | Integrators | Fail-closed control plane; do not call raw serve ports from product code |
| [`docs/interactive-agents/README.md`](docs/interactive-agents/README.md) | Standing Codex workers | `/v1/scillm/agents/*` handoff → lease → turn; see [routing](docs/interactive-agents/routing.md) |
| [`docs/SCILLM_EXEC.md`](docs/SCILLM_EXEC.md) | Exec graphs | `scillm exec`, cursor stream-json, exec artifacts |

