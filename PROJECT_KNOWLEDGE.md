# Project Knowledge: scillm

**Last updated:** 2026-04-25 12:55 by agent
**Status:** Active development

## Current Understanding

- scillm is a single-tenant LLM proxy at localhost:4001
- Direct provider routing via openai SDK (Bifrost removed 2026-04-13)
- All logging via ArangoDB (`llm_call_log` collection), NOT Redis
- Redis is ONLY for optional caching
- Silent batch failures are forbidden — must log raw responses for debugging
- **JSONL backup** — all calls also written to `/mnt/storage12tb/scillm-logs/` (append-only, survives DB wipes)

### Dynamic Fallback Chain (2026-04-15)

The ENTIRE fallback chain is now built dynamically from real-time Chutes utilization data.

**How it works:**
1. `chutes_router.py` fetches utilization from Chutes API (cached 5 min)
2. Scores each model: `util% * 80` (lower = better), penalizes >25% rate-limit or >95% util
3. Sorts all discovered models by score (best first)
4. Appends static fallbacks: `text-kimi`, `text-qwen3`, `text-qwen3-large`
5. Injects full chain via `_dynamic_fallback_chain` → `app.py` → `router.py`

**Example chain** (actual output 2026-04-15):
```
['DeepSeek-TNG-R1T2-Chimera-TEE',  # score=20, util=25%
 'DeepSeek-V3.1-TEE',              # score=29, util=37%
 'DeepSeek-R1-0528-TEE',           # score=72, util=90%
 'DeepSeek-V3.2-TEE',              # score=100, saturated
 'DeepSeek-V3-0324-TEE',           # score=100, rate-limited
 'text-kimi', 'text-qwen3', 'text-qwen3-large']  # static fallbacks
```

**Why this matters:** Previously, dynamically discovered models (like Chimera-TEE) had NO fallback chain in static config → 429s reached clients. Now every call gets a full 8-model chain.

**NOT in batch chain:** OAuth providers (Codex, Claude) — risk of account ban

### Concurrency Guard Hardening (2026-04-15)

**Five fixes deployed:**

1. **Semaphore race condition** — When 429 backoff created new semaphore, `_in_flight` counter wasn't accounted for. Fix: pre-acquire slots for in-flight requests.

2. **Provider resolution** — `deepseek-ai/DeepSeek-V3.1-TEE` was substring-matching "deepseek" → wrong limit (8 vs 4). Fix: check for "/" first → routes to chutes.

3. **Background stale cleanup** — Ran only in `pre_call`. If queue full, no pre_calls, no cleanup → zombies persist forever. Fix: background task runs every 30s independent of request flow.

4. **Mandatory X-Caller-Skill** — Unknown callers created untraceable zombie requests. Fix: 400 error with helpful message if header missing.

5. **Reset endpoint** — `POST /v1/scillm/concurrency/reset` clears stuck queues without restart.

**Usage:**
```bash
# Check status
curl -H "Authorization: Bearer sk-dev-proxy-123" \
  "http://localhost:4001/v1/scillm/concurrency?model=text"

# Reset if stuck
curl -X POST -H "Authorization: Bearer sk-dev-proxy-123" \
  "http://localhost:4001/v1/scillm/concurrency/reset"
```

### Batch Reliability Hardening (2026-04-15)

**Goal:** Project agents should NEVER see 429 errors — scillm handles all rate limiting internally.

**Seven fixes deployed:**

1. **Abuse guard disabled** — Authenticated callers are legitimate. The abuse guard was blocking batch callers after transient provider errors, causing ALL subsequent requests to fail. Now disabled: `pre_call()` returns immediately without checking blocked clients.

2. **Queue timeout returns 503 (not 429)** — Semantically correct: 429="you're sending too fast" vs 503="service overloaded". Queue exhaustion is capacity saturation, not rate limiting. Error message now references SKILL.md chunking docs.

3. **Queue timeout extended to 600s** — Was 60s which caused batches of 100+ to fail. Now 10 minutes — allows deep queues to drain.

4. **Queue rejection disabled** — `QUEUE_REJECT_THRESHOLD=0` and `MAX_QUEUE_PER_PROVIDER=0`. All requests queue indefinitely rather than rejecting with 429.

5. **Zombie slot cleanup reduced to 90s** — Was 300s which caused zombie slots to persist 4+ minutes during batches. Now matches realistic timeouts.

6. **Background cleanup auto-restart** — Cleanup task could die silently leaving zombies forever. Now auto-restarts up to 3 times.

7. **asyncio.Lock in active_calls.py** — Was using `threading.Lock` which blocks the event loop. Now uses `asyncio.Lock` with proper `async with` syntax.

**Error semantics now:**
| Scenario | Status | Meaning |
|----------|--------|---------|
| Provider rate limit (429 from upstream) | 429 | Proxy retries internally via fallback chain |
| Queue timeout after 600s | 503 | Service unavailable — batch too large for capacity |
| Invalid request | 400 | Bad request format |

**The only remaining failure mode:** 503 after 600s queue wait. This means the batch is too large for available capacity. Fix: use chunked processing (CHUNK_SIZE=4) per SKILL.md.

### Server-side DeepSeek Model Pool (2026-04-25)

Large QRA/default DeepSeek batches should use the server-side pool endpoint instead of manually splitting work across providers.

**Endpoint:** `POST /v1/scillm/batch/completions`

**Pool:** `qra-deepseek-pool`

| Lane | Provider | Model | Weight | Max Concurrency | Timeout |
|------|----------|-------|--------|-----------------|---------|
| `chutes-deepseek` | Chutes | `deepseek-ai/DeepSeek-V3-0324-TEE` | 3 | 5 | 420s |
| `opencode-go-deepseek-v4-flash` | OpenCode Go | `opencode-go/deepseek-v4-flash` | 2 | 4 | 620s |

**Behavior:**
- Uses weighted round-robin assignment, then runs all items with `asyncio.create_task` + `asyncio.as_completed`.
- Returns results in completion order, not input order. Join results by `item_id`.
- Adds `scillm_metadata.batch_id`, `item_id`, `model_pool`, `lane`, `selected_model`, and `provider` to each inner call.
- This is for throughput across independent provider pools, not quality evaluation. Use `/llm-eval-lab` when every prompt must hit every model.
- Do not model this as fallback. Fallback improves reliability after failure; provider-pool batching raises throughput immediately.

**Discovery:** `GET /v1/scillm/model-pools`

**Dashboard status:** `GET /v1/scillm/model-pools/qra-deepseek-pool/status`

Use the pool status endpoint as the source of truth for dashboards. It returns aggregate `in_flight/limit/queued/available` plus per-lane Chutes/OpenCode Go state and drift fields. Raw `GET /v1/scillm/active-calls` is only a debugging view.

**OpenCode Go JSON contract:** DeepSeek/MiniMax OpenCode Go models use the Anthropic-compatible `/messages` endpoint. OpenAI `response_format` is not native there, so `/scillm` translates `response_format={"type":"json_object"}` and JSON schema response formats into provider-boundary JSON-only instructions in both the system prompt and final user turn while preserving all system messages.

**OpenCode Go multimodal:** As of April 26, 2026, `opencode models opencode-go --verbose` reports DeepSeek V4 Flash/Pro with `attachment=false`, `input.image=false`, and `input.pdf=false`. `/scillm` treats `opencode-go/deepseek-v4-*` and `opencode-go/minimax-*` as text-only lanes and rejects image/PDF content early with guidance to use `vlm`, Gemini, Claude, or Codex VLM paths.

**Current empirical basis:** On `prompt_cwe20_ex0002`, OpenCode Go `deepseek-v4-flash` matched `deepseek-v4-pro` on the current QRA scorer (`0.933`) and was faster than Pro (`135.01s` vs `217.8s`), while Chutes was faster (`80.72s`) and semantically comparable. The pool uses Chutes as the larger lane and OpenCode Go Flash as additional independent capacity.

### Codex OAuth gpt-5.5 Support (2026-04-25)

`gpt-5.5` is supported through the Codex OAuth path (`~/.codex/auth.json`) and is explicitly configured in `local/proxy_server_config.yaml`.

**Bug fixed:** app-level model validation was rejecting `gpt-5.5` before the router's `gpt-* | codex-*` auto-routing path could create a Codex OAuth group. Validation now allows Codex-prefixed model IDs when Codex OAuth credentials are available, and `/v1/scillm/models` advertises the explicit `gpt-5.5` group.

**Live smoke:** `POST /v1/chat/completions` with `model: "gpt-5.5"` returned HTTP 200 and content `OK` after the Docker proxy rebuild.

### Docker Deployment Strategy (2026-04-15)

**Target audience:** Power users only — engineers who need deep LLM proxy customization. Not for average users.

**Two deployment modes:**

| Mode | Compose File | Use Case |
|------|--------------|----------|
| **Standalone** | `compose.scillm.standalone.yml` | External users — self-contained, includes all services |
| **Core** | `compose.scillm.core.yml` | Internal use — assumes memory service on host |

**Services in standalone:**
- `arangodb` (:8529) — Database
- `memory` (:8601) — Logging, batch resume, latency stats
- `embedding` (:8602) — Sentence embeddings for `/v1/embeddings`
- `utls-proxy` (:8444) — TLS fingerprint for Codex
- `scillm-proxy` (:4001) — Main LLM gateway

**Source management (decided 2026-04-15):**
- Memory service source lives at `/workspace/experiments/memory/` (active development)
- Copy to `scillm/services/memory/` when releasing (manual sync)
- **Why not published images:** Both projects under active development — CI overhead and version coordination friction slow iteration
- **Why not git submodule:** Clone complexity, detached HEAD headaches
- **When to revisit:** Move to published images when memory API stabilizes (<1 change/month)

**Sync workflow:**
```
/workspace/experiments/memory/  →  (manual sync)  →  scillm/services/memory/
         (source of truth)                              (distribution snapshot)
```

**Key files:**
- `services/memory/` — Copy of memory service for standalone deploy
- `services/embedding/` — Embedding service (PyTorch + sentence-transformers)
- `deploy/docker/compose.scillm.standalone.yml` — Full stack compose
- `deploy/docker/compose.scillm.core.yml` — Minimal compose (proxy only)

## Recent Decisions

| Date | Decision | Why |
|------|----------|-----|
| 2026-04-15 | Disable abuse guard for authenticated callers | Was blocking batch callers after transient errors → cascading failures. Authenticated callers with master key are legitimate. |
| 2026-04-25 | Add `qra-deepseek-pool` server-side batch endpoint | Raises large-batch throughput by splitting work across independent Chutes and OpenCode Go lanes using `as_completed`, not fallback. |
| 2026-04-25 | Add live model-pool status and drift accounting | Dashboards use `/v1/scillm/model-pools/{pool}/status`; active-call cleanup is TTL-backed and drift is explicit. |
| 2026-04-25 | Prefer OpenCode Go `deepseek-v4-flash` over `deepseek-v4-pro` for batch lane | Same current QRA score as Pro on test prompt, materially faster; Pro remains a quality spot-check option. |
| 2026-04-25 | OpenCode Go `/messages` must not default `max_tokens=4096` | The default cap caused hidden/reasoning token exhaustion and empty visible output; omit unless explicitly requested. |
| 2026-04-25 | Add `gpt-5.5` Codex OAuth support | Orchestration requested `gpt-5.5`; validation rejected it before router auto-routing. Added explicit config group, validation allowance, discovery/docs, and live smoke. |
| 2026-04-15 | Queue timeout returns 503 (not 429) | 429="you're sending too fast" vs 503="service overloaded". Queue exhaustion is capacity saturation, not rate limiting. |
| 2026-04-15 | Extend queue timeout from 60s to 600s | Short timeout caused batches of 100+ to fail. 10min allows deep queues to drain. |
| 2026-04-15 | Disable queue rejection (always queue) | QUEUE_REJECT_THRESHOLD=0. No upfront 429s — requests wait in queue instead of immediate rejection. |
| 2026-04-15 | Use asyncio.Lock in active_calls.py | threading.Lock blocks event loop. Async middleware must use async primitives. |
| 2026-04-15 | Mandatory X-Caller-Skill header | Requests without header rejected with 400 + helpful error. Prevents untraceable zombie requests from clogging queue. |
| 2026-04-15 | Background stale slot cleanup (30s) | Runs independent of request flow. Fixes: when queue full, no pre_calls run, so stale detection never triggered. Now zombies auto-cleaned. |
| 2026-04-15 | Reset endpoint `/v1/scillm/concurrency/reset` | Clears stuck queues without container restart. Returns slots_cleared, queue_cleared, pauses_cleared. |
| 2026-04-15 | Fix provider resolution for Org/Model format | `deepseek-ai/DeepSeek-V3.1-TEE` was matching "deepseek" substring → wrong limits (8 vs 4). Now checks "/" first → routes to chutes. |
| 2026-04-15 | Fix semaphore race condition in concurrency_guard.py | Pre-acquire slots for in-flight requests when creating new semaphore during backoff. Fixes "9/8 slots" error causing cascading batch failures. |
| 2026-04-15 | Standalone Docker deployment with bundled services | Power users get self-contained `docker compose up`. Memory service copied (not published image) because both projects under active development. |
| 2026-04-15 | Dynamic fallback chains from Chutes utilization | Entire chain built at runtime, sorted by utilization score. Fixes 429s reaching clients from dynamically discovered models. |
| 2026-04-17 | JSONL backup to /mnt/storage12tb/scillm-logs/ | Agent wiped 14GB ArangoDB — needed DB-independent backup. Append-only, daily files, monthly dirs. |
| 2026-04-14 | CWE batch uses 6 Chutes models only | No OAuth (ban risk), no external DeepSeek (paid), no Gemini. All models verified 100% QRA grounding. Qwen3.5-397B is slowest last resort. |
| 2026-04-13 | Remove Bifrost gateway | Was never enabled (BIFROST_ENABLED=false). Direct openai SDK routing is simpler. ArangoDB llm_call_log provides all monitoring data. |
| 2026-04-13 | Build scillm batch dashboard in ux-lab | React components using EmbryStyle/NVIS tokens, queries ArangoDB for batch progress, per-skill usage, error rates |
| 2026-04-13 | Add caller_info fallback when x-caller-skill missing | Logs user_agent and other headers when skill header absent — helps identify unknown callers |
| 2026-04-13 | Fixed deprecated model refs in 4 skills | scillm/prove.py, ingest-audiobook, review-music, ingest-movie were using deprecated deepseek-ai/DeepSeek-V3 |
| 2026-04-13 | Automatic batch resume via scillm_metadata | Skills pass batch_id + item_id; scillm auto-skips completed items on retry |
| 2026-04-13 | Skill identification via x-caller-skill header | Per-skill usage tracking and error correlation in llm_call_log |
| 2026-04-13 | Add raw response logging to arango_log.py | Batch failures (0 stored) were impossible to debug without seeing LLM responses |
| 2026-04-13 | Clean up legacy Redis logs (75K entries) | Duplicate logging — all logging now via ArangoDB only |
| 2026-04-13 | Document misuse patterns in SKILL.md | Schema mismatches, silent failures, Redis logging are now documented anti-patterns |
| 2026-04-10 | Bifrost P1+P2 architecture | Go gateway for performance + Python for API translation |
| 2026-04-05 | Use fallbacks instead of priority field | litellm ignores model_info.priority — was silently broken |

## Open Questions

- [x] Why did batch store 0/1075 QRAs? → Schema mismatch (`reason` vs `abstain_reason`)
- [ ] Why wasn't caching preserving failed batch responses?

## Key Files

| File | Purpose |
|------|---------|
| `src/scillm/proxy/app.py` | Main FastAPI proxy; includes `/v1/scillm/batch/completions` and `/v1/scillm/model-pools` |
| `src/scillm/proxy/providers/opencode_go.py` | OpenCode Go routing seam, live model parsing, `/messages` adapter without default `max_tokens` |
| `chutes/middleware/arango_log.py` | Logs every LLM call to ArangoDB + JSONL backup (dual write) |
| `chutes/middleware/batch_resume.py` | Checks ArangoDB for completed work items (automatic batch resume) |
| `chutes/middleware/json_guard.py` | JSON validation and repair |
| `chutes/middleware/concurrency_guard.py` | Provider-aware semaphore (chutes=4, ollama=1) |
| `local/proxy_server_config.yaml` | Single source of truth for models/providers |
| `docs/dynamic-fallback-chain-walkthrough.html` | Visual walkthrough of dynamic fallback chain architecture |
| `deploy/docker/compose.scillm.standalone.yml` | Self-contained compose (all services bundled) |
| `deploy/docker/compose.scillm.core.yml` | Minimal compose (assumes services on host) |
| `deploy/docker/Dockerfile.scillm` | Single-stage Python image (Bifrost removed) |
| `services/memory/` | Memory service copy for standalone deploy |
| `services/embedding/` | Embedding service (sentence-transformers) |
| `~/.pi/skills/scillm/SKILL.md` | Skill documentation with misuse patterns |
| `.archive/bifrost/` | Archived Bifrost code (removed 2026-04-13) |

## Misuse Patterns (Forbidden)

| Pattern | Why | Fix |
|---------|-----|-----|
| Silent batch failures | "0 stored" with no explanation wastes hours | Log first failure with expected vs actual schema |
| Schema mismatch | Checking wrong field names (e.g., `reason` vs `abstain_reason`) | Log raw `response_content` to `llm_call_log` |
| Redis for logging | Duplicate logging, wrong tool | Use ArangoDB via `arango_log.py` only |
| max_tokens | Causes truncated output | Never set it — auto-stripped |
| Fire-all-at-once batching | >4 requests causes queue timeout | Use CHUNK_SIZE=4 loop |
| Manual Chutes/OpenCode splitting for QRA throughput | Reimplements scheduling inconsistently across agents | Use `POST /v1/scillm/batch/completions` with `qra-deepseek-pool` |
| Deprecated model names | `deepseek-ai/DeepSeek-V3` triggers abuse guard | Use aliases (`text`, `vlm`) not direct model names |
| Missing x-caller-skill | Can't debug which skill caused errors | Add header; fallback logs user_agent only |

## Infrastructure State

**Internal (core compose):**
- **scillm proxy:** localhost:4001 (Docker, network_mode: host, direct provider routing)
- **utls-proxy:** localhost:8444 (TLS fingerprint for Codex)
- **Memory service:** localhost:8601 (external, from `/workspace/experiments/memory/`)
- **Embedding service:** localhost:8602 (external)
- **ArangoDB:** localhost:8529 (external)

**External users (standalone compose):**
- All services bundled in one `docker compose up`
- Services communicate via Docker network (service names as hostnames)
- ArangoDB data persisted in named volume

**Redis:** NOT used by scillm (embry-redis is for PCP system metrics only)
