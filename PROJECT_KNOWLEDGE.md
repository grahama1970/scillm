# Project Knowledge: scillm

**Last updated:** 2026-06-17 by agent
**Status:** Active development

## Current Understanding

- 2026-06-17 **Direct Chutes passthrough routes added:** New routes at `/v1/scillm/chutes/completions`, `/v1/scillm/chutes/batch`, and `/v1/scillm/chutes/models` bypass the entire middleware stack. Motivation: the 11-layer middleware chain (ChutesRouter, ConcurrencyGuard, TimeoutEstimator, CallerPolicy, JsonGuard, GroundingGuard, SchemaGuard, CircuitBreaker, BudgetGuard, CostHeader, ArangoLog) compounded failures to ~60%. Each layer added to fix a specific bug, but together they reject healthy Chutes requests before they reach the API. The direct route calls `httpx.AsyncClient` directly to `POST https://llm.chutes.ai/v1/chat/completions` — same as curl. Model availability checked via `GET /v1/models` (inference API), not management utilization API. Batch uses `asyncio.Semaphore` held only during the HTTP call (not backoff sleep) + `asyncio.as_completed`. No `max_tokens` forwarded. `skills/scillm/SKILL.md` updated with usage docs.

- 2026-06-02 **OpenCode transport L6 retry/fork/abort/timeout/blocked unavailable-model/missing-runtime/denied-permission proven:** Retry/fork live canary `.plan-iterate/opencode-transport-retry-fork-canary/run-1780433841/` (`transport_run_id=otr-9d0a40ef68bc`) used `agent_id=patch-worker`, wrote invalid attempt-1 evidence (`status=bad`), failed the project-agent validation gate, retried with `fork_supersede=true`, preserved attempt 1 as `delivery_state=superseded`, completed attempt 2 as `agent_id=patch-worker`, and final evidence `evidence/retry-canary.json` was `{"schema":"scillm.retry_canary.v1","attempt":2,"status":"ok","recovered":true}`. Abort live canary `.plan-iterate/opencode-transport-abort-canary/run-1780435278/` (`transport_run_id=otr-ad64035e0394`) observed `evidence/abort-started.json`, called transport abort, preserved terminal `delivery_state=aborted`, `active=false`, empty `active_subagent_run_id`, emitted `message.aborted`, did not emit `message.completed`, and did not write `evidence/abort-finished.json`. Timeout live canary `.plan-iterate/opencode-transport-timeout-canary/run-1780435998/` (`transport_run_id=otr-b21110315484`) wrote `evidence/timeout-started.json`, exceeded the 25-second transport budget during foreground sleep, preserved terminal `delivery_state=timed_out`, `active=false`, empty `active_subagent_run_id`, emitted `message.timed_out`, did not emit `message.completed` or `message.failed`, and did not write `evidence/timeout-finished.json`. Blocked-substrate live canary `.plan-iterate/opencode-transport-blocked-canary/run-1780436585/` (`transport_run_id=otr-c1e826695e9e`) deliberately requested unsupported `model=gpt-5.5-pro`, preserved terminal `delivery_state=blocked`, `active=false`, empty `active_subagent_run_id`, emitted `message.blocked`, did not emit `message.completed` or `message.failed`, preserved provider error details with a concrete model-related blocked reason, and did not write `evidence/blocked-should-not-run.json`. Missing-runtime live canary `.plan-iterate/opencode-transport-missing-runtime-canary/run-1780437756/` (`transport_run_id=otr-0bc6807bae83`) forced a nonexistent command, preserved terminal `delivery_state=blocked`, `active=false`, empty `active_subagent_run_id`, emitted `message.blocked`, did not emit `message.completed` or `message.failed`, preserved `blocked_reason=command_not_found`, and did not write the fallback marker. Denied-permission live canary `.plan-iterate/opencode-transport-permission-denied-canary/run-1780439148/` (`transport_run_id=otr-147591eb4bf2`) used a per-workspace OpenCode agent with `bash: ask`, scillm emitted synthetic `permission_requested` via pending-permission polling, scillm rejected through `/permission/reply`, preserved terminal `delivery_state=blocked`, emitted `message.blocked`, did not emit completed/failed/timed_out, and did not write `evidence/permission-denied-should-not-exist.json`. Retry worker messages used OpenCode `providerID=openai`, `modelID=gpt-5.5`. This proves single-worker retry/fork, abort, timeout recovery, unavailable-model blocked classification, missing-runtime blocked classification, unwritable-workspace blocked classification, and denied-permission blocked classification. Multi-node DAG amendment and longer campaign loops are covered by the newer entries below.

- 2026-06-02 **OpenCode transport real-skill worker canary passed:** Live canary `.plan-iterate/opencode-transport-real-skills-canary/run-1780439490/` (`transport_run_id=otr-364a1b7be2b1`) created a streamed `patch-worker` child with selected skills `["code-runner","test"]` and copied only those two `SKILL.md` contracts into the isolated workspace. The worker explicitly read the selected skill files, patched `src/task_spec_helpers.py`, wrote `agent_outputs/real_skills_result.json`, ran `python -m pytest -q tests`, and returned `PATCH_APPLIED`. The seeded tests failed before delegation (`initial_pytest_returncode=1`) and passed afterward (`final_pytest_returncode=0`). The receipt records schema `scillm.opencode_transport_real_skills.v1`, `skill_loaded_by=explicit_worker_read`, exact selected skill source paths, changed files, `terminal_status=passed`, and empty blocker. Transport state preserved `agent_id=patch-worker`, OpenCode `agent=build`, `mode=workspace_write`, `delivery_state=completed`, and `skills_missing=[]`. This proves selected real-skill delivery for `code-runner`/`test` without scanning all skills; broader `dogpile`/`create-evidence-case`-style skill coverage remains open.

- 2026-06-02 **OpenCode transport multi-node amendment canary passed; root-owned workspace substrate fixed:** Live proof `.plan-iterate/phased-plan-agentic-example/run-1780440345/` created a project-agent phased plan, compiled it to `harness_turn.dag.v1`, lowered it to `scillm.exec.graph.v1`, and executed OpenCode transport workers through the Orchestrator. Attempt 1 intentionally omitted `code_review_gate`; audit returned `goal_met=false` and `decision=not_met_update_plan_and_resubmit`. Attempt 2 amended the plan, preserved concurrent fanout (`explore_context`, `prompt_contract_check`) plus sequential joins (`patch_worker_write` then `code_review_gate`), and returned `goal_met=true`. Worker evidence included `explorer`, `prompt-reviewer`, `patch-worker`, and `code-reviewer`; read-only workers returned assistant text, and `patch-worker` wrote `attempt-2/workspaces/patch_worker_write/agent_outputs/patch_worker_write.json` owned `graham:graham`. A failed rerun before the fix exposed that scillm exec graph helper commands can run as `root`, create root-owned bind-mounted workspaces, and cause OpenCode sidecar user `graham` to block with `permission_denied`. `src/scillm/harness/opencode_transport_worker_task.py` now normalizes isolated workspace ownership/write bits for UID/GID 1000 before dispatch. Regression proof: `PYTHONPATH=src python -m pytest -q tests/test_phased_plan_agentic_example.py tests/test_worker_agents.py::test_compile_semantic_dag_opencode_transport_worker_task tests/test_opencode_transport.py tests/test_opencode_serve_api.py tests/test_patch_delegate_receipt.py` passed with `87 passed, 1 warning`. Broader `dogpile`/`create-evidence-case` skill canaries are still useful but no longer block the multi-node amendment or Level 7 campaign proof.

- 2026-06-02 **OpenCode transport Level 7 campaign canary passed; streaming is mandatory for campaign control:** Live proof `.plan-iterate/opencode-transport-level7-campaign/run-1780441159/` (`transport_run_id=otr-085efcba65c2`) ran a bounded long-campaign shape with one `attempts.jsonl` ledger. Attempt 1 wrote invalid `campaign/retry.json` and failed project-agent validation; attempt 2 used `fork_supersede=true`, rewrote valid evidence, and passed validation. Streaming timeout probe ended `delivery_state=timed_out` with `campaign/timeout-finished.json` absent. Streaming abort probe wrote `evidence/abort-start.json`, was aborted through transport, ended `delivery_state=aborted`, and `campaign/abort-finished.json` stayed absent. Blocked probe forced `definitely_missing_scillm_level7_runtime --version` and ended `delivery_state=blocked`, `blocked_reason=command_not_found`, and no forbidden marker. The first Level 7 attempts exposed that blocking `POST .../message` is not a reliable campaign control surface for timeout/abort-sensitive work: it can outlive client timeout or classify too late. Long-running campaign and `$hack`-like loops must use scillm transport streaming plus planner-owned attempt ledgers and terminal audit. Regression proof: `PYTHONPATH=src python -m pytest -q tests/test_opencode_transport_campaign_canary.py tests/test_phased_plan_agentic_example.py tests/test_worker_agents.py::test_compile_semantic_dag_opencode_transport_worker_task tests/test_opencode_transport.py tests/test_opencode_serve_api.py tests/test_patch_delegate_receipt.py` passed with `89 passed, 1 warning`.

- 2026-06-02 **OpenCode compact health CPU spike fixed:** Live sampling showed `GET /v1/scillm/opencode/health` still called OpenCode `/agent` even when `full=false`; that catalog call caused OpenCode to spawn git/config work and burn roughly 30-300% CPU despite returning `status=ok`. `src/scillm/proxy/opencode_serve_api.py` now uses a static built-in agent list for compact health (`agent_catalog_source=static_default`) and reserves expensive `/agent` discovery for `?full=true` or `/opencode/agents`. Regression proof: `PYTHONPATH=src python -m pytest -q tests/test_opencode_serve_api.py::test_opencode_health_is_compact_by_default tests/test_opencode_serve_api.py::test_opencode_health_full_includes_agent_catalog` passed. Live post-restart proof: compact health returned `agent_catalog_source=static_default`; final `docker stats` samples for `scillm-opencode-serve-1` were `0.84%`, `2.10%`, `0.68%`, `2.09%`, and `1.02%` CPU.

- 2026-06-02 **OpenCode blocked-substrate hardening:** OpenCode `info.error` payloads are now terminal `blocked` delivery states in both blocking and streaming transport paths. Blocking `post_message_sync()` raises HTTP 502 `blocked_substrate` with `failure_type=provider_error`, provider details, and `terminal_result.delivery_state=blocked`; streaming transport emits terminal `message.blocked` and does not emit false `message.completed` or generic `message.failed`. Regression tests include `test_post_message_sync_fails_closed_on_opencode_message_error` and `test_transport_message_stream_marks_provider_error_blocked`; broad suite passed with `81 passed, 1 warning`.

- 2026-06-02 **OpenCode serve unwritable-workspace patch delegate proven blocked:** Live canary `.plan-iterate/opencode-serve-unwritable-canary/run-1780436956/` (`run_id=oc-18cd71954499`) created a read-only workspace, asked a live OpenCode `build` worker to edit `src/calc.py`, and verified `src/calc.py` stayed unchanged (`return a - b`). scillm returned `PATCH_DELEGATE_BLOCKED` with `patch_delegate_reason=permission_denied`, `patch_delegate.substrate_reason=permission_denied`, `receipt_has_concrete_blocker=true`, and `diff_count=0`. `src/scillm/proxy/opencode_serve_api.py` now feeds assistant text through `classify_patch_delegate_result()` so no-diff patch delegates with concrete blocker text surface the real substrate reason instead of vague `no_patch_delta`. Regression tests include `test_pdf_lab_patch_delegate_surfaces_concrete_blocker`; broader serve/transport suite passed with `79 passed, 1 warning`.

- 2026-06-02 **OpenCode transport unwritable-workspace worker proven blocked:** Live canary `.plan-iterate/opencode-transport-unwritable-canary/run-1780437472/` (`transport_run_id=otr-ac4bf597c322`) created a read-only workspace, dispatched streamed `agent_id=patch-worker`, and verified `src/calc.py` stayed unchanged (`return a - b`). The child ended `delivery_state=blocked`, `active=false`, empty `active_subagent_run_id`, `message.blocked=true`, `message.completed=false`, `message.failed=false`, `blocked_reason=permission_denied`, and receipt classifier evidence present. `src/scillm/proxy/opencode_transport.py` and `src/scillm/proxy/opencode_transport_stream.py` now classify concrete worker-reported blockers before completing `workspace_write` children, so `PATCH_DELEGATE_BLOCKED - permission denied...` cannot be marked completed merely because assistant text exists. Regression tests include `test_post_message_sync_blocks_on_concrete_worker_blocker` and `test_transport_message_stream_blocks_on_concrete_worker_blocker`; broader transport/serve suite passed with `81 passed, 1 warning`.

- 2026-06-02 **OpenCode transport missing-runtime worker proven blocked:** Live canary `.plan-iterate/opencode-transport-missing-runtime-canary/run-1780437756/` (`transport_run_id=otr-0bc6807bae83`) dispatched streamed `agent_id=patch-worker` and forced `definitely_missing_scillm_runtime_1780437472 --version`. The child ended `delivery_state=blocked`, `active=false`, empty `active_subagent_run_id`, `message.blocked=true`, `message.completed=false`, `message.failed=false`, `blocked_reason=command_not_found`, receipt classifier evidence present, and no fallback marker was written. No code patch was needed because transport blocker classification from the unwritable-workspace hardening already covered command-not-found worker text.

- 2026-06-02 **OpenCode abort race hardening:** Live abort canaries exposed two transport bugs: OpenCode can return `MessageAbortedError` after cancellation, and stale in-flight message paths could overwrite persisted `aborted` with `completed`. `src/scillm/proxy/opencode_transport.py` now treats `MessageAbortedError` as terminal `aborted`, and `_update_child()` refuses to overwrite persisted terminal `aborted`/`superseded` child states. Regression tests include `test_post_message_sync_classifies_opencode_aborted_error_as_aborted`, `test_post_message_sync_preserves_abort_race_terminal_state`, and `test_update_child_does_not_overwrite_persisted_abort`; broad suite passed with `78 passed, 1 warning`.

- 2026-06-02 **OpenCode timeout hardening:** Live timeout canaries exposed that the streaming transport could wait for in-flight OpenCode `send_message()` after the stream deadline and leave an active `failed` child. `src/scillm/proxy/opencode_transport.py` now has terminal `timed_out` classification and `src/scillm/proxy/opencode_transport_stream.py` marks/aborts/cancels timed-out workers at the stream deadline, emits `message.timed_out`, and closes without false `message.completed`. Regression tests include `test_post_message_sync_marks_wait_idle_timeout_terminal` and `test_transport_message_stream_marks_timeout_terminal`; broad suite passed with `80 passed, 1 warning`.

- 2026-06-02 **OpenCode OAuth model fail-closed fix:** A live retry canary first exposed that OpenCode could route worker messages to unsupported `gpt-5.5-pro` despite parent observation showing `gpt-5.5`. scillm transport now sanitizes `SCILLM_OPENCODE_TRANSPORT_PARENT_MODEL=gpt-5.5-pro` to `gpt-5.5`, defaults worker messages through `SCILLM_OPENCODE_TRANSPORT_WORKER_MODEL` or sanitized `gpt-5.5`, and treats returned OpenCode `info.error` payloads as terminal `blocked_substrate` evidence with concrete provider details. Regression tests: `tests/test_opencode_transport.py::test_parent_ui_model_sanitizes_unsupported_oauth_pro` and `::test_post_message_sync_fails_closed_on_opencode_message_error`.

- 2026-06-02 **OpenCode transport worker catalog hardening:** `patch-worker` resolves to OpenCode `agent=build`, `mode=workspace_write`; read-only `code-reviewer` resolves to `agent=explore`, `mode=propose_patches`; transport auto-child creation from `agent_id` must not override catalog role/agent/mode. Fork/supersede now preserves `agent_id` and event records can carry worker identity. Regression tests include `test_fork_supersede_preserves_active_child_agent_id`; broad checks passed with `73 passed, 1 warning`.

- 2026-06-02 **Docker context hygiene fixed for scillm-proxy rebuilds:** A failed rebuild attempt transferred 17.5 GB because `.dockerignore` only excluded `.scillm` while the image uses `COPY . /app`. `.dockerignore` now excludes VCS data, run artifacts, agent worktrees, caches, `.skills`, `local`, `reviews`, `examples`, and similar non-runtime bulk. Follow-up rebuild transferred a 17.96 MB proxy context and completed successfully. For bind-mounted source edits under `src`, a `docker restart scillm-scillm-proxy-1` remains the quickest reload path.

- 2026-05-28 **Harness phases 01–04 ACCEPTED; phase 05 next:** Ledgers accepted through `phase-04-skill-adapters`. Slices proven via `run_phase_{025,03,04}_e2e_gates.sh` (Codex-free on 025/04 proof path). **Not proven:** full `turn_loop` or end-to-end harness (phases 05–06). See `local/HANDOFF.md`.

- 2026-05-28 **$plan-iterate memory-harness-v2 (ACTIVE):** Cross-phase continuation contract in force — do not stop between phases waiting for human "proceed". Controller stops only on `BLOCKED`, `HUMAN_REQUIRED`, `MAX_ITERATIONS_REACHED`, or `OVERALL_COMPLETE`. Every phase requires live e2e (`scripts/run_phase_*_e2e_gates.sh`) + cumulative `.plan-iterate/plan-create-report.md`. Plan graph: `.plan-iterate/plans/memory-harness-v2/plan-graph.json` (phases 01 → 015 → 02 → 2.5 → 03–07).
- 2026-05-28 **Phase 01 ACCEPTED:** Memory harness write contract — schema validation on `/upsert`, `/edges/upsert`, `/edges/materialize-turn`. Live gate: `./scripts/run_phase_01_e2e_gates.sh` → `memory/tests/health/test_harness_write_e2e.py`. Fixed deployed `embry-memory` bug: missing `validate_harness_document` import in `memory/src/graph_memory/service/app/_core.py` (bind-mount `memory/src`).
- 2026-05-28 **Phase 1.5 IMPLEMENTATION GREEN:** Coding delegation proof — `src/scillm/harness/coding_delegation.py` + live `./scripts/run_phase_015_e2e_gates.sh` PASS (~56s). Fixes: harness_turn `idempotency_key` sha256 format, `outcome` enum (`ok` not `success`), recall via `/recall/by-keys` not BM25 `/recall`. Ledger: `ready_for_review` — close with package → scillm gpt-5.5 review → `accepted`, then start Phase 02.

- 2026-05-28 **Memory-first agentic harness (LOCKED):** The project-agent harness is **not** Codex App Server. Canonical turn truth lives in `/memory` collections using JSON schemas `scillm.harness_turn.v1` and `memory.recall_fusion.v1`. Each turn: extract-entities → `/intent` → `/context-pack` → `/skills/select` (planned) → scillm act (any supported model) + validated skill adapters → harness validation → `POST /upsert` `harness_turns` + async edges/semantic sync. Next turn recalls prior `harness_turn` from memory — not worker thread history. AgentTransport v1 (`scillm_agents_api` → `/v1/scillm/agents/*` → Codex app-server) is **in scope** as the first T2 actuation backend. Memory remains authoritative; worker thread history is never conversation truth. The review bundle borrowed Codex event *field ideas* only; runtime is memory upsert/recall. Evidence pack: `reviews/memory-harness-v2-webgpt-artifacts/`.
- 2026-05-28 **Skill injection architecture (WebGPT lock):** Separate skill **selection** (harness retrieval via `/skills/select`), **injection** (capsules into prompt/context-pack), and **execution** (`skill_call` via validated adapters). Do not scan raw `skills/` every turn — index SKILL.md frontmatter into Arango skill registry + Qdrant summaries; staleness via git-sha/file-watch/nightly. `/intent` emits `capability_needs` + `skill_policy`; `/skills/select` scores candidates using registry + prior `skill_invocations` receipts. Dogpile is the first long-running skill adapter proof. Schemas in v4 bundle: `memory.skills_select.v1`, `memory.skill_invocation.v1`.

- 2026-05-24: `$plan-iterate` historical-ledger cleanup archived all pre-existing phase ledgers under each phase `cleanup/` directory, preserved original `PHASE_STATUS.json` bytes with sha256 records, and rewrote the live ledger statuses to terminal `superseded` so stale phase schema drift no longer blocks unrelated final closure. Older backlog and next-action notes below are archived history unless a future entry explicitly promotes them to current active work.

- scillm is a single-tenant LLM proxy at localhost:4001
- Direct provider routing via openai SDK (Bifrost removed 2026-04-13)
- All logging via ArangoDB (`llm_call_log` collection), NOT Redis
- Redis is ONLY for optional caching
- Silent batch failures are forbidden — must log raw responses for debugging
- No-exceptions scillm policy: every `/v1/chat/completions` and `/v1/scillm/batch/completions` request must include `X-Caller-Skill`; `max_tokens` is rejected; batch-pool requests must include explicit `model_pool`, stable `batch_id`, and per-item ids so violations return actionable `scillm_policy_violation` errors to the project agent. Strict reliability is enforced in all environments: guarded requests, required middleware, logging, metrics, and batch-pool partial failures must not fail open.
- **JSONL backup** — all calls also written to `/mnt/storage12tb/scillm-logs/` (append-only, survives DB wipes)
- 2026-04-30: Long **chat/batch** streaming calls (`/v1/chat/completions`, `/v1/scillm/batch/completions/stream`) use SSE heartbeat liveness with short provider connect timeouts, unbounded provider read timeouts, caller-controlled overall budgets, and optional named progress events. This contract does **not** apply to `cursor_exec` — see 2026-05-30 entry below and `docs/SCILLM_EXEC.md`.
- 2026-05-19: scillm OAuth error handling was hardened for Codex/GPT and Claude. New calls expose structured provider errors (provider_auth_error, PROVIDER_AUTH_FAILED, model requested/served details, provider auth status, and project-agent messages), support exact-model fail-closed gates (require_exact_model / allow_model_remap=false), and attach non-streaming scillm_reasoning plus multimodal scillm_multimodal.image_seen_by proof fields. The core Docker container was rebuilt/restarted and live gpt-5.5 text plus multimodal smoke calls proved model_served=gpt-5.5, reasoning forwarded true, and image_seen_by=codex-oauth. Claude OAuth is expired and now fails with a clear structured auth error. The existing plan-iterate ledger is still correctly blocked until review-code and review-design are rerun with the new proof policy; historical artifacts do not become admissible retroactively.
- 2026-05-22: scillm-owned Python batch helpers now transparently auto-stream large prompts and reassemble the SSE stream into the same final result shape. Raw `POST /v1/chat/completions` remains OpenAI-compatible and non-streaming unless callers set `stream: true`. The accepted `$plan-iterate` phase `.plan-iterate/phase-20260522-scillm-streaming-reliability` proves the behavior with focused tests, external gpt-5.5 review, and a live `oc-kimi` create-qras-like large prompt canary.
- 2026-05-22: Follow-up caller audit found that `review-prompt` and `review-code` already used explicit streaming for their main scillm review calls, while `review-design` async batch calls and the server-side QRA model-pool endpoint still had blocking paths. `review-design.call_scillm_async` now streams and reassembles responses, `/v1/scillm/batch/completions/stream` is implemented as a real SSE endpoint, and the create-qras paved wrapper now uses that stream endpoint first with blocking batch only as a 404 compatibility fallback.
- 2026-05-22: Project-agent transport contract is explicit: agents and humans should not choose streaming versus blocking during normal work. Skill wrappers and scillm helpers own transport selection, use stream-first for large review/QRA/oracle/batch work, and reassemble the same final result object/schema for consumers. `stream:false` is a compatibility/debug override, not a normal project-agent decision.
- 2026-05-22: The live `http://localhost:3002/#scillm/dag-planner` Design Board/MAP is not acceptable as the DAG planner IA. Fresh screenshot proof showed the scillm project rendering a generic UX Lab empty dotted canvas with `DROP IMAGES HERE OR RIGHT-CLICK TO ADD`, `Select a card to view details`, and `0 ITEMS`, not a DAG orchestration surface. `$review-design webgpt` via `$ask` tab `837343564` returned `VERDICT: needs_changes`. The replacement contract is: top-down DAG orientation by default; persistent execution strip; main rendered DAG with active branch; contextual right data pane only after selecting a node/edge/amendment/event; visible `dag.json`, rendered tree, and one diff view; runtime amendment/provenance overlays; no generic image-drop/design-board affordances on this route.

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

Large QRA/default DeepSeek batches should use the server-side stream pool endpoint instead of manually splitting work across providers.

**Endpoint:** `POST /v1/scillm/batch/completions/stream`

**Pool:** `qra-deepseek-pool`

| Lane | Provider | Model | Weight | Max Concurrency | Timeout |
|------|----------|-------|--------|-----------------|---------|
| `chutes-deepseek` | Chutes | `deepseek-ai/DeepSeek-V3-0324-TEE` | 3 | 5 | 420s |
| `opencode-go-deepseek-v4-flash` | OpenCode Go | `opencode-go/deepseek-v4-flash` | 2 | 4 | 620s |

**Behavior:**
- Uses weighted round-robin assignment, then runs all items with `asyncio.create_task` + `asyncio.as_completed`.
- Streams `batch_started`, `item_started`, `heartbeat`, `item_completed`, `item_failed`, and `batch_done` events.
- Returns item results in completion order, not input order. Join results by `item_id`.
- Adds `scillm_metadata.batch_id`, `item_id`, `model_pool`, `lane`, `selected_model`, and `provider` to each inner call.
- This is for throughput across independent provider pools, not quality evaluation. Use `/llm-eval-lab` when every prompt must hit every model.
- Do not model this as fallback. Fallback improves reliability after failure; provider-pool batching raises throughput immediately.

**Discovery:** `GET /v1/scillm/model-pools`

**Dashboard status:** `GET /v1/scillm/model-pools/qra-deepseek-pool/status`

Use the pool status endpoint as the source of truth for dashboards. It returns aggregate `in_flight/limit/queued/available` plus per-lane Chutes/OpenCode Go state and drift fields. Raw `GET /v1/scillm/active-calls` is only a debugging view.

**OpenCode Go JSON contract:** DeepSeek/MiniMax OpenCode Go models use the Anthropic-compatible `/messages` endpoint. OpenAI `response_format` is not native there, so `/scillm` translates `response_format={"type":"json_object"}` and JSON schema response formats into provider-boundary JSON-only instructions in both the system prompt and final user turn while preserving all system messages.

**OpenCode Go multimodal:** As of April 26, 2026, `opencode models opencode-go --verbose` reports DeepSeek V4 Flash/Pro with `attachment=false`, `input.image=false`, and `input.pdf=false`. `/scillm` treats `opencode-go/deepseek-v4-*` and `opencode-go/minimax-*` as text-only lanes and rejects image/PDF content early with guidance to use `vlm`, Gemini, Claude, or Codex VLM paths. OpenCode intends `opencode run --file` and TUI attachment paths to support multimodal models, but upstream issues #16723 (`run --file` hardcodes `text/plain`) and #20802 (custom OpenAI-compatible image attachments do not reach vision-capable models correctly) are open, so do not use headless OpenCode CLI as a reliable multimodal workaround yet.

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
- 2026-05-17: test-interactions review reliability work found that visual UX proof for scillm exec graph debugging must use deterministic CDP run results plus focused/zoomed screenshots selected ahead of viewport shots, then a strict /review-design pass through scillm gpt-5.5. Do not treat DOM assertions or viewport-only screenshots as visual proof. For strict reviews set TEST_INTERACTIONS_REVIEW_DESIGN_MODEL=gpt-5.5, TEST_INTERACTIONS_REQUIRE_REVIEW_DESIGN_MODEL=gpt-5.5, TEST_INTERACTIONS_REVIEW_PROVIDER_FALLBACKS=, TEST_INTERACTIONS_ALLOW_SCILLM_VISUAL_FALLBACK=0, and TEST_INTERACTIONS_REVIEW_DISABLE_MEMORY=1. Context passed from test-interactions to review-design must be written as a real file because review-design --code-context treats arguments as paths.
- 2026-05-17: Nico review iterations for the scillm exec DAG debugger reached `satisfied` for core proof/debugging obligations after deterministic `test-interactions` captured `/tmp/scillm-exec-dag-ux/captures-nico-loop-15` with 9 PASS / 0 FAIL / 0 WARN and strict scillm gpt-5.5 review wrote `/tmp/scillm-exec-dag-ux/INTERACTION_REPORT_nico_loop_15_gpt55.md`. The final accepted UI separates lifecycle/result, optional failure pass semantics, selected/inspected node state versus keyboard focus, event timestamps in UTC, reported artifact, output hash, evidence status, and disabled completed-run controls. Remaining Nico findings were low-severity polish only.
- 2026-05-19: DAG viewer-editor readiness is accepted for tomorrow's local workflow testing. Accepted plan-iterate phases are .plan-iterate/phase-20260519-dag-self-improvement-loop and .plan-iterate/phase-20260519-dag-viewer-editor-tomorrow-test. The editor now visibly surfaces runtime readiness, missing fields, manual nodes, selected-node next action, and fanout rows with Agent, Contract, Model, Review level, Proof floor, Contract preset, Enabled, Read-only, editable Prompt contract, Save contract, Save agent, Duplicate, and Remove. The tomorrow test used the live harness at http://127.0.0.1:5179/, live review-catalog saves, and deterministic test-interactions with 11 PASS / 0 FAIL / 0 WARN. Visual proof is .plan-iterate/phase-20260519-dag-viewer-editor-tomorrow-test/evidence-artifacts/ui-interactions/dag-viewer-editor-tomorrow-workflow/0009_review-code-fanout-data-pane_click.png; endpoint/catalog proof is under the same phase validation-logs directory. The legacy text runtime alias has now been removed; /v1/scillm/models has no text alias/model and review fanout models are gpt-5.5, oc-kimi, oc-glm, oc-deepseek, and oc-qwen.
- 2026-05-20: DAG planner-editor fanout best-practices proof was refreshed after the ask gpt-5.5 review. The live Vite harness at http://127.0.0.1:5180 imports examples/exec-graph-debugger/ScillmExecGraphDebugger.tsx and now has a missingBestPractices=1 fixture that removes review-code-round-1 review_scopes[0].best_practice_skills for fail-closed proof. Deterministic test-interactions passed for default fanout controls (4 PASS / 0 FAIL / 0 WARN) and missing best-practices warning (3 PASS / 0 FAIL / 0 WARN). Visual proof crops are .plan-iterate/phase-20260519-dag-self-improvement-loop/evidence-artifacts/dag-review-fanout-visual-proof/review-code-fanout-best-practices-visible.png and review-code-fanout-missing-best-practices-warning-visible.png. Full result artifacts are under evidence-artifacts/test-interactions-dag-review-fanout-best-practices-final and evidence-artifacts/test-interactions-dag-review-fanout-missing-best-practices-final.
- 2026-05-20: DAG viewer-editor UX contract phase `.plan-iterate/phase-20260520-dag-viewer-editor-ux-contract` is now `ready_for_final_gate`, not blocked on missing output hashes. The root cause was the scillm exec runtime omitting `output_hash` / `output_artifact` from successful `node_results`; the DAG viewer was correctly failing closed. `src/scillm/proxy/exec_api.py` now writes canonical `output.evidence.json` and hash metadata for each completed node, and the ux-lab DAG viewer API plus amendment endpoint prefer the `scillm-exec-run-hash-bound` artifact set. Real e2e sanity passed after one caught-and-fixed stale-hash bug: live snapshot returned 7/7 hash-bound nodes with `missing_output_hash_nodes: []`, the route rendered with Deploy blocked, and draft amendment save returned HTTP 200. Evidence: `evidence-artifacts/scillm-exec-run-hash-bound/status.json`, `evidence-artifacts/dag-viewer-api-proof/hash-bound-snapshot-20260520T1845Z.json`, `evidence-artifacts/e2e-sanity-20260520T1910Z/result.json`, `evidence-artifacts/e2e-sanity-20260520T1910Z/dag-planner-live.png`, and `validation-logs/pytest-exec-hash-bound-20260520T1848.log`.
- 2026-05-20: DAG viewer-editor amendment actions are ready for bounded human/project-agent testing, not production deploy or final accepted closure. Phase `.plan-iterate/phase-20260520-dag-viewer-editor-ux-contract` is now `external_review_passed` after scillm `gpt-5.5` high review v3 passed. The visible Design lens shows the Memory-backed amendment panel with approved/rejected records and approve/reject/supersede actions; real UX API e2e saved two harmless amendment records, updated one to `approved` and one to `rejected`, and listed both back. Evidence: `/tmp/phase-20260520-dag-viewer-editor-ux-contract-amendment-actions-final.zip`, `.plan-iterate/phase-20260520-dag-viewer-editor-ux-contract/evidence-artifacts/amendment-actions-20260520T2055Z/result.json`, `ui-proof.json`, `dag-planner-amendments.png`, and `reviews/20260520T210652Z-scillm-gpt55-high-amendment-actions-v3-response.md`. Non-goal still open: applying an approved amendment to mutate the authoritative runtime graph remains intentionally out of scope and must stay fail-closed until implemented and reviewed.
- 2026-05-21: Runtime intervention for the DAG viewer-editor is now evidence-gated through accepted phases. `phase-20260521-dag-runtime-control-contract` accepted the backend action contract for graph/node/subtree pause, resume, disable, stop/cancel, action history, and fail-closed running-node conflicts. `phase-20260521-dag-runtime-control-ui-wire` accepted the UI wiring to the real action endpoint with terminal/no-run_id fail-closed behavior. `phase-20260521-active-execution-follow-and-status` accepted the low-option follow-current behavior. `phase-20260521-approved-amendment-apply-runtime` accepted approved Memory amendment application as a provenance-recorded runtime decision overlay that preserves base/draft graph evidence and rejects stale base hashes.
- 2026-05-21: Direct DAG execution and the executable domain-review-loop ledger are now accepted for the bounded DAG viewer-editor project goal. `phase-20260521-dag-json-execution-catalog-closure` proves editable `dag.json` can be inspected, recorded, compiled, and executed directly by `$plan-iterate`/scillm while preserving node/review catalog provenance and inline overrides. `phase-20260521-domain-review-loop-ledger-current-tree` supersedes the stale 2026-05-20 full-loop e2e evidence against the current tree: run-003 completed 13/13 nodes through `/v1/scillm/exec/graph`, all outputs were hash-bound, review-code/review-design/review-prompt round and final nodes recorded non-empty `evidence_checked`, and the final `$plan-iterate` gate returned `ready_for_review`, `verification_verdict=PASS`, and zero critical/high/medium blockers. Failed run-001 and run-002 remain preserved as drift/debug evidence for scheduler deadlock on reviewer timeout and stale verifier schema.
- 2026-05-21: `scillm exec oc-chutes-deepseek` is implemented as an `opencode_exec` profile-only OpenCode CLI worker lane, separate from existing Scillm one-shot HTTP `opencode-go/*` model calls. The profile uses Chutes from `.env` and defaults to `chutes/moonshotai/Kimi-K2.6-TEE` with `SCILLM_OPENCODE_CHUTES_DEEPSEEK_MODEL` override. Raw chat profiles/direct model IDs are rejected for this exec lane. Proof artifacts: targeted pytest `41 passed in 42.15s`; live canary run `oc-chutes-canary-1779382901` wrote only `allowed/oc_canary.json` with no allowlist violations; `$hack exploit` Docker probe passed and preserved raw logs under `.artifacts/opencode_exec_hack_probe/`.
- 2026-05-21: `scillm exec pi-chutes-kimi` is implemented as a `pi_exec` profile-only Pi CLI worker lane, separate from Scillm one-shot HTTP `oc-*` and `opencode-go/*` model routes. The profile invokes the local Pi fork `/home/graham/bin/pi` -> `/home/graham/workspace/experiments/pi-mono` with provider `chutes`, default model `moonshotai/Kimi-K2.6-TEE`, and `--thinking off`; `SCILLM_PI_BINARY` and `SCILLM_PI_CHUTES_KIMI_MODEL` are the supported overrides. Docker mounts `/home/graham/.pi/agent` plus the Pi fork so the proxy sees the same Chutes provider registry/auth as the host. Raw chat aliases/direct model ids are rejected by `scillm exec`. Proof: targeted pytest `51 passed in 42.21s`; read-only live run `pi-chutes-readonly-smoke-1779384703` returned `status=ok`, `runner=pi_exec`, `pi_provider=chutes`, `pi_model=moonshotai/Kimi-K2.6-TEE`; workspace-write canary `pi-chutes-canary-1779384713` wrote only `allowed/pi_canary.json` and reported `write_audit.violations=[]`; `$hack exploit` Docker probe passed and preserved raw logs under `.artifacts/pi_exec_hack_probe/`.
- 2026-05-30: **cursor_exec HTTP slim response** — `POST /v1/scillm/exec` no longer embeds full `cursor_stream_text` in JSON (fixes httpx "chunk longer than limit" false `process_error` on long runs). Receipt fields + on-disk `stdout.log` / `cursor-events.jsonl` remain authoritative. Poll client: `scripts/pdf_lab/scillm_exec_poll.py` (mirrored from pdf_oxide).
- 2026-05-30: **`cursor_exec` liveness ≠ chat SSE / timeout estimator.** Monitor exec runs via artifacts, not estimated chat timeouts alone:
  - `/tmp/scillm-exec/<run_id>/events.jsonl` — supervisor stdout/stderr + `emit()` trail (`GET /v1/scillm/exec/{run_id}/events` tails the same file)
  - `.scillm/cursor-headless/<run_ctx>/cursor-events.jsonl` — parsed stream-json NDJSON (incremental append during the run)
  - Terminal receipt/response: `stream_completed`, `recovered_from_stream`, `result_event`, `tool_call_count`, `text`
  - **Success:** terminal `{"type":"result",...}` with `subtype` / `is_error` checks — not process exit alone
  - **Liveness:** semantic stream events (`system`, `thinking`, `assistant`, `tool_call`, `result`); do not infer stuck from wall time while tool/assistant activity continues
  - **`timeout_s` / `idle_timeout_s`:** fail-closed backstops only on `cursor_exec`; do not use chat `stream_heartbeat_s` or `timeout_estimator` middleware as the primary progress signal
  - **Two-call pattern (e.g. PDF Lab):** call1 `cursor-plan` diagnose (read-only); call2 `cursor-auto` + allowlist (bounded writes). Example budgets: 600s/120s idle then 1200s/300s idle — tune backstops, not primary scheduling
  - **Orchestrators:** may blocking-`POST /v1/scillm/exec` and trust server-side supervision, or tail `events.jsonl` / poll for terminal `result` during long runs; do not duplicate stream-json parsing in callers unless bypassing scillm
  - **Other exec runners** (`codex_exec`, `pi_exec`, …): byte-level stdout/stderr idle — only `cursor_exec` uses semantic NDJSON idle. Canonical detail: `docs/SCILLM_EXEC.md`, implementation: `src/scillm/proxy/exec_api.py` `_run_cursor_agent_process`.

- 2026-05-27: Exec Codex profiles are provider-prefixed: canonical `codex-gpt-5.5` and `codex-vision` on `codex_exec` (CLI `scillm exec codex-gpt-5.5`). Bare `gpt-5.5` remains chat-only on `/v1/chat/completions`; `codex_exec` still accepts `model: "gpt-5.5"` as a deprecated alias resolving to `codex-gpt-5.5`. Docs: `docs/SCILLM_EXEC.md`, `README.md` exec table.
- Standing-agent live proof for project agents: `examples/standing-agent-sample/` delegates a real reviewer handoff to worker `scillm-reviewer` via `project_agent_client.py`; run `./examples/standing-agent-sample/run.sh` after `./scripts/start_scillm_standing_agent.sh`. Artifacts: `.scillm/standing-agent-sample/*.json`.
- 2026-05-23: Interactive standing-agent work now treats scoped implementation workers as candidate-patch producers in worker worktrees. The project agent sends bounded phase packets, reviews worker diffs, decides request_iteration/reject/merge-adopt, reruns deterministic validation, packages evidence, and only then sends the one-shot scillm phase review. plan-iterate remains the phase controller and accepts/iterates/blocks/human-gates the phase; workers and scillm reviewers do not self-certify closure.
- 2026-05-24: Interactive subagent target clarified: the desired product behavior is a visible three-party conversation among human, project agent, and named subagent, not an opaque background worker. The subagent should stream as a participant, the human and project agent should be able to inspect the shared tmux/PTY session, and course corrections must be recorded with actor/time/session/transcript evidence. Source artifact: docs/interactive-agents/visible-subagent-conversation-target.md. Superseded candidate now completed: `.plan-iterate/phase-20260524-visible-nico-conversation-member` turned this target into a runtime/API contract and proof path for ask plus scillm /agents integration.
- 2026-05-24 correction: tmux/PTY must not be read-only in the desired interactive subagent design. The target is bidirectional shared operator control: both human and project agent can inspect and steer the subagent session, and every injected correction must be attributed, mirrored into events, and bound to transcript position. The existing read-only Phase 7 tmux diagnostic behavior is insufficient and should be treated as historical/current-state only, not the next product target.
- 2026-05-24: The visible subagent conversation target is now explicitly memory-first and skill-runtime-capable for every participant. Before starting or steering Nico/subagents, the project agent must run /memory recall over the request and extracted entities; meaningful turns, blockers, course corrections, and outcomes must be upserted as compact records with raw transcripts/events path-referenced. All agents, not only the project agent, need access to `$memory` and other required skills through documented runtime entrypoints; headless codex exec without skill/runtime access is insufficient.
- 2026-05-27 tier routing (execution surfaces): chat=one-shot; exec=deterministic pipelines NOT code writing; agents=optional standing workers via `/v1/scillm/agents/*` (separate from memory harness). **Memory harness** owns extract-entities, `/intent`, `/context-pack`, `/skills/select`, turn-close upsert to `harness_turns`, and model pick before scillm act.
- Tier examples: design critique -> chat + oc-kimi; monitor-sparta checkpoint loop -> exec; multi-file code fix -> memory harness turn loop + scillm chat/agents as actuator (model-agnostic). Exec and agents are different call kinds; do not route product code authorship through exec graphs.
- 2026-05-28: Memory already has partial skill recommendation via capability_routing.py (skill_route on recall + recommend-skill-chain cascade), skill_chains collection + --brief chain recall, and ingest-skills CLI. Phase 3 /skills/select should extend this, not greenfield.
- - 2026-05-30 **cursor_exec stream-read fix + fake-agent probe:** asyncio StreamReader.readline() caps lines at about 64KiB; oversized cursor stream-json lines used to surface as failed process_error ("Separator is not found, and chunk exceed the limit") even when Cursor had succeeded. Fix: chunked line reader `_iter_subprocess_text_lines()` in `src/scillm/proxy/exec_api.py`, plus `cursor-events.jsonl` reconciliation on terminal `result`. **Fake-agent probe** = `scripts/test_fixtures/fake_cursor_stream_agent.py` — a tiny script that prints one 75KB JSONL line and a terminal success event; used only to regression-test exec without calling the real Cursor CLI. Set `SCILLM_CURSOR_AGENT_BINARY` to that path for probes (compose: `${SCILLM_CURSOR_AGENT_BINARY:-/home/graham/.local/bin/agent}`). Tests: `tests/test_exec_cursor_stream_http.py`; optional `SCILLM_CURSOR_STREAM_PROBE=1 ./scripts/sanity_exec_endpoints.sh`. Live proof: `POST /v1/scillm/exec` `cursor_exec` → `completed`, `stream_completed=true`. Not a substitute for real cursor jobs (still need real agent + terminal result in `cursor-events.jsonl`). Also fixed ExecRun regression where `emit` / `_run_cursor_agent_process` were nested outside the class.
- - 2026-05-30 **Exec & agents runtime status (verified this session):**
  - **`POST /v1/scillm/exec` (core):** `tests/test_exec_e2e.py` 6/6 PASS on live proxy (local_command, batch, graph, cancel, runtime env). `scripts/sanity_exec_endpoints.sh` covers the same deterministic paths.
  - **`cursor_exec`:** Stream-read fix + `ExecRun.emit` class regression fixed in `src/scillm/proxy/exec_api.py`. Fake-agent probe PASS (`tests/test_exec_cursor_stream_http.py` + live curl). Production binary restored to `/home/graham/.local/bin/agent`. Real cursor jobs still require terminal `result` in `cursor-events.jsonl` (pdf_oxide historical failures without that are a separate issue).
  - **Exec profiles (earlier proof, still valid):** `codex_exec` (`codex-gpt-5.5`), `pi_exec` (`pi-chutes-kimi`), `opencode_exec` (`oc-chutes-deepseek`) — see 2026-05-21 entries.
  - **`/v1/scillm/agents/*` (standing workers):** Handoff/lease/turn/steer path accepted via plan-iterate phase `20260524-visible-nico-conversation-member`; sample at `examples/standing-agent-sample/`. Separate from memory harness (harness turn truth = `/memory`, not agent thread history).
  - **Memory harness (not full e2e yet):** Phases 01–04 **accepted**; phase 05 `turn_loop` **not started** (see Agent Takeover Notes table). E3 `progress_events` product scenario proven in harness tests this branch but not yet reflected in phase ledger table above.
- - 2026-05-30 **Goals / outstanding source of truth:** See **Agent Takeover Notes** (replaced 2026-05-28 handoff table). Harness phases **01–07 accepted**, **08–09 complete**, **10–12** implemented with gate scripts but ledgers not all closed. Do not treat "phase 05 next / no turn_loop" bullets above as current — superseded by phase-05/06 acceptance and `src/scillm/harness/turn_loop.py`.
- 2026-06-01: **OpenCode transport v1** (`/v1/scillm/opencode/transport/*`) is the canonical DAG/agent-debugger control plane over OpenCode Serve (parent/child sessions, delivery state under `.scillm/opencode-transport/`). `POST .../message` defaults to **SSE** (`stream: true`): relays OpenCode bus events including `reasoning_delta`, `tool_call`, `permission_requested`, and `session_error` while sync `/session/:id/message` runs. Project agents must monitor the stream or `events.jsonl` — not infer success from empty `assistant_text` or a long blocking HTTP read. `timeout_s` is the overall stream budget. Legacy blocking JSON: `stream: false`. `/agent-debugger` streams by default and writes `monitor_events.jsonl`. Spec: `docs/SCILLM_OPENCODE_TRANSPORT_V1.md`; skill: transport section in `/scillm` SKILL.md.
- 2026-06-01 Phase Harness routing clarified, now superseded in human-facing wording by Planner / Orchestrator / Executor: the project agent owns roadmap, scope, human communication, and final audit; the Phase Harness owns delegated complicated phase execution with classify/plan-DAG/receipts/validation/amend/terminal-status; `phase-agent` is only a callable role/persona for phase runs, not a second project agent; OpenCode subagents/workers own bounded task attempts and return evidence/diffs/events only.
- 2026-06-01 **Planner / Orchestrator / Executor terminology adopted:** Use Planner for the human-facing Project Agent, Orchestrator for the Agentic/Execution Harness, and Executor for bounded workers such as OpenCode subagents, Debugger, Patcher, Reviewer, test runner, scillm exec, and standing Codex workers. This is the preferred human-facing explanation; backend docs and schemas may still use Project Agent, Execution Harness, Phase Run, Campaign Run, and Role Actor as precise aliases. The transport/UI mental model should show Planner -> Orchestrator -> Executors rather than treating every participant as just another agent.
- 2026-06-01 **WebGPT terminology review passed with clarification:** `$ask webgpt` on scillm tab `837343814` returned `VERDICT: PASS` for Planner / Orchestrator / Executor, with the condition that docs state these are human-facing role labels, not implementation class names or schema identifiers. Planner means the human-facing Project Agent and must not be confused with the internal DAG planner. Keep Execution Harness as the backend-precise Orchestrator name unless a separate schema/runtime migration is planned.
- 2026-06-02 **pdf-lab scillm endpoint hardening lane:** The `$pdf-lab` second-pass repair path should use chat for cheap preflight, OpenCode serve for one bounded patch delegate, and OpenCode transport for Orchestrator/Debugger parent-child DAG control. Evidence from the live sanity pass is under `.plan-iterate/pdf-lab-opencode-serve-hardening/`: `actual-run/status.json`, `actual-run/validation.log`, `e2e-sanity/chat-local-after-fix.body`, `e2e-sanity/chat-local-missing-caller-after-fix.body`, `e2e-sanity/opencode-messages-after-fix.json`, `e2e-sanity/transport-mounted-message.json`, `e2e-sanity/transport-mounted-state.json`, and `e2e-sanity/transport-mounted-git-status.txt`. The mounted-workspace transport canary completed with non-empty assistant text, `diff=[]`, and clean worktree status; arbitrary `/tmp` code roots are not acceptable proof because proxy/OpenCode runtime filesystem visibility can diverge. Missing `X-Caller-Skill` is now a verified 400 `caller_skill_required` path.
- 2026-06-02 **Transport collaboration room (ux-lab, pi-mono):** Route `#scillm/transport` is the operator-facing **three-party chat** for OpenCode transport dialog (`GET/POST .../transport/runs/{id}/dialog`). Visible parties: **Human**, **Project agent** (Planner), **Worker** (Executor). The **Orchestrator** (Execution Harness) is **not** a fourth chat avatar yet — harness/transport/system rows render as **Harness** with `Workflow` icon (`transport_start`, forwards, delivery). **Spawn** cards from the Project agent also use `Workflow` (dispatch via harness); plan/handoff/skill prose uses `Route` (Planner). Composer is **human-only** (no speaker toggle). Copy for review bundles screenshot + `REVIEW_REQUEST.md` + transport source from `pi-mono/packages/ux-lab/src/components/scillm/transport/`. Future: add explicit Orchestrator collaborator + bubble when product wants harness as a peer; reserve `Workflow` + label **Orchestrator** or **Harness**. Implementation: `messageCardContract.ts`, `TransportMessageTimeline.tsx`, `TransportComposer.tsx`. Backend dialog still uses `collaborator: project_agent|human|worker`; internal parse kind `reviewer` is legacy display plumbing only.
- 2026-06-02 **Transport Copy for review bundle (ux-lab):** Zip now includes `README.md`, narrow `REVIEW_REQUEST.md` (default scope `icon-role`), `CHANGE_SUMMARY.md`, real `DIFF.md` via `GET /ux-lab-api/transport-review/diff` when `pnpm dev` is running, `FOCUSED_SOURCE.md` (8 critical files), `FULL_SOURCE_APPENDIX.md` (full tree), and `transport-room.png`. Manual optional: `SCREENSHOT_BEFORE.png`. Requires Vite dev server for embedded git diff.
- 2026-06-02 **OpenCode server integration state:** Direct OpenCode serve with GPT-5.5 OAuth is proven through medium-to-phase-sized code-writing tasks (Levels 1-6), selected skill contracts are reliable when included by custom command `@SKILL.md` references, directory-scoped `/event` SSE is proven, and the managed scillm sidecar is stable after the named `graham` runtime user plus post-install chown fix. The scillm patch delegate path has terminal diff evidence via bounded filesystem snapshot fallback and a live `PATCH_APPLIED` canary (`oc-7e8329d24a59`, `diff_source=filesystem_snapshot`), plus a live serve unwritable-workspace `PATCH_DELEGATE_BLOCKED/permission_denied` canary (`oc-18cd71954499`). Transport L5 is proven for `patch-worker` and `code-reviewer`; L6 is proven for retry/fork, abort, timeout, unavailable-model blocked classification, missing-runtime blocking, denied-permission blocking, transport unwritable-workspace blocking, one deterministic real-skill worker canary for `code-runner`/`test` (`otr-364a1b7be2b1`), and one multi-node amended DAG proof (`.plan-iterate/phased-plan-agentic-example/run-1780440345/`). L7 long-running campaign behavior is proven by `.plan-iterate/opencode-transport-level7-campaign/run-1780441159/`; campaign timeout/abort must use streaming transport. Docker context hygiene is fixed for proxy rebuilds (`17.96 MB` context after `.dockerignore` update). Remaining useful hardening: broader real-skill canaries for research/evidence skills such as `dogpile` or `create-evidence-case`.
- 2026-06-02 **OpenCode server integration plan artifact:** The phase plan for completing OpenCode server integration into `$scillm` is `docs/goals/opencode-server-scillm-integration-plan.md`. It defines readiness levels L1-L7, records current status as L7 proven for the bounded campaign ladder, preserves evidence-backed thresholds for direct OpenCode vs scillm transport/agentic harness delegation, and names remaining useful hardening such as broader research/evidence real-skill canaries.
- 2026-06-02 **OpenCode transport L5 agent_id canary passed:** Live streamed transport canaries proved `agent_id` workers through `$scillm`. `patch-worker` run `otr-1bdfc581b1e4` auto-created from `agent_id` only, resolved to OpenCode `build` with `workspace_write`, wrote `evidence/patch-worker-stream-canary.json`, ran Python validation, emitted `message.completed`, and returned a diff. `code-reviewer` run `otr-bb5ddd957d14` auto-created from `agent_id` only, resolved to OpenCode `explore` with `propose_patches`, returned `VERDICT: PASS`, emitted `message.completed`, and returned `diff=[]`. Fixes applied: patch-worker template now uses `opencode_agent=build` and `mode=workspace_write`; transport API no longer overrides catalog mode for `agent_id` auto-child creation; read-only git diff enforcement is scoped to the workspace path instead of the dirty parent repo.
- 2026-06-02 **OpenCode health output compacted:** `GET /v1/scillm/opencode/health` now omits `agents_full` by default, does not call the expensive OpenCode `/agent` endpoint, and returns compact readiness (`full=false`, `agent_catalog_source=static_default`, `agent_count=7`, OpenCode `healthy=true`, version `1.14.31`). `GET /v1/scillm/opencode/health?full=true` remains the opt-in debugging path and returns `agents_full` from OpenCode `/agent`. This closes the monitor-ingestion and compact-health CPU issue from the OpenCode server integration plan; later work can broaden real-skill canaries, but the blocked-substrate and multi-node amendment gates are already proven.

## Recent Decisions

| Date | Decision | Why |
|------|----------|-----|
| 2026-06-02 | Transport room UX uses Planner/Orchestrator/Executor icon contract in ux-lab | Human-facing labels: Project agent (Route), Harness/Orchestrator events (Workflow), Worker (BotMessageSquare). Three-party legend until Orchestrator is a visible fourth party; aligns with `/scillm` SKILL.md and WebGPT PASS on role terminology. |
| 2026-06-02 | `$pdf-lab` live gates must use mounted isolated code roots for OpenCode serve/transport proof | A `/tmp` transport canary exposed a workspace visibility/acceptance failure while a mounted project-workspace detached worktree passed with `delivery_state=completed`, non-empty assistant text, empty diff, and clean git status. |
| 2026-06-01 | Use Planner / Orchestrator / Executor as the human-facing scillm role model | This is clearer than treating every participant as an agent: Planner = Project Agent, Orchestrator = Execution Harness, Executor = OpenCode subagent/Debugger/Patcher/Reviewer/test runner/scillm exec/standing Codex worker. Backend docs and schemas may retain precise aliases. |
| 2026-05-28 | $plan-iterate cross-phase continuation + universal live e2e gates | Phases must not yield between accepted phases; fixable failures are patch loops not stops; severity gate blocks advance if any accepted phase ledger is invalid |
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
| 2026-04-27 | Fail closed for guarded and observability failures in all environments | scillm reliability requires incorrect calls, missing required middleware, schema/grounding failures, logging failures, metrics failures, and batch-pool item failures to return explicit actionable errors instead of silently degrading. |
| 2026-04-30 | Use SSE heartbeats and overall budgets for long /scillm calls | Fixed 15s response caps break grounded reasoning calls; streaming callers need short connect timeout, heartbeat/idle liveness, and a 5-10 minute overall budget. |
| 2026-05-17 | test-interactions strict visual review must fail closed on required model mismatch or unavailable gpt-5.5 | The user expects scillm gpt-5.5 from the Codex OAuth path, not Chutes/text/model fallback. During exec graph UX review, fallback language and literal-context handling created ambiguity; strict env gates and required served-model checks are the reliable contract. |
| 2026-05-17 | plan-iterate is the parent evidence gate for review-design, review-code, and review-prompt loops | Domain review skills own their specific loops: review-design drives test-interactions for UI evidence, review-code drives code critique plus tests, and review-prompt owns its coded prompt contract loop. plan-iterate records phase state, validation artifacts, reviewer receipts, blockers, and acceptance; it should not replace the domain loop or accept reviewer prose without deterministic evidence. |
| 2026-05-19 | Fail closed for OAuth review proof and exact-model mismatch | The review ledger showed that hidden model remaps and missing scillm proof fields can make empty or ad hoc review artifacts look acceptable. Phase-closing review calls must preserve provider OAuth errors, requested/served model details, reasoning proof, multimodal proof, and must not treat historical artifacts as green after proxy fixes. |
| 2026-05-19 | Use plan-iterate as DAG viewer-editor readiness gate | The DAG viewer-editor is user-facing orchestration UI and crosses evidence, review, prompt, model-selection, and workflow contracts. Calling it ready requires plan-iterate phase evidence, deterministic test-interactions, actual screenshot inspection, endpoint/catalog checks, and accepted or explicitly not-required review status. |
| 2026-05-19 | Preserve review ledger history with resolved and superseded states | Old needs_changes findings should be marked resolved_by_later_pass, stale PASS reviews should be marked superseded_by_later_pass, and only the latest unsuperseded accepted evidence can close a phase. This keeps audit history without laundering stale or contradictory review artifacts into closure. |
| 2026-05-20 | DAG planner-editor fanout nodes require visible best-practices skill contract fields | The review-code fanout data pane must expose model, agent, contract, proof floor, prompt preset, prompt body, and best_practice_skills. Empty best_practice_skills must be a blocking/missing-field state with visible warning and deterministic screenshot proof, not a hidden reducer-only validation. |
| 2026-05-20 | DAG viewer-editor closure must require hash-bound exec node output evidence. | The old missing-hash blocker was a producer/runtime bug, not a UI bug. `node_results` for completed nodes must include `output_hash`, `output_artifact`, `output_hash_algorithm`, and `evidence_status`; the viewer should fail closed when those fields are absent. |
| 2026-05-20 | plan-iterate full-loop proof requires reviewer evidence_checked, not only aggregate summaries. | The v2 full-loop canary proved graph execution but external review correctly rejected empty final reviewer `evidence_checked` fields. The v3 canary requires final review-code, review-design, and review-prompt nodes to record concrete dependency result artifacts, and external gpt-5.5 high review passed the bounded canary. |
| 2026-05-20 | review-design false-green reviews require modern reference and interaction evidence | Repeated human-discovered DAG planner-editor UX errors showed that design PASS cannot rely on generic screenshot critique. review-design now has fail-closed iterate prompt gates for missing_evidence, screenshot_checks, and modern_reference_gaps; test-interactions generate now enumerates live [data-qid] interactive controls with focused screenshot assertions; serious/repeated false-green workflow-editor reviews should run dogpile/current-reference collection and pass it via --modern-reference --require-modern-reference. |
| 2026-05-20 | review-design requires Dogpile-derived requirements plus reference screenshots for serious visual benchmarks | The false-green hardening now separates Dogpile URL/report evidence from visual benchmark evidence. review-design iterate extracts design requirements and reference URLs from Dogpile partial-results JSON, requires matching Dogpile persona when --require-modern-reference is used, accepts --reference-screenshots to attach captured benchmark screenshots to every section review, and tells reviewers to mark missing reference screenshots as missing_evidence when visual comparison is required. Captured React Flow reference screenshots for the current DAG editor benchmark live at /tmp/review-design-dogpile-reference-screenshots. |
| 2026-05-22 | Auto-stream large prompts in scillm-owned Python batch helpers, not the raw chat endpoint | Preserves OpenAI-compatible raw HTTP semantics while improving liveness, heartbeat observability, and reliability for large review, QRA, oracle, and open-source model calls. Consumers of the helpers still receive a normal completed result object. |
| 2026-05-22 | Make server-side QRA model-pool batches stream-first | The create-qras skill already documented `/v1/scillm/batch/completions/stream`, but the proxy did not implement it. The stream endpoint now emits batch/item/heartbeat events and each item calls the internal chat endpoint with `stream=true`; the blocking endpoint remains compatibility fallback. |
| 2026-05-22 | Do not make project agents choose streaming | Streaming is a scillm/wrapper transport concern, not a project-agent prompt decision. Large review/QRA/oracle/batch workflows should call the appropriate helper or stream endpoint and receive the same final result shape; `stream:false` is only for compatibility/debug/small smoke cases. |
| 2026-05-22 | Replace DAG Planner Design MAP, do not patch generic Design Board canvas | The current `#scillm/dag-planner` Design Board/MAP renders the wrong product: a generic empty UX Lab design canvas. The DAG planner should default to a top-down executable DAG map with active execution strip, contextual right pane on selection only, dag.json/rendered tree/diff inspection, amendment overlays, and node/subtree intervention controls. |
| 2026-05-23 | Interactive standing agents use worker worktrees with project-agent merge authority | Headless codex exec was unreliable for multi-round bug fixing; standing workers can keep context and edit scoped worktrees, but the project agent must review/adopt the candidate patch and plan-iterate must gate closure after deterministic evidence and one-shot scillm review. |
- Visible subagent course correction is a first-class requirement: the human and project agent must be able to observe Nico's live transcript/PTY while work is happening and inject bounded steering that is mirrored into events.jsonl with actor, target session, timestamp, transcript position, and resulting state. Nico's 2026-05-24 `$ask` run recommended first-class visible_subagent routing, natural triggers, memory-first persona recall, skill-syntax preservation, dynamic steering events, and repo-scoped .ask_artifacts/visible-subagent outputs. Current limitation: the real `$ask` subagent-runner completed under /tmp/ask-oracle-subagents/ask-oracle-nico-1-1779632885-4a3c3b9b, but Nico reported bwrap shell failure and could not re-read SKILL.md from inside the run, so current `$ask` can consult Nico but is not yet the full visible skill-runtime-capable conversation target. Source: docs/interactive-agents/visible-subagent-conversation-target.md
- Active plan-iterate phase created for the visible Nico conversation member target: `.plan-iterate/phase-20260524-visible-nico-conversation-member`. Goal: make Nico a full visible, memory-first, skill-runtime-capable member of the human/project-agent conversation through a natural `$ask` visible-subagent trigger. The phase records skill context, progress context, and plan graph `visible-nico-conversation-member`; acceptance requires natural trigger routing, memory-first recall, skill syntax preservation, live transcript deltas, attributed dynamic steering events, Nico skill-read/runtime proof, repo-scoped `.ask_artifacts/visible-subagent/*` artifacts, sample conversation proof, deterministic validation, and read-only final review.
- - 2026-05-24: Visible Nico conversation member phase first implementation slice is now validated. Natural `Ask Nico ...` triggers route through `$ask` visible_subagent mode, preserve the original utterance and normalized skill mentions, default runner-backed Codex oracle sessions to an explicit `danger-full-access` sandbox, and emit parent-visible `subagent_started`, `transcript_delta`, heartbeat, and `subagent_final` events. Real smoke `visible-nico-skill-read-smoke-20260524` proved Nico could read `/home/graham/workspace/experiments/agent-skills/skills/memory/SKILL.md`. Evidence: `.plan-iterate/phase-20260524-visible-nico-conversation-member/evidence-artifacts/visible-nico-skill-read-smoke-summary.json`, `.plan-iterate/phase-20260524-visible-nico-conversation-member/validation-logs/ask-visible-subagent-focused-tests-final.log`, and mirrored request/status/events artifacts under `.plan-iterate/phase-20260524-visible-nico-conversation-member/evidence-artifacts/visible-nico-skill-read-smoke-20260524/`. Historical caveat, now completed by the accepted phase: mid-run human/project-agent steering input was not implemented or proven in this first slice.
- 2026-05-24: The visible Nico conversation member phase `.plan-iterate/phase-20260524-visible-nico-conversation-member` is now `accepted` and valid. It replaces terminal/PTY/tmux keystroke steering with scillm Codex app-server handoff/lease/turn/steer integration, keeps visible-subagent routing fail-closed, preserves skill syntax in the sample conversation proof, and records app-server steering as normal path rather than simulated terminal input. Deterministic proof: `.plan-iterate/phase-20260524-visible-nico-conversation-member/validation-logs/scillm-agent-turn-steer-tests-after-review-fix.log` and `.plan-iterate/phase-20260524-visible-nico-conversation-member/validation-logs/ask-visible-subagent-app-server-routing-fix-tests.log`. Review proof: `.plan-iterate/phase-20260524-visible-nico-conversation-member/reviews/review-code-app-server-steering-r8.md`.
| 2026-05-27 | Exec tier is not for writing code | Exec is for monitor-style deterministic pipelines with LLM at gates and recover+report; product code authorship uses agents (standing Codex workers). |
| 2026-05-27 | Three-tier routing is harness-owned | Project agent runs extract-entities, memory recall, skill chain, tier+model pick, then scillm call; chat/exec/agents are execution surfaces only. |
| 2026-05-28 | Memory harness is /memory not Codex App Server | Turn truth lives in harness_turns + recall_fusion via /memory; Codex App Server is optional separate /v1/scillm/agents transport only |
| 2026-05-28 | Skill pipeline: intent → skills/select → capsule inject → skill_call | WebGPT lock separates selection, injection, and execution; index skills to Arango+Qdrant; record skill_invocations for scoring |
| 2026-05-30 | Separate chat streaming liveness from `cursor_exec` stream-json supervision | Chat SSE heartbeats and timeout estimator apply to `/v1/chat/completions` only; `cursor_exec` completes on terminal stream `result`, uses semantic idle reset, and keeps `timeout_s` as backstop — documented in `docs/SCILLM_EXEC.md` |
| 2026-05-28 | Phase-03 gate sets PYTHONPATH for memory pytest | Without it pytest collects ModuleNotFoundError: graph_memory; inline sample already had PYTHONPATH |
| 2026-05-30 | Use fake_cursor_stream_agent only for stream-read regression probes | Proves the 64KiB readline bug is fixed without Cursor API quota; production cursor_exec still uses /home/graham/.local/bin/agent by default. |
| 2026-06-01 | OpenCode transport message stream-first with reasoning monitor | DAG/agent-debugger workers need liveness and reasoning visibility during long OpenCode runs; blocking POST timeouts hid permission stalls and empty assistant_text false negatives. |
| 2026-06-02 | Use OpenCode server in scillm as a three-tier executor substrate, not as the orchestrator | Direct OpenCode serve is now proven for one bounded source-mutating attempt with tests and receipts; scillm transport/agentic harness remains required for DAG state, concurrent workers, retries, fork/supersede, amendments, long-running monitoring, and orchestrator-owned terminal status. |

## Open Questions

- [x] Why did batch store 0/1075 QRAs? → Schema mismatch (`reason` vs `abstain_reason`)
- [ ] Why wasn't caching preserving failed batch responses?
- [x] Should the legacy text alias be removed from /v1/scillm/models now that the DAG viewer-editor filters model choices to oc-kimi, oc-glm, oc-deepseek, and oc-qwen? Removed as a runtime alias on 2026-05-19; authenticated text calls now fail with HTTP 400 unknown model.
- [x] What backend step should generate or formally exempt required output hashes for required DAG execution nodes so the DAG viewer-editor can move from fail-closed blocked state to admissible closure evidence? Resolved 2026-05-20: scillm exec runtime writes canonical `output.evidence.json` per completed node and stamps `node_results` with hash metadata.
- [x] Should Trace/Debug external review be rerun through the explicit scillm contract-correct model path, using cropped screenshots and longer/streaming timeout settings, instead of the generic review-design VLM lane that repeatedly returned HTTP 504? Resolved 2026-05-20: direct scillm `gpt-5.5` high-reasoning image review passed after Deploy was visibly blocked.

## Key Files

| File | Purpose |
|------|---------|
| `examples/standing-agent-sample/project_agent_client.py` | Project-agent consumer sample for `/v1/scillm/agents/*` reviewer handoffs |
| `examples/standing-agent-sample/sample_module/greeter.py` | Intentional bug fixture reviewed by the standing-agent sample |
| `config/scillm-agents.yaml` | Standing worker registry (`scillm-reviewer`, `standing-agent-sample`) |
|
| `src/scillm/proxy/app.py` | Main FastAPI proxy; includes `/v1/scillm/batch/completions` and `/v1/scillm/model-pools` |
| `src/scillm/proxy/opencode_transport.py` | Transport v1 state, delivery, sync message |
| `src/scillm/proxy/opencode_transport_stream.py` | SSE relay + reasoning/tool/permission event stream |
| `src/scillm/proxy/opencode_transport_events.py` | Normalize OpenCode bus → monitor events |
| `src/scillm/proxy/opencode_transport_api.py` | `/v1/scillm/opencode/transport/*` routes |
| `src/scillm/proxy/providers/opencode_go.py` | OpenCode Go routing seam, live model parsing, `/messages` adapter without default `max_tokens` |
| `src/scillm/proxy/chutes_direct.py` | Direct Chutes passthrough (no middleware): hot check, single completion, batch with semaphore+retry+as_completed |
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
| max_tokens / max_completion_tokens | Causes truncated or empty visible output on reasoning providers | Never set them for provider-bound reasoning calls unless the caller has a specific cap contract; scillm strips unsafe caps before routing where appropriate |
| Assuming raw chat streams by default | Raw `/v1/chat/completions` remains OpenAI-compatible and blocking unless `stream: true` is set | Use `stream: true` explicitly or call the scillm Python batch helpers for large prompt batches |
| Fire-all-at-once batching | >4 requests causes queue timeout | Use CHUNK_SIZE=4 loop |
| Manual Chutes/OpenCode splitting for QRA throughput | Reimplements scheduling inconsistently across agents | Use `POST /v1/scillm/batch/completions/stream` with `qra-deepseek-pool` |
| Deprecated model names | `deepseek-ai/DeepSeek-V3` triggers abuse guard; legacy `text-*` compatibility aliases have been removed | Use explicit current models such as `gpt-5.5`, `oc-kimi`, `oc-glm`, `oc-deepseek`, `oc-qwen`, or `vlm` where documented |
| Blocking OpenCode transport `POST .../message` without stream | Hides reasoning/permission stalls; HTTP read timeout looks like failure | Use `stream: true` (default); watch `reasoning_delta` / `permission_requested` in SSE or `events.jsonl`; `timeout_s` = stream budget |
| Treating chat timeout/SSE rules as `cursor_exec` progress | Causes false "stuck" kills during long tool stretches or mis-tuned orchestrator timeouts | Tail `events.jsonl` / `cursor-events.jsonl`; success = terminal `result` event; use `timeout_s`/`idle_timeout_s` as backstop only; see `docs/SCILLM_EXEC.md` |
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

## Agent Takeover Notes

### Goals (updated 2026-05-30)

**North star:** scillm is the single OpenAI-compatible gateway (`localhost:4001`) plus bounded **exec** and **agents** runtimes for Embry OS project agents — with a **memory-first harness** that owns turn truth (not Codex thread history).

| Layer | Goal | Success looks like |
|-------|------|-------------------|
| **Proxy (chat/batch)** | Route all LLM traffic with policy, fallbacks, logging, streaming liveness | Callers use family profiles or pools; `X-Caller-Skill` always set; no silent batch failures |
| **Exec (`/v1/scillm/exec`)** | Deterministic DAG/graph workers (`local_command`, `codex_exec`, `pi_exec`, `opencode_exec`, `cursor_exec`) | Hash-bound node evidence, runtime actions, amend overlays; long `cursor_exec` runs complete via stream-json + `cursor-events.jsonl` |
| **Agents (`/v1/scillm/agents/*`)** | Standing workers: handoff → lease → turn → result/steer | `examples/standing-agent-sample/` works; visible-subagent path accepted (Codex app-server, not tmux-only) |
| **Memory harness** | `harness_turns` + skills pipeline → compile → `exec/graph` → validate → turn-close | `scillm harness loop` closes turns in memory; phases 01–07 accepted |
| **Harness product path** | Caller-facing amend loops, generic NLP planner, operator docs, `progress_events` contract | Phases 08–09 **complete**; 10–12 implemented with gate scripts; E1/E2/E3/E4/E8/E9 runnable in multi-example suite |
| **DAG viewer-editor** (parallel track) | Visual, evidence-gated orchestration over `dag.json` + plan-iterate | Accepted through 2026-05-21 domain-review-loop; collaboration workbench phase `ready_for_review` |

**Architecture locks (do not regress):**
- Turn truth = `/memory` (`harness_turn.v1`, `recall_fusion.v1`), not worker transcripts.
- Tier routing: **chat** = one-shot; **exec** = pipelines (not product code authorship); **agents** = standing workers; harness picks model/actuator per turn.
- `cursor_exec` liveness ≠ chat SSE — monitor `events.jsonl` + `cursor-events.jsonl`; terminal `result` event is success.

### Phase status — memory-harness-v2 + product path

| Phase | ID | Status (2026-05-30) |
|-------|-----|---------------------|
| 1 | `phase-01-memory-write-contract` | **accepted** |
| 1.5 | `phase-015-coding-delegation-proof` | **accepted** |
| 2 | `phase-02-semantic-dag-compiler` | **accepted** |
| 2.5 | `phase-025-agent-transport-adapter` | **accepted** (Codex-free gate path) |
| 3 | `phase-03-skills-select-injection` | **accepted** |
| 4 | `phase-04-skill-adapters` | **accepted** |
| 5 | `phase-05-full-turn-dag` | **accepted** (`turn_loop.py` exists) |
| 6 | `phase-06-real-world-harness-e2e` | **accepted** |
| 7 | `phase-07-monitor-harness` | **accepted** |
| 8 | `phase-08-product-path-amend-loop` | **complete** |
| 9 | `phase-09-caller-facing-loop` | **complete** |
| 10 | `phase-10-generic-nlp-planner` | **implementation + gate script** (no `PHASE_STATUS.json` ledger yet) |
| 11 | `phase-11-operator-readiness` | **pending formal close** (gate script exists) |
| 12 | `phase-12-progress-events-amend-loop` | **implementation + gate script** (E3 scenario in multi-example suite) |

Plan graph: `.plan-iterate/plans/memory-harness-v2/plan-graph.json` · stress goals: `GOALS-HARNESS.md` · entrypoint: `scillm harness loop`

### Exec & agents — verified surfaces (2026-05-30)

| Surface | Status |
|---------|--------|
| `POST /v1/scillm/exec` deterministic | **OK** — `tests/test_exec_e2e.py` 6/6; `scripts/sanity_exec_endpoints.sh` |
| `cursor_exec` stream >64KiB | **OK** — `_iter_subprocess_text_lines`; fake-agent probe + live curl; `ExecRun.emit` regression fixed |
| `cursor_exec` production | **Use with care** — real jobs need terminal `result` in `cursor-events.jsonl`; pdf_oxide poll may pick wrong events file by mtime (separate fix) |
| `scillm exec` CLI | **OK** — routes to `/v1/scillm/exec` profiles |
| `/v1/scillm/agents/*` | **Accepted** — handoff/lease/turn/steer; standing-agent sample |
| Full harness e2e | **Accepted** phase 06 — not “only slices” anymore |

### Outstanding (prioritized)

**P0 — closure / drift**
- [ ] **Formalize phases 10–12** in plan-iterate (`PHASE_STATUS.json`, external review if required) — code + `run_phase_10_*` / `run_phase_12_*` gates exist but ledgers lag.
- [ ] **`phase-dag-collaboration-workbench`** is `ready_for_review` — may **BLOCK** `plan-iterate continue` until reviewed/accepted or explicitly superseded.
- [ ] **Large uncommitted tree on `main`** — harness, exec, agents, middleware; commit when user requests.

**P1 — harness product scenarios**
- [ ] **E2** (operator-doc amend loop) — still `blocked_implementation` in `run_harness_multi_example_suite.sh`.
- [ ] **E5** (two-turn memory recall planner) — still blocked.
- [ ] Re-run full **`./scripts/run_harness_multi_example_suite.sh`** and archive `suite_results.json` after E3/E9 changes land.

**P2 — exec hardening**
- [ ] **pdf_oxide / poll clients:** `find_cursor_events_path()` should prefer `run_id`-scoped `cursor_events_path`, not newest mtime glob.
- [ ] Optional: one **real** short `cursor_exec` canary (not fake-agent) after deploy.

**P3 — parallel tracks**
- [ ] **DAG viewer-editor collaboration workbench** — close review phase or defer with explicit supersede.
- [ ] **Visible subagent** — accepted for app-server steering; mid-run human steering beyond first slice is historical unless re-opened.
- [ ] **Proxy / OAuth** — Claude OAuth may be expired; check `/v1/scillm/auth` before Claude-dependent work.

### Next agent-executable actions

1. `curl -s http://127.0.0.1:4001/health/liveliness` — proxy up.
2. `./scripts/run_phase_05_e2e_gates.sh` … `run_phase_09_caller_facing_e2e_gates.sh` — confirm still PASS after local changes.
3. `./scripts/run_phase_10_generic_planner_e2e_gates.sh` and `./scripts/run_phase_12_progress_events_e2e_gates.sh`.
4. `./scripts/run_harness_multi_example_suite.sh` — refresh multi-example evidence.
5. Init/close plan-iterate ledgers for phases **10–12**; resolve **dag-collaboration-workbench** blocker.
6. Update `local/HANDOFF.md` (still 2026-05-28; says phase 05 not started).

### Evidence pointers

| Artifact | Path |
|----------|------|
| Plan graph | `.plan-iterate/plans/memory-harness-v2/plan-graph.json` |
| Harness goals | `GOALS-HARNESS.md` |
| Turn loop | `src/scillm/harness/turn_loop.py` |
| Harness CLI | `src/scillm/cli.py` (`harness loop`, `exec`) |
| Exec contract | `docs/SCILLM_EXEC.md` |
| Multi-example suite | `scripts/run_harness_multi_example_suite.sh` → `.plan-iterate/harness-multi-example-validation/` |
| Cursor stream fixture | `scripts/test_fixtures/fake_cursor_stream_agent.py` |
| Handoff (stale) | `local/HANDOFF.md` |

### Last verified (2026-05-30)

- `curl http://127.0.0.1:4001/health/liveliness` → ok
- `tests/test_exec_e2e.py` → 6 passed
- `tests/test_exec_cursor_stream_http.py` + live `POST /v1/scillm/exec` cursor stream probe → completed / stream_completed
- Phase ledgers: 01–07 **accepted**; 08–09 **complete** (per `PHASE_STATUS.json`)

## Goal History

### Active Goal: Evidence-gated DAG viewer-editor workspace

Summary: make multi-agent orchestration in `$scillm` collaborative, visual, and
less opaque. The DAG viewer-editor should let the human and project agent start
from `dag.json`, see exactly where the project agent is inside `$plan-iterate`,
understand the orchestration at a glance, stop/edit/resume at meaningful nodes,
and inspect/amend the same DAG JSON that `$plan-iterate` executes.

Current phase pointer: `.plan-iterate/phase-20260521-domain-review-loop-ledger-current-tree`
is accepted/valid. It follows accepted runtime-control, UI-control,
active-follow, amendment-apply, direct `dag.json` execution/catalog provenance,
and current-tree executable review-loop ledger phases.

Requirements:
- Show the current `$plan-iterate` position dynamically: active phase, active
  node, current round, running reviewer/test/model calls, blocked gates, and
  next expected action.
- Make the orchestration readable at a glance: node type, owner/agent, model,
  contract, status, dependencies, evidence state, and closure authority must be
  visible without reading raw JSONL.
- Support controlled intervention: human/project agent can stop, pause, edit,
  disable, duplicate, resume, or rerun appropriate nodes without breaking the
  evidence ledger.
- Make `dag.json` the authoritative instruction object: easy to view,
  copy/export, validate, diff, and feed directly into `$plan-iterate` as the
  replayable execution graph.
- Preserve immutable project goals separately from the mutable DAG plan. Each
  `$plan-iterate` iteration may produce a new/amended DAG plan, but it must be
  derived from and checked against the immutable goals.
- Show plan lineage: original DAG, current executing DAG, proposed amendment,
  accepted/rejected changes, and the iteration/round that produced each plan.
- Render node/subtree state with NVIS-compliant EmbryStyle status semantics from
  `$ux-lab`: green=nominal/admissible, red=critical/blocked, amber=warning or
  needs acknowledgement, blue=info/selected, with redundant text/icons so color
  is not the only signal.
- The minimum top status bar for the DAG page must show: immutable goal summary,
  active DAG version, current iteration/max iterations, current active
  node/subtree, run state, evidence gate state, amendment state, and next safe
  action.
- Graph auto-positioning follows active execution by default: root before start,
  active node/branch while running, blocked node/subtree when paused or blocked,
  and closure gate at completion. Manual pan/zoom/node selection/older-iteration
  browsing suspends auto-follow until the user selects `Follow active node`.
  Critical blockers should surface as queue/alert items without stealing the
  viewport while follow mode is suspended.
- Use a constrained `Next actions` queue instead of a broad control queue: show
  one primary next safe action, up to three secondary pending actions, and hide
  lower-priority choices behind `More actions`. Actions should be contextual to
  the selected node/subtree or current run state. The graph supports selecting,
  adding, focusing, and expanding/collapsing nodes/subtrees; detailed editing of
  node fields, review scopes, models, contracts, prompts, and amendment
  rationale belongs in the selected-node data pane.
- Adding nodes should start from editable templates, not blank free-form nodes.
  Dropdown catalogs should expose node types, review types, models, personas/
  agents, contracts, prompt presets, proof levels, and best-practice skill sets.
  The data pane must support editing, duplicating, saving, and reusing these
  templates/catalog entries.
- Templates exist to reduce human and project-agent error by making DAG nodes
  typed, validated, and easily composable. Treat templates as LEGO-like
  orchestration blocks: each entry should define required fields, compatible
  upstream/downstream node types, default contracts/prompts/models/proof gates,
  and validation rules so nodes connect predictably instead of relying on
  free-form human or agent improvisation.
- Keep bespoke DAG editor code to the absolute minimum. Prefer catalog-driven
  schemas, generic node renderers, shared validation rules, and reusable form
  controls over custom per-node UI/logic. New node types should usually be
  added by changing catalog/schema data, not by writing a new bespoke component.
- Template/catalog state should be centralized and shared by all project agents:
  `$scillm` serves the runtime/catalog endpoints used by the DAG viewer-editor,
  while `$memory` stores durable catalog records and provenance. `dag.json`
  stores selected catalog ids plus explicit inline overrides, not a private copy
  of the full catalog.
- Loaded presets are editable starting points, not locked copies. After a human
  or project agent selects a template/preset for a node, they can customize the
  node-specific fields in the data pane or JSON editor. Those customizations
  should be represented as explicit inline overrides against the pinned catalog
  entry so the shared preset remains reusable and provenance stays clear.
- Distinguish planned graph, running graph, completed evidence, and draft
  amendments so edits cannot launder historical execution results.
- Phase ledgers remain valid under `$plan-iterate validate`.
- Domain review loops record `state.json`, `events.jsonl`, aggregate verdict,
  matrix, best-practices inputs, and three per-round plans.
- UI claims are backed by fresh screenshots, not DOM assertions alone.
- Reviewer PASS is treated as a receipt; deterministic evidence and phase
  comparison decide closure.
- Stale or contradictory reviewer/phase states are preserved as history and
  marked superseded/resolved, not deleted.
- The next phase is explicit before implementing additional DAG editor polish
  or runtime graph mutation.

Status: proposed minimum accepted. The accepted template/catalog/runtime-artifact
phase removed fake live controls and proved read-only runtime evidence.
Follow-on accepted phases implemented real runtime control, wired the DAG
viewer-editor controls, added active execution follow, applied approved
amendments as provenance overlays, proved direct `dag.json` execution with
catalog/preset provenance, and refreshed the executable domain-review-loop
ledger against the current tree.

Evidence:
- `.plan-iterate/phase-20260520-dag-viewer-editor-ux-contract/PHASE_STATUS.json`
- `.plan-iterate/phase-20260520-dag-viewer-editor-ux-contract/domain-review-loops/review-design-modern-ref-rerun-20260520/DESIGN_REVIEW_ITERATE_MATRIX.md`
- `.plan-iterate/phase-20260520-dag-viewer-editor-ux-contract/evidence-artifacts/review-design-rerun-modern-ref-20260520T234218Z/semantic-after-patch-test-interactions-r19/results.json`
- `.plan-iterate/phase-20260521-dag-template-catalog-contract/PHASE_STATUS.json`
- `.plan-iterate/phase-20260521-dag-template-catalog-contract/evidence-artifacts/ui-proof/runtime-artifacts/proof-after-control-fix.json`
- `.plan-iterate/phase-20260521-dag-template-catalog-contract/reviews/20260521T142530Z-scillm-gpt55-high-response.md`
- `.plan-iterate/phase-20260521-dag-runtime-control-contract/PHASE_STATUS.json`
- `.plan-iterate/phase-20260521-dag-runtime-control-ui-wire/PHASE_STATUS.json`
- `.plan-iterate/phase-20260521-active-execution-follow-and-status/PHASE_STATUS.json`
- `.plan-iterate/phase-20260521-approved-amendment-apply-runtime/PHASE_STATUS.json`
- `.plan-iterate/phase-20260521-dag-json-execution-catalog-closure/PHASE_STATUS.json`
- `.plan-iterate/phase-20260521-domain-review-loop-ledger-current-tree/PHASE_STATUS.json`

Archived decision item: run human/project-agent canary testing against the accepted
ledgers before opening low-severity UX polish or production-hardening work.
Direct `dag.json` execution, shared catalog provenance, and the current-tree
executable review-loop ledger are accepted for the proposed minimum.

### Archived Goal: Fresh DAG viewer-editor real-world canary

Summary: prove the DAG viewer-editor works end to end against a fresh real
`dag.json` that is also executed by `/v1/scillm/exec/graph`, including live
active-branch visibility, runtime pause/resume/stop, editable review-code
fanout node data, Memory amendment persistence, formal diff, rendered JSON, and
visual proof artifact.

Status: accepted. The phase found and fixed a runtime-control bug in
`src/scillm/proxy/exec_api.py`: active run registration was scoped around
creating `StreamingResponse`, so live graph runs could be deregistered before
the response stream completed. Stream registration now wraps the iterator so
runtime actions can target the active run until streaming ends.

Evidence:
- `.plan-iterate/phase-20260521-dag-viewer-editor-realworld-canary/PHASE_STATUS.json`
- `.plan-iterate/phase-20260521-dag-viewer-editor-realworld-canary/evidence-artifacts/canary-proof-summary.md`
- `.plan-iterate/phase-20260521-dag-viewer-editor-realworld-canary/evidence-artifacts/test-interactions-realworld-canary-r3/results.json`
- `.plan-iterate/phase-20260521-dag-viewer-editor-realworld-canary/evidence-artifacts/live-run-final-status-r3.json`
- `/tmp/phase-20260521-dag-viewer-editor-realworld-canary-accepted.zip`

Caveat: the final live run is intentionally `cancelled` because the canary
presses Stop as the last runtime-control check. Some selector-container crops
are blank white, so accepted visual proof uses the inspected full-step
screenshots rather than those crops.

Follow-up sanity proof: a separate happy-path e2e canary now proves the DAG
viewer-editor is not obviously broken in normal use. It runs
`phase-20260521-dag-viewer-editor-happy-path-sanity-run-r2` to completion,
passes 9/9 browser interactions, saves and refreshes a Memory amendment, shows
the formal diff/rendered JSON, and ends with backend state `completed` and all
four nodes hash-bound. Evidence:
`.plan-iterate/phase-20260521-dag-viewer-editor-realworld-canary/evidence-artifacts/happy-path-sanity-proof.md`

### Prior Goal: DAG viewer-editor amendment action proof

Summary: prove that the DAG viewer-editor can show Memory-backed amendment
records and support bounded approve/reject/supersede interaction testing
without implying production graph mutation.

Requirements:
- Use real API-backed amendment records, not static dashboard placeholders.
- Prove approve and reject actions with `$test-interactions` and screenshots.
- Keep approved-amendment application to the authoritative runtime graph out of
  scope until a separate phase owns that authority.

Status: externally reviewed/pass for bounded testing; not accepted as runtime
graph mutation.

### Prior Goal: Hash-bound exec node output evidence

Summary: fix the producer/runtime gap where completed scillm exec DAG nodes did
not emit hash-bound output evidence, causing the viewer to fail closed.

Requirements:
- Completed nodes emit `output_hash`, `output_artifact`,
  `output_hash_algorithm`, and `evidence_status`.
- Viewer fails closed when required node hash/artifact evidence is absent.
- Real endpoint/snapshot proof shows no missing required output hashes.

Status: resolved for the scoped exec runtime/viewer evidence path.

## Agent verification policy (2026-06-03)

Mocks/unit tests alone are insufficient. All scillm/integration changes must be verified with **live E2E sanity checks** (real :4001/:4098, restart Docker after code changes, existing sanity scripts). Report mocked vs live explicitly before claiming something works.

