## SciLLM Notebooks

This directory contains self‑contained, runnable notebooks that cover three tracks:
- OpenAI‑compatible gateways (e.g., Chutes) for JSON/chat, VLM, batching, and routing.
- Native provider basics (OpenAI, Anthropic, Perplexity) to verify direct SDK‑style flows.
- Agents and bridges (Codex agent, Mini‑agent, CodeWorld, Certainly) to validate end‑to‑end tooling.

Executed snapshots live under `notebooks/executed/` for “known‑good” reference outputs.

### What To Run First
- 00_quickstart.ipynb — First success (strict JSON) using the Auto Router for any OpenAI‑compatible base you provide. If `CHUTES_API_BASE`/`CHUTES_API_KEY` are set, it calls Chutes; otherwise, set your own compatible base/key.
- 14_provider_matrix.ipynb — Provider sanity across OpenAI, Anthropic, Perplexity (skips sections if keys are missing).

### Recommended Environment (once)
- General stability (applies to all notebooks)
  - LITELLM_MAX_RETRIES=3 LITELLM_RETRY_AFTER=2 — sensible retry/backoff
  - Optional async: SCILLM_DISABLE_AIOHTTP=1 LITELLM_TIMEOUT=45
  - Strict JSON helpers: SCILLM_REQUIRE_NONEMPTY_KEYS=title (maps ""→null for the key)
- OpenAI‑compatible gateways (Chutes & similar)
  - SCILLM_CHUTES_CANONICALIZE_OPENAI_AUTH=1 — stable auth (Bearer→x‑api‑key on 401; cached per base)
  - SCILLM_COOLDOWN_429_S=120 SCILLM_RATE_LIMIT_QPS=2 — avoid burst 429s

### Notebook Catalog (basic → advanced)
- Fundamentals
  - 00_quickstart.ipynb — Auto Router “hello JSON” for OpenAI‑compatible bases.
  - 01_chutes_openai_compatible.ipynb — Direct JSON + Router fallbacks + Auto Router (applies to any compatible base; Chutes is the example).
- Reliability & Batch (OpenAI‑compatible)
  - 02_router_parallel_batch.ipynb — Parallel fan‑out; Router + Fallbacks (Text & VLM).
  - 03_model_list_first_success.ipynb — First‑success across an explicit model list.
- Advanced Features (formatting & streaming)
  - 04a_tools_only.ipynb — Function/tool calling (smoke‑safe, non‑stream).
  - 04b_streaming_demo.ipynb — Streaming demo (gated; enable when you want to experiment).
- Agents / Bridges
  - 05_codex_agent_doctor.ipynb — Codex agent health + minimal JSON chat.
  - 06_mini_agent_doctor.ipynb — Mini‑agent health.
  - 07_codeworld_mcts.ipynb — CodeWorld MCTS demo.
  - 08_certainly_bridge.ipynb — Certainly bridge doctor.
- Providers (native)
  - 11_provider_perplexity.ipynb — Perplexity Sonar basics.
  - 12_provider_openai.ipynb — OpenAI basics.
  - 13_provider_anthropic.ipynb — Anthropic basics.
  - 14_provider_matrix.ipynb — Matrix sanity across providers (skips if missing keys).
  - 09_fallback_infer_with_meta.ipynb — Fallback + attribution (served model in meta) — provider‑agnostic pattern.

### Executed Snapshots
- Location: `notebooks/executed/*.ipynb`.
- Regeneration: `make notebooks-smoke` executes core notebooks and writes fresh “_executed” snapshots (a small metadata cell annotates key envs such as timeouts and retries).

### Troubleshooting
- General
  - Missing keys — notebooks skip or print a friendly hint; set keys in your shell and rerun.
  - Timeouts/hangs — prefer httpx‑only async: `SCILLM_DISABLE_AIOHTTP=1` and set `LITELLM_TIMEOUT`.
  - Strict JSON content issues — enable `SCILLM_REQUIRE_NONEMPTY_KEYS` to coerce empty string values to null for selected keys.
- OpenAI‑compatible bases (Chutes & similar)
  - 401 — enable `SCILLM_CHUTES_CANONICALIZE_OPENAI_AUTH=1` (auto Bearer→x‑api‑key switch on 401). Use the “curl header check” cell in 00_quickstart.
  - 429/503/“capacity” — expected under load; retries back off and Router failover selects alternates automatically.

### FAQ
- Do we need 00_quickstart.ipynb?
  - Yes. It’s the fastest way to prove the environment is wired before using richer notebooks. Keep it short and runnable; link it from onboarding.

- Router vs. model_list vs. direct completion — when to use which?
  - Router: default for reliability. It cools down saturated deployments and fails over automatically. Use for production paths and batches.
  - model_list (direct completion with `model_list=[...]`): simple “first‑success” without a Router instance. Good for quick scripts.
  - Direct completion (single model): use only for smoke checks or when you must target one fixed deployment.

- How should I choose alternates (fallback models)?
  - Prefer same family/tier first (e.g., Qwen3‑235B‑A22B → Qwen3‑235B‑A22B‑Thinking for VLM; Kimi‑K2 → DeepSeek‑V3.1 for text). Keep context windows comparable.
  - Or skip manual selection: use `auto_router_from_env(kind='text'|'vlm')` to discover and rank candidates automatically.

- Can I target multiple Chutes bases (regions/orgs)?
  - Yes. Set numbered envs: `CHUTES_API_BASE_1`, `CHUTES_API_KEY_1`, `CHUTES_API_BASE_2`, `CHUTES_API_KEY_2`, …
  - `auto_router_from_env(...)` will probe each base and build a combined model list ordered by availability/utilization.

- Why am I seeing 401 on an OpenAI‑compatible base?
  - Some gateways accept `x-api-key` but reject `Authorization: Bearer`. SciLLM normalizes automatically when `SCILLM_CHUTES_CANONICALIZE_OPENAI_AUTH=1`. Use the “curl header check” cell in 00_quickstart to confirm.

- How are capacity errors handled?
  - Responses with HTTP 429/503 or a body containing “capacity” are mapped to a retryable RateLimitError. Set `LITELLM_MAX_RETRIES`/`LITELLM_RETRY_AFTER`. Router cools down the deployment and selects the next alternate.

- Streaming sometimes leaves “Unclosed client session” warnings — what now?
  - Use httpx‑only async: `SCILLM_DISABLE_AIOHTTP=1` and keep `LITELLM_TIMEOUT` set (e.g., 45). Prefer non‑streaming first for reliability; see 04b for manual streaming.

- Where do executed notebooks live and why keep them in git?
  - Under `notebooks/executed/`. They provide “known‑good” evidence with short output slices. Useful for onboarding and quick diffs in CI.

- How do I run these notebooks reliably?
  - Ensure the virtualenv is active and env keys are exported. For a quick sweep, run `make notebooks-smoke` to rebuild and execute a curated set.

- Are tokens/secrets printed anywhere?
  - No. The snippets avoid printing tokens; logging emits header style names only (never values). Avoid committing local cells that print secrets.

- How do I get reproducible runs over time?
  - Pin `httpx`/provider SDK versions in your environment and set deterministic limits (`LITELLM_TIMEOUT`, retries). For fully deterministic CI, capture and replay small fixtures or keep reliance on live providers minimal.

- VLM content format — raw OpenAI “content list” or simplified text?
  - Use raw OpenAI “content” lists for VLM (see 02 and 01) so the same payload works across providers/gateways. The Router path supports both text and VLM payloads.

- Where to report issues?
  - Open an issue in this repo with the smallest failing cell and the first ~20 lines of stderr, plus your `CHUTES_*` surface (redact tokens). Mention which notebook and cell number.
