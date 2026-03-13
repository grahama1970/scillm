---
name: scillm
description: >
  LLM completions (text and VLM) via the scillm Docker proxy on localhost:4001.
  One endpoint: POST /v1/chat/completions (standard OpenAI-compatible).
  Models: text, vlm, local-text.
  Built on litellm by BerriAI — single-tenant fork with wall-time retries,
  JSON repair, source grounding, and batch request-response pairing.
allowed-tools: Bash, Read
triggers:
  - batch LLM calls
  - parallel completions
  - describe image
  - describe figure
  - describe table
  - VLM call
  - multimodal
  - extract JSON from
  - analyze image
  - LLM completion
  - preflight check
  - source grounding
  - grounding verification
  - verify grounded
metadata:
  short-description: scillm (proxy-first LLM completions via /v1/chat/completions)
provides:
  - llm-completion
composes: [task-monitor, create-evidence-case, analytics, create-figure]

taxonomy:
  - inference
  - llm
---

# scillm — One Endpoint for All LLM Calls

## How to Call

**POST `http://localhost:4001/v1/chat/completions`** — standard OpenAI format.
Use httpx, openai SDK, or curl. No pip install. No custom endpoints. No imports.

Auth: `Bearer sk-dev-proxy-123` (dev master key).

The proxy handles provider cascading, retries, JSON validation, VLM auto-routing,
concurrency limits, budget tracking, and optional Redis caching.

## Available Models

| Model | Backend | Use Case | Fallback |
|-------|---------|----------|----------|
| `text` | Chutes DeepSeek-V3 | General text, extraction, summarization | → text-deepseek → text-gemini |
| `vlm` | Chutes Qwen3-VL-235B | Image/figure/table description | → vlm-openrouter |
| `local-text` | Ollama qwen2.5:0.5b (local) | Smoke tests, always-on fallback | (none) |
| `moonshot-text` | Moonshot Kimi K2 | Alternative text provider | (none) |

Callers say `model: "text"` — the proxy picks the provider. 20+ aliases map provider-native names to groups.

---

## Single Call (Paved Path)

The easiest way — zero boilerplate:

```python
from scillm.paved import chat, chat_json, analyze_image, analyze_image_json

# Text
answer = await chat("What is the capital of France?")

# With system prompt
answer = await chat("Summarize AC-17", system="You are a compliance analyst.")

# JSON response (auto-validated, auto-repaired)
data = await chat_json("Return {name, age} for Alice who is 25")

# Image analysis (local file or URL — auto base64-encoded)
desc = await analyze_image("/path/to/diagram.png", "What does this show?")
desc = await analyze_image("https://example.com/chart.jpg", "Describe this chart")

# Image → structured JSON
data = await analyze_image_json("receipt.jpg", 'Extract {"total": number, "items": [...]}')
```

Defaults to `localhost:4001` + `sk-dev-proxy-123`. Override via env vars (`SCILLM_API_BASE`, `SCILLM_PROXY_KEY`, `SCILLM_MODEL`) or function kwargs.

### With httpx (no scillm import)

```python
import httpx

resp = httpx.post(
    "http://localhost:4001/v1/chat/completions",
    headers={"Authorization": "Bearer sk-dev-proxy-123"},
    json={"model": "text", "messages": [{"role": "user", "content": "What is 2+2?"}]},
    timeout=30.0,
)
content = resp.json()["choices"][0]["message"]["content"]
```

### With openai SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:4001/v1", api_key="sk-dev-proxy-123")
resp = client.chat.completions.create(
    model="text",
    messages=[{"role": "user", "content": "Hello"}],
    max_tokens=64,
)
print(resp.choices[0].message.content)
```

## JSON Response

Add `response_format` — the proxy auto-validates and retries on broken JSON:

```python
resp = httpx.post(
    "http://localhost:4001/v1/chat/completions",
    headers={"Authorization": "Bearer sk-dev-proxy-123"},
    json={
        "model": "text",
        "messages": [{"role": "user", "content": "Return {name, age} for Alice who is 25"}],
        "response_format": {"type": "json_object"},
        "max_tokens": 64,
    },
    timeout=30.0,
)
data = json.loads(resp.json()["choices"][0]["message"]["content"])
```

## Image Analysis (VLM Auto-Routing)

Send images with `model: "text"` — the proxy auto-detects `image_url` parts and
routes to VLM providers. No need to know the model name:

```python
import base64, httpx

with open("photo.png", "rb") as f:
    b64 = base64.b64encode(f.read()).decode()

resp = httpx.post(
    "http://localhost:4001/v1/chat/completions",
    headers={"Authorization": "Bearer sk-dev-proxy-123"},
    json={
        "model": "text",  # auto-routed to vlm when image detected
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Describe this image"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}],
        "max_tokens": 512,
    },
    timeout=60.0,
)
description = resp.json()["choices"][0]["message"]["content"]
```

## Message Formats

scillm accepts three forms for `messages` — use whichever is simplest for your use case:

| Form | When to use | Example |
|------|-------------|---------|
| **Plain string** | Single-turn text prompts | `"messages": "What is 2+2?"` |
| **Convenience fields** | Images/files (auto VLM routing) | `"messages": "Describe this", "file_path": "photo.png"` |
| **OpenAI array** | Multi-turn, system prompts, multimodal | `"messages": [{"role": "system", ...}, {"role": "user", ...}]` |

Plain strings are auto-wrapped as `[{"role": "user", "content": str}]`.
Convenience fields (`url`, `urls`, `file_path`, `paths`) auto-detect images, base64-encode local files, and route to VLM.
OpenAI-style arrays pass through unchanged — full control for multi-turn, system prompts, and explicit `image_url` parts.

## Batch Calls (Parallel Completions)

### Simple: asyncio.gather

Call the same endpoint concurrently. The proxy handles concurrency internally:

```python
import asyncio, httpx

async def complete(client, prompt):
    resp = await client.post(
        "http://localhost:4001/v1/chat/completions",
        headers={"Authorization": "Bearer sk-dev-proxy-123"},
        json={
            "model": "text",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 256,
        },
        timeout=45.0,
    )
    return resp.json()["choices"][0]["message"]["content"]

async def main():
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            complete(client, "What is 2+2?"),
            complete(client, "What is 3+3?"),
            complete(client, "What is 4+4?"),
        )
    return results
```

### Advanced: parallel_acompletions_iter (request-response pairing)

Async iterator that yields results as they complete, with every result carrying its original request.
`messages` accepts a plain string (auto-wrapped as `[{"role": "user", "content": str}]`) or
an OpenAI-style message array (required for multi-turn, system prompts, or multimodal/VLM content):

```python
from scillm.batch import parallel_acompletions_iter

requests = [
    {"messages": f"Summarize document {doc_id}", "metadata": {"doc_id": doc_id}}
    for doc_id in document_ids
]

async for result in parallel_acompletions_iter(requests, concurrency=6):
    doc_id = result["request"]["metadata"]["doc_id"]
    if result["ok"]:
        save(doc_id, result["response"])
    else:
        retry_queue.append(doc_id)
```

For multimodal (VLM), use convenience fields — scillm auto-detects images and routes to VLM:

```python
# Local file paths — auto base64-encoded, auto-routed to VLM
requests = [
    {"messages": "Describe this image", "file_path": "/path/to/photo.png"},
    {"messages": "What's in this diagram?", "paths": ["fig1.jpg", "fig2.png"]},
]

# URLs — auto-detected as image content
requests = [
    {"messages": "Describe this image", "url": "https://example.com/photo.jpg"},
]

# OpenAI-style image_url parts also work (for full control)
requests = [
    {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "Describe this image"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
    ]}]}
]
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

Pass a `source` field and scillm verifies the response is grounded using fuzzy token matching.
If the response doesn't meet the threshold, scillm retries with progressive prompts:

```python
from scillm.batch import parallel_acompletions

requests = [
    {
        "messages": "Summarize AC-17 requirements",
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
| `grounding_threshold` | `float` | 0.7 | Minimum fuzzy match score (0.0-1.0) to accept. |
| `grounding_retries` | `int` | 2 | Max retry attempts with progressive grounding prompts. |

Progressive retry strategy:
1. **Try 1:** Normal completion
2. **Try 2:** Appends "Base your answer strictly on the provided source text."
3. **Try 3:** Appends "Base your answer strictly on the provided source text. Quote directly from the source."

Results carry `grounding_score` (float) and `grounding_attempts` (int). Uses `rapidfuzz.fuzz.token_set_ratio` (~1ms per check).

## Hedged Calls (Race Two Models)

Client-side — fire two models, take the first response:

```python
async def hedged_call(client, prompt, primary="text", backup="text-gemini"):
    async def call(model):
        resp = await client.post(
            "http://localhost:4001/v1/chat/completions",
            headers={"Authorization": "Bearer sk-dev-proxy-123"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 256},
            timeout=30.0,
        )
        return resp.json()["choices"][0]["message"]["content"]

    done, pending = await asyncio.wait(
        [asyncio.create_task(call(primary)), asyncio.create_task(call(backup))],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()
    return await next(iter(done))
```

---

## Middleware Stack

The proxy runs these middleware components on every request:

| Middleware | File | Purpose |
|-----------|------|---------|
| **JSON Guard** | `json_guard.py` | Validates JSON when `response_format.type == "json_object"`. Attempts repair (brace trim + json_repair lib) before rejecting. Failed validation triggers cascade to next provider. |
| **Concurrency Guard** | `concurrency_guard.py` | Per-provider semaphore (chutes=4, ollama=1, etc). Queues excess requests instead of 429. Prevents Chutes 90s penalty. |
| **VLM Auto-Router** | `vlm_router.py` | Detects `image_url` parts in messages, rewrites text model to `vlm`. Callers don't need to know model names. |
| **Cache Init** | `cache_init.py` | Auto-detects Redis at startup (via REDIS_HOST/REDIS_URL). Enables caching if available, no-op otherwise. |
| **Budget Guard** | `budget_guard.py` | Tracks Chutes daily usage. Classifies 429s as budget vs throttle. |
| **Pricing** | `pricing.py` | Per-1k token cost estimation. |
| **Metrics** | `metrics.py` | Prometheus counters: calls, 429s, budget limits, retries. |

## Fallback Cascade

When a provider fails, the proxy cascades to the next group:

```
text (Chutes DeepSeek-V3) → text-deepseek (DeepSeek direct) → text-gemini (Gemini 2.5 Flash)
vlm  (Chutes Qwen3-VL)   → vlm-openrouter (Claude Sonnet)
```

Circuit breaker: 3 failures trigger a 20-second cooldown per group.

## Retry Policy

Per-exception-type retries across the cascade:

| Exception | Retries | Rationale |
|-----------|---------|-----------|
| 5xx (InternalServerError) | 8 | Provider will recover |
| 429 (RateLimit) | 6 | Back off but persist |
| Timeout | 6 | Retry aggressively |
| Auth (401/403) | 0 | Don't retry |
| BadRequest (400) | 0 | Don't retry |
| ContentPolicy | 0 | Don't retry |

With 3 providers in cascade (text -> text-deepseek -> text-gemini), effective retry
budget is ~8 retries x 3 providers = 24 attempts before final failure.

## Caching

Redis caching is auto-enabled when `REDIS_HOST` or `REDIS_URL` is set:
- Deploy with `compose.scillm.stack.yml` (includes Redis)
- TTL: `SCILLM_CACHE_TTL_SEC` (default 3600s)
- Namespace: `SCILLM_CACHE_NAMESPACE` (default "scillm")
- Core compose (no Redis) works fine — caching is optional

---

## Ops Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/health/liveliness` | GET | Is the proxy alive? |
| `/v1/scillm/health` | GET | Router health + fallback config + concurrency status |
| `/v1/scillm/models` | GET | Model groups, deployments, aliases |
| `/v1/models` | GET | OpenAI-compatible model list |
| `/v1/budget` | GET | Current daily spend and remaining budget |
| `/metrics` | GET | Prometheus counters (requests, errors, latency by group) |

```bash
curl http://localhost:4001/v1/scillm/health -H "Authorization: Bearer sk-dev-proxy-123"
curl http://localhost:4001/v1/scillm/models -H "Authorization: Bearer sk-dev-proxy-123"
curl http://localhost:4001/v1/budget -H "Authorization: Bearer sk-dev-proxy-123"
curl http://localhost:4001/metrics
```

---

## Composable Skills

scillm integrates with these skills via the proxy endpoint:

| Skill | Integration | Example |
|-------|-------------|---------|
| `/create-evidence-case` | LLM completions for claim generation | Evidence case authoring |
| `/analytics` | LLM completions for data analysis | Statistical analysis |
| `/create-figure` | LLM completions for chart descriptions | Publication figures |
| `/task-monitor` | Health endpoint monitoring | Progress tracking |
| `/lean4-prove` | Separate skill with own bridge | Formal theorem proving (not via scillm proxy) |

All composable skills call `http://localhost:4001/v1/chat/completions` — no direct
provider access, no SDK imports, no API keys needed beyond the proxy master key.
