<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="local/artifacts/logo/SciLLM_balanced.dark.svg" />
    <img src="local/artifacts/logo/SciLLM_balanced.animated.light.svg" alt="scillm" width="400" />
  </picture>
</p>

<h3 align="center">One proxy. Any provider. Zero glue code.</h3>
<p align="center">Agents and humans call <code>/scillm</code> — it routes, retries, and repairs.</p>

---

Single-tenant, OpenAI-compatible proxy + `/scillm` skill. Originally forked from [litellm](https://github.com/BerriAI/litellm) by BerriAI, then rewritten from scratch — the proxy, router, batch iterator, and middleware are all new code (~4,300 lines) that calls providers directly via the OpenAI SDK. No litellm code runs. Point any OpenAI SDK at `localhost:4001` and get routing, failover, wall-time retries, JSON repair, batch pairing, and auto multimodal handling — without provider-specific glue code.

## Quick Start

```bash
# 1. Configure API keys in .env
#    Required: CHUTES_API_KEY, CHUTES_API_BASE
#    Optional: DEEPSEEK_API, GEMINI_API_KEY, OPENROUTER_API_KEY, MOONSHOT_API_KEY

# 2. Build and start (proxy + Redis)
docker compose up -d --build

# 2b. Or include local Ollama for offline models
docker compose --profile local up -d --build

# 3. Verify
curl -s http://localhost:4001/health/liveliness
# → {"status": "ok"}

# 4. Call it
curl http://localhost:4001/v1/chat/completions \
  -H "Authorization: Bearer sk-dev-proxy-123" \
  -H "Content-Type: application/json" \
  -d '{"model":"text","messages":[{"role":"user","content":"hi"}],"max_tokens":16}'
```

Makefile shortcuts: `make proxy-rebuild` (build+start), `make proxy-up`, `make proxy-down`, `make proxy-logs`

**Container details:** `python:3.12-slim`, `uvicorn` on port 4001, `network_mode: host` (for local Ollama access), config mounted from `local/proxy_server_config.yaml`, health check every 15s. Redis included for caching and request logging. Ollama available via `--profile local`.

## Security

scillm is designed for **local development and trusted networks**. The default configuration uses:

- A static bearer token (`sk-dev-proxy-123`) — set `LITELLM_MASTER_KEY` in `.env` to override.
- `network_mode: host` — required for local Ollama access. In production, switch to port mapping (`-p 4001:4001`) behind a reverse proxy with TLS.
- API keys in `.env` — acceptable for dev. In production, use Docker Secrets, Vault, or your cloud's secrets manager.

scillm does not implement multi-tenant auth, key rotation, or per-client access control. It's designed for single-user research and engineering workflows.

## What You Get

Every provider scillm targets speaks OpenAI-compatible API (`/v1/chat/completions`). Provider-specific handler code is unnecessary for this use case. Adding a provider is 5 lines of YAML. The new code is ~4,300 lines total.

- **Wall-time retry budget** — Providers with hot/cold cycling return 503 now but may be back in 20 seconds. Fixed retry counts fail here. scillm retries on a clock — keep trying for 90 seconds, not 3 attempts.
- **Batch iterator with request-response pairing** — Fire 200 parallel requests, results arrive out of order. scillm's `as_completed` iterator pairs every response with its original request.
- **JSON repair inside retry loop** — Trailing commas, missing braces, markdown fences wrapping JSON. scillm repairs it instead of wasting the call.
- **Auto multimodal routing** — Pass an image and scillm figures it out. No model selection needed — the proxy detects images in messages and reroutes to a vision model automatically.
- **Bounded concurrency queue** — Chutes.ai has a 5-connection limit. Exceed it and you get a 429 with a 90-second penalty. scillm queues overflow instead of rejecting it.
- **Source grounding verification** — Pass source text, scillm verifies the response is grounded using fuzzy matching, retries with progressive prompts if not.
- **Fallback cascade with circuit breaker** — `text` → `text-deepseek` → `text-gemini`. 3 failures trigger a 20-second cooldown per group.
- **5xx-specific backoff** — Server errors (503) get different retry timing than rate limits (429).

## How to Call It

### `/scillm` skill

Agents and humans invoke `/scillm` the same way — no API calls, no key management, no provider selection:

```
/scillm "Explain quantum computing in one sentence"
/scillm "Analyze results/chart.png and explain the trends"
```

Reference file paths inline and scillm handles the rest — reads the file, base64-encodes it, picks a vision model, routes the request. Works across Claude Code, Codex, Gemini, KiloCode, Kimi, and any agent that supports slash commands.

### OpenAI SDK

The proxy speaks standard OpenAI. Any existing code or SDK works — just point it at `localhost:4001`:

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:4001/v1", api_key="sk-dev-proxy-123")
r = client.chat.completions.create(model="text", messages=[{"role": "user", "content": "hi"}])
```

Both paths hit the same Docker proxy at `localhost:4001`.

## Parallel Batch Completions

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

<p align="center">
  <img src="local/artifacts/scillm_architecture.svg" alt="scillm architecture diagram" width="700" />
</p>

## Model Groups

Callers say `model: "text"` — the proxy picks the provider. When models change, update the config. Callers never change.

| Group | Provider | Model | Fallback chain |
|-------|----------|-------|----------------|
| `text` | Chutes | DeepSeek-V3 | → text-deepseek → text-gemini |
| `vlm` | Chutes | Qwen3-VL-235B | → vlm-openrouter |
| `local-text` | Ollama | qwen2.5:0.5b | (none) |
| `moonshot-text` | Moonshot | kimi-k2 | (none) |

20+ aliases map provider-native names to groups.

## Adding a Provider

5 lines of YAML. Zero code changes.

```yaml
- model_name: new-provider
  params:
    model: provider-native/model-name
    api_base: https://api.newprovider.com/v1
    api_key: os.environ/NEW_PROVIDER_KEY
    timeout: 45
```

Optionally add to fallback cascade, then `make proxy-rebuild`.

**Constraint:** Provider must speak `/v1/chat/completions`. Most do.

## Testing

```bash
make smokes-cli-fast    # Quality gate: proxy-only smokes
make smokes             # Full suite including Ollama preflight
make test-e2e           # 24 pytest e2e tests against live proxy
make test-adversarial   # 44 adversarial tests (auth, streaming, grounding, edge cases)
```

Test files: `tests/test_proxy_e2e.py` (contract tests), `tests/test_proxy_adversarial.py` (edge cases), `tests/test_grounding.py` (unit tests for grounding helpers).

## Composable Skill

`/scillm` is a composable building block in a system of 230+ agent skills. Any skill that needs an LLM completion calls `/scillm` — it handles provider selection, retries, and repair. Skills chain together to build pipelines:

<p align="center">
  <img src="local/artifacts/scillm_composable_skill.png" alt="scillm composable skill diagram" width="700" />
</p>

Adding a provider or changing a model in the proxy config updates every skill in the chain at once. No skill needs to know which provider is behind `model: "text"`.

## Ops Endpoints

| Endpoint | What it tells you |
|----------|------------------|
| `GET /health/liveliness` | Is the proxy alive? |
| `GET /v1/scillm/health` | Model groups, fallback chains, retry policy, concurrency slots |
| `GET /v1/scillm/models` | Deployed models with group membership |
| `GET /v1/models` | OpenAI-compatible model list |
| `GET /v1/budget` | Current daily spend and remaining budget |
| `GET /metrics` | Prometheus counters (requests, errors, latency by group) |

## Appendix: Relationship to litellm

scillm is not a replacement for litellm. litellm solves provider heterogeneity — normalizing 100+ APIs with different auth, streaming formats, and tokenizers into one interface. scillm doesn't need that because it only targets providers that already speak OpenAI. Different problems, different scope.

scillm started as a fork of [litellm](https://github.com/BerriAI/litellm) by BerriAI. The active codebase is a rewrite — the proxy, router, batch iterator, and middleware are all new code (~4,300 lines) that calls providers directly via the OpenAI SDK. No litellm code runs. The original litellm provider handlers, UI, docs, and tests (~290,000 lines) are archived in `.archive/`.

### What scillm learned from litellm

The routing concepts — fallback cascade, circuit breaker, model groups, YAML config — are informed by litellm's design. The implementation is new:

| Concept | litellm approach | scillm approach |
|---------|-----------------|-----------------|
| **Provider dispatch** | 100+ provider-specific handlers (auth, streaming normalization, tokenization, cost calculation) | `openai.AsyncOpenAI(base_url=provider)` — providers must speak OpenAI |
| **Fallback cascade** | `fallbacks` config with `allowed_fails` + `cooldown_time` circuit breaker | YAML groups with circuit breaker (same concept, reimplemented) |
| **Retry** | Count-based with `retry_after` header support, per-deployment cooldown, and timeout | Wall-time budget — keep trying for 90s, designed for serverless GPU cold starts |
| **JSON handling** | Pass-through with optional `response_format` schema validation | Multi-stage repair (`json_repair` lib + brace trimming) in middleware and batch loop |
| **Concurrency** | `max_parallel_requests` per deployment with failover to next deployment | Bounded queue with TTL (queues overflow instead of rejecting) |
| **Batch** | Router-level batching | Client-side `as_completed` iterator with request-response pairing |
| **VLM** | Native multimodal support per provider | Auto-detects `image_url` in messages, reroutes to vision model transparently |
| **Scope** | Enterprise gateway: 100+ providers, multi-tenant auth, SSO, admin UI, observability integrations | Single-tenant proxy for agents: 6 providers, one YAML file, Prometheus metrics |

### What scillm adds

- **Source grounding verification** — fuzzy-match response against source text, retry with progressive prompts
- **JSON repair in middleware and batch loop** — attempt to fix broken JSON before discarding the response
- **Wall-time retry budget** — designed for serverless GPU providers with hot/cold cycling
- **VLM auto-routing** — callers use one model group for everything; proxy detects images and reroutes to a vision model
- **Batch request-response pairing** — async iterator that pairs every response with its original request and metadata

## License

scillm originated as a fork of [litellm](https://github.com/BerriAI/litellm) by [BerriAI](https://berri.ai), licensed under the MIT License. The active codebase is a rewrite; the original litellm code is archived but included for attribution.
