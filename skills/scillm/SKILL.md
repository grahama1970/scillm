---
name: scillm
description: >
  Internal Tau-owned LLM proxy on localhost:4001. Maintainer surfaces:
  one-shot chat, exec workers, and subagent delegates (OpenCode serve,
  standing agents). Chutes, Gemini, Claude/Codex OAuth, OpenCode Go, Ollama.
  ZIP/PDF, JSON repair.
allowed-tools: Bash, Read
triggers:
  - batch LLM calls
  - parallel completions
  - describe image
  - generate image
  - image generation
  - gpt-image-2
  - multimodal
  - LLM completion
  - call claude
  - call codex
  - call kimi
  - kimi exec
  - scillm exec
  - opencode serve
  - opencode transport
  - standing agent
  - subagent
  - coding delegate
metadata:
  short-description: scillm (one-shot, exec, subagent — localhost:4001)
provides:
  - llm-completion
composes:
  - task-monitor
  - memory
  - dogpile
  - debugger
taxonomy:
  - inference
  - llm
---

# scillm — One Endpoint for All LLM Calls

> Project-agent boundary: SciLLM is now a Tau-owned provider sidecar. If a
> project agent tries to call `$scillm`, `/scillm`, `http://localhost:4001`,
> `/v1/chat/completions`, or `/v1/scillm/*` directly during ordinary project
> work, stop and remind it to route provider/model work through a
> `tau.dag_contract.v1` node, Tau skill node, or Tau-owned `command_spec`.
> Direct SciLLM use is only for explicit SciLLM/Tau maintenance or Tau-authored
> provider adapters that write receipts.

**Repo:** [README.md](../../README.md) · **Deep refs:** [references/](references/) · **Contracts:** [docs/SCILLM_EXEC.md](../../docs/SCILLM_EXEC.md), [docs/SCILLM_OPENCODE_SERVE.md](../../docs/SCILLM_OPENCODE_SERVE.md)

## Direct Maintainer Surfaces

Direct calls are for Tau provider adapters and SciLLM maintainers only. Every
direct scillm call uses the same host and headers:

| Requirement | Value |
|-------------|-------|
| Base URL | `http://localhost:4001` |
| Auth | `Authorization: Bearer sk-dev-proxy-123` (or your proxy key) |
| Attribution | **`X-Caller-Skill: <your-project>`** — **required** on chat; use on all surfaces |

**Pick exactly one call type:**

```text
What do you need?
│
├─ 1. ONE-SHOT     → answer from a model (text, JSON, VLM describe)
│                    POST /v1/chat/completions
│
├─ 1b. IMAGE       → create PNG from prompt file (NOT chat)
│                    run.sh generate-image  OR  POST /v1/images/generations
│                    Terminal: scillm.image.completed (stderr) or body.scillm.terminal
│
├─ 2. EXEC         → one bounded headless worker shot (DAG gate, no merge loop)
│                    POST /v1/scillm/exec
│
└─ 3. SUBAGENT     → delegate repo work to a worker with tools
       ├─ bounded session (read/grep/patch once)  → POST /v1/scillm/opencode/runs
       └─ multi-turn standing worker (lease/turn)  → /v1/scillm/agents/*
```

| Call type | Endpoint | Returns | Use when |
|-----------|----------|---------|----------|
| **One-shot** | `POST /v1/chat/completions` | One completion JSON | Question, extraction, classify, describe image |
| **Image create** | `run.sh generate-image` or `POST /v1/images/generations` | PNG + receipt; `scillm.terminal` | Generate icons, mockups, assets — never use chat for this |
| **Exec** | `POST /v1/scillm/exec` | Worker receipt / stdout JSON | Pipeline gate, one CLI shot, graph node |
| **Subagent (serve)** | `POST /v1/scillm/opencode/runs` | `assistant_text`, events, optional diff | Agent investigates/edits repo with skills/tools |
| **Subagent (standing)** | `/v1/scillm/agents/*` | Turn result across leases | Same Codex worker across multiple turns |

**Do not mix these up:**
- Chat `model` names (`gpt-5.5`, `moonshot-text`, `opencode-go/*`) → **one-shot only**
- Exec `profile` names (`codex-gpt-5.5`, `kimi-k2.6`, `pi-chutes-kimi`) → **exec only**
- OpenCode `agent` profiles (`build`, `scillm-debugger`) → **subagent serve only**
- Never call Moonshot/Chutes/Gemini APIs directly — always `localhost:4001`

## Reliable Chutes Calls Via `scillm-agent`

Use `scillm-agent` as the protected control surface when a Tau provider adapter
or SciLLM maintainer needs reliable Chutes single calls, batch calls, model
choice, cold-model handling, dynamic concurrency, prompt preflight, or repair
receipts. Chutes chat aliases are disabled; pass an exact live `Org/Model` ID
selected with `ops-chutes`.

```bash
curl -sS http://localhost:4001/v1/scillm/chutes/completions \
  -H "Authorization: Bearer sk-dev-proxy-123" \
  -H "X-Caller-Skill: scillm-agent" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3.6-27B-TEE",
    "messages": [{"role": "user", "content": "Tell me a story."}],
    "stream": false
  }'
```

Before reliability-sensitive calls, check `ops-chutes model-health <model>` and
`ops-chutes recommend <model> --json`. Do not hard-code Chutes fallback chains:
inventory changes frequently, especially for batches. If no live comparable
model exists, fail before launching a large batch with a structured error.

Do not use `chutes-deepseek`, `chutes-qwen`, `chutes-kimi`, `vlm-chutes`, or
`gpt-chutes` for Chutes chat calls.

Golden proof:

```bash
bash scripts/prove_chutes_golden_curl.sh \
  --model 'Qwen/Qwen3.6-27B-TEE' \
  --batch-size 2 \
  --concurrency 2 \
  --wall-time-s 180 \
  --prompt 'Tell me a 100 word story.'
```

| Endpoint | Use |
|----------|-----|
| `GET /v1/scillm/chutes/models` | List available Chutes models |
| `POST /v1/scillm/chutes/completions` | Single completion (stream or non-stream) |
| `POST /v1/scillm/chutes/batch` | Batch with semaphore + exponential backoff + `asyncio.as_completed` (SSE stream) |

**`POST /v1/scillm/chutes/batch`** yields SSE events as items complete (not in input order):

```bash
curl -s -N --no-buffer http://localhost:4001/v1/scillm/chutes/batch \
  -H "Authorization: Bearer sk-dev-proxy-123" \
  -H "X-Caller-Skill: scillm-agent" \
  -H "Content-Type: application/json" \
  -d '{
    "requests": [
      {"item_id":"item-0","model":"Qwen/Qwen3.6-27B-TEE","messages":[{"role":"user","content":"Say OK"}]},
      {"item_id":"item-1","model":"Qwen/Qwen3.6-27B-TEE","messages":[{"role":"user","content":"Say hi"}]}
    ],
    "concurrency": 4
  }'
```

Each event: `{"index": 0, "ok": true, "content": "...", "model_served": "...", "attempts": 1, "elapsed_s": 1.23}`

From Python (asyncio):

```python
import asyncio
import httpx
import json

async def chutes_batch(requests: list[dict], concurrency: int = 4):
    url = "http://localhost:4001/v1/scillm/chutes/batch"
    headers = {"Authorization": "Bearer sk-dev-proxy-123", "Content-Type": "application/json"}
    body = {"requests": requests, "concurrency": concurrency}
    async with httpx.AsyncClient(timeout=600.0) as client:
        async with client.stream("POST", url, json=body, headers=headers) as resp:
            async for line in resp.aiter_lines():
                line = line.strip()
                if line.startswith("data: "):
                    yield json.loads(line[6:])

async def main():
    items = [
        {"item_id": "item-0", "model": "Qwen/Qwen3.6-27B-TEE", "messages": [{"role": "user", "content": "Say OK"}]},
        {"item_id": "item-1", "model": "Qwen/Qwen3.6-27B-TEE", "messages": [{"role": "user", "content": "Say hi"}]},
    ]
    async for result in chutes_batch(items):
        status = "OK" if result["ok"] else f"FAIL ({result.get('error')})"
        print(f"[{result['index']}] {status} — {result.get('content', '')[:60]}")

asyncio.run(main())
```

Results arrive in **completion order** (fastest first). Use `result["index"]` to map back to the original request.

**Architecture:**
- Semaphore held only during the HTTP call (not during backoff sleep), so retrying one item doesn't block concurrent items
- httpx.AsyncClient directly to `POST https://llm.chutes.ai/v1/chat/completions` (no OpenAI SDK)
- Exponential backoff on 5xx/429/timeout; wall-time budget kills the retry loop
- `X-Caller-Skill` is recommended for attribution; use `scillm-agent` for Chutes reliability lanes

---

### 1. One-shot (chat completions)

**When:** You want one model response. No repo tool loop. No worker receipt.

```bash
curl -s http://localhost:4001/v1/chat/completions \
  -H "Authorization: Bearer sk-dev-proxy-123" \
  -H "X-Caller-Skill: my-project" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.5","messages":[{"role":"user","content":"What is 2+2?"}]}'
```

**Common models:**

| Intent | Model |
|--------|-------|
| General text | `gpt-5.5`, `claude-sonnet-4-6`, `gemini-flash`; for Chutes, exact live `Org/Model` from `ops-chutes` |
| Image / PDF (pick provider) | `gpt-5.5`, `claude-sonnet-4-6`; for Chutes VLM, exact live `Org/Model` from `ops-chutes` |
| Kimi text (Moonshot API) | `moonshot-text` |
| Kimi + image (Moonshot API) | `moonshot-text` + OpenAI `image_url` parts |
| Kimi via OpenCode Go | `oc-kimi` or `opencode-go/kimi-k2.6` |

**Kimi rules (one-shot):**
- `moonshot-text` → native Moonshot `kimi-k2.6`; supports PNG/JPEG via `image_url`
- `kimi_exec` is **not** one-shot — it is **exec** (text only, no images)
- Do **not** use generic `vlm` when you want Kimi — that is Gemini → Claude → Codex
- Avoid legacy alias `text` when provider choice matters

**Image example (Moonshot Kimi):**

```bash
curl -s http://localhost:4001/v1/chat/completions \
  -H "Authorization: Bearer sk-dev-proxy-123" \
  -H "X-Caller-Skill: my-project" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "moonshot-text",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "What color is this image? One word."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,<BASE64>"}}
      ]
    }]
  }'
```

**Slash shortcut:** `/scillm "…"` · `/scillm --model moonshot-text "Describe @screenshot.png"`


### 1b. Image generation (create images) — **maintainer path**

**`/scillm` slash wrapper — image vs chat (critical):**

| Task | Command | Termination signal |
|------|---------|-------------------|
| Text / VLM describe | `run.sh "…"` or `run.sh --model gpt-5.5 "…"` | Chat completion text on stdout |
| **Generate image (GPT)** | `run.sh generate-image --prompt-file … --out … --auth openai-api-key` | stderr NDJSON `scillm.image.completed` + exit 0 + receipt JSON |
| Generate image (OAuth) | `run.sh generate-image --prompt-file … --out …` (default `--auth codex-oauth`) | stderr `scillm.image.completed` after codex JSONL |

**Never** use plain `run.sh "generate a PNG…"` for image creation — that only calls `/v1/chat/completions` and finishes with **text**, not a PNG.

Progress events (stderr, one JSON object per line):

```jsonl
{"type":"scillm.image.started","auth":"openai-api-key","model":"gpt-image-2","prompt_chars":582}
{"type":"scillm.image.completed","auth":"openai-api-key","ok":true,"terminal":true,"elapsed_ms":14200,"path":"artifacts/images/x.png","receipt_path":"artifacts/images/x_receipt.json","sha256":"…","width":1024,"height":1024}
```

HTTP `POST /v1/images/generations` also returns a terminal envelope:

```json
"scillm": {"status": "completed", "terminal": true, "elapsed_ms": 14200, "caller_skill": "…", "has_b64": true}
```

CLI: `scillm image generate --prompt-file … --out … --auth openai-api-key`



**When:** Generate a **new** image from a large/detailed spec (mockups, icons, scenes, UI assets).

## Auth model (read this first)

| Auth | How | Use when |
|------|-----|----------|
| **`codex-oauth` (DEFAULT)** | `codex login` (ChatGPT subscription) + built-in **`image_gen`** via `codex exec` | Tau/SciLLM maintenance — no `OPENAI_API_KEY` |
| **`openai-api-key` (opt-in)** | `POST /v1/images/generations` on scillm with `OPENAI_API_KEY` on proxy host | CI, headless servers without Codex, explicit API billing |

**Critical:** Codex OAuth tokens **cannot** call `https://api.openai.com/v1/images/generations` (401: missing `api.model.images.request` scope). Do not point OAuth at the HTTP images endpoint.

**Do not use** bare `$imagegen` / agent-only flows without the wrapper — they save under `~/.codex/generated_images/` and skip receipts.

## Canonical workflow (DEFAULT: Codex OAuth)

1. **Ensure auth:** `codex login status` → logged in via ChatGPT.

2. **Write prompt file** in repo (structured sections; up to **32,000 chars**):

   Template: `examples/image-prompts/sample-icon.prompt.md` (`SUBJECT`, `COMPOSITION`, `STYLE`, `MUST INCLUDE`, `MUST NOT INCLUDE`, `OUTPUT`).

3. **Run receipt wrapper** (monitors codex JSONL; copies PNG + writes receipt):

```bash
bash skills/scillm/run.sh generate-image \
  --auth openai-api-key \
  --prompt-file path/to/your.prompt.md \
  --out artifacts/images/your-asset.png \
  --model gpt-image-2 \
  --quality high

# equivalent:
python scripts/generate_image.py --auth openai-api-key ...
```

`--auth codex-oauth` is the default; `--caller-skill` is not required on this path.

4. **Pass/fail gate:**

   - exit code `0`
   - `--out` PNG exists (`bytes > 0`)
   - receipt JSON (`<stem>_receipt.json` or `--receipt`) has `ok: true`, `auth: "codex-oauth"`, `sha256`, `width`, `height`
   - codex JSONL shows `thread.started` within ~30s (script fails fast if silent)

## Opt-in workflow: OpenAI API key (HTTP)

Use only when Codex OAuth is unavailable or you explicitly want direct API billing.

```bash
python scripts/generate_image.py \
  --auth openai-api-key \
  --prompt-file path/to/your.prompt.md \
  --out artifacts/images/your-asset.png \
  --caller-skill your-project \
  --model gpt-image-2 \
  --quality high \
  --size 1536x1024
```

Requires `OPENAI_API_KEY` on the **scillm proxy host**. Requires `X-Caller-Skill` (via `--caller-skill`).

Direct HTTP equivalent:

```bash
curl -s http://localhost:4001/v1/images/generations \
  -H "Authorization: Bearer sk-dev-proxy-123" \
  -H "X-Caller-Skill: my-project" \
  -H "Content-Type: application/json" \
  -d @request.json
```

`request.json` → `model: gpt-image-2`, `quality: high`, `prompt` (max 32k chars), `response_format: b64_json`.

| Intent | Model | Auth on proxy |
|--------|-------|----------------|
| Best quality / detailed spec | `gpt-image-2` | `OPENAI_API_KEY` |
| Transparent PNG | `gpt-image-1.5` + `background=transparent` | `OPENAI_API_KEY` |
| Cheap draft | `gpt-image-1-mini` | `OPENAI_API_KEY` |
| Fast/free | `z-image-turbo` | `CHUTES_API_KEY` |

Batch (API-key path): `POST /v1/scillm/batch/images/generations` with `items[]`.

Discover: `GET /v1/scillm/capabilities` → `image_generation`.

## Not image generation

| Surface | What it does |
|---------|----------------|
| `gpt-5.5` on `/v1/chat/completions` | Chat + vision **in** (describe existing images) |
| Codex `$imagegen` without wrapper | OAuth yes, but **no** guaranteed workspace path/receipt |
| `codex exec` without `generate_image.py` | You must monitor JSONL + copy files yourself |

Prompt > 32k chars: compress to a brief (one-shot chat) before image generation.

More: [references/chat-calls.md](references/chat-calls.md) · [references/files-multimodal.md](references/files-multimodal.md)

---

### 2. Exec (bounded worker shot)

**When:** A pipeline or DAG needs **one** headless worker attempt — classify, gate, diagnose — without you merging a multi-step agent session.

**Not for:** Open-ended product implementation loops (use subagent serve). Not for ordinary Q&A (use one-shot).

```bash
curl -s -X POST http://localhost:4001/v1/scillm/exec \
  -H "Authorization: Bearer sk-dev-proxy-123" \
  -H "X-Caller-Skill: my-project" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "kimi_exec",
    "model": "kimi-k2.6",
    "prompt": "What is 2+2? Reply with the digit 4 only.",
    "metadata": {"sandbox": "read-only"}
  }'
```

**Common exec profiles:**

| Profile | Worker | Notes |
|---------|--------|-------|
| `codex-gpt-5.5` | Codex CLI | Bounded `codex exec` — not chat `gpt-5.5` |
| `kimi-k2.6` / `kimi` | Kimi CLI (`kimi -p`) | **Text only** — no image API |
| `pi-chutes-kimi` | Pi over Chutes Kimi | Low-overhead exec lane |
| `oc-chutes-deepseek` | OpenCode CLI one-shot | Skills denied in generated config |

**Rules:**
- Pass **profile names** (`kimi-k2.6`), not chat aliases (`oc-kimi`, `moonshot-text`, `opencode-go/*`)
- `kimi_exec` needs Kimi CLI auth in the proxy container (`~/.kimi`, `KIMI_API_KEY`)
- Exec returns evidence for the graph — **you** validate and decide next step

More: [references/exec-workers.md](references/exec-workers.md) · [docs/SCILLM_EXEC.md](../../docs/SCILLM_EXEC.md)

---

### 3. Subagent (delegated workers)

**When:** You delegate repo work to a worker that can **read, grep, use skills, and optionally patch**. You own validation and merge.

#### 3a. OpenCode serve — bounded session (most common)

**When:** One investigation or patch attempt with tools. Single session, then done.

```bash
curl -s -X POST http://localhost:4001/v1/scillm/opencode/runs \
  -H "Authorization: Bearer sk-dev-proxy-123" \
  -H "X-Caller-Skill: my-project" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Read tests/test_foo.py and explain the failure. Do not edit.",
    "agent": "build",
    "skills": ["memory", "debugger", "scillm"],
    "timeout_s": 600
  }'
```

| Field | Rule |
|-------|------|
| `agent` | OpenCode **profile** (`build`, `scillm-debugger`) — from `GET /v1/scillm/opencode/agents` |
| `skills` | Optional allowlist (`memory`, `dogpile`, `debugger`, `scillm`, …) |
| `prompt` | Your task — paste `/memory` recall and `/dogpile` synthesis here first |

**Wrong:** `"agent": "opencode-go/kimi-k2.6"` — that is a **chat model**, not an agent profile.

**Wrong:** `POST /v1/chat/completions` in a loop to "fix the bug" — no tool loop, no artifacts.

Enable: `SCILLM_OPENCODE_SERVE_ENABLED=1`. Verify: `bash scripts/sanity_opencode_serve.sh`

More: [references/opencode-serve.md](references/opencode-serve.md)

#### 3b. Standing agents — multi-turn Codex worker

**When:** Same worker across multiple turns with lease/handoff (reviewer, implementation worker).

```text
GET  /v1/scillm/agents/registry
POST /v1/scillm/agents/{worker_id}/handoffs
POST /v1/scillm/agents/{worker_id}/leases
POST /v1/scillm/agents/{worker_id}/turn
GET  /v1/scillm/agents/{worker_id}/result?handoff_id=...
POST /v1/scillm/agents/{worker_id}/cleanup
```

Prerequisite: worker registered in `config/scillm-agents.yaml`. Proof: `bash scripts/sanity_agents_endpoints.sh`

More: [references/standing-agents.md](references/standing-agents.md)

#### 3c. Transport / harness (advanced)

**When:** DAG parent/child runs, SSE steering, execution harness loops — not everyday project-agent calls.

More: [references/opencode-transport.md](references/opencode-transport.md) · [docs/SCILLM_OPENCODE_TRANSPORT_V1.md](../../docs/SCILLM_OPENCODE_TRANSPORT_V1.md)

---

## Quick comparison (copy this)

| You want… | Call type | Example |
|-----------|-----------|---------|
| "What is 2+2?" | One-shot | `POST /v1/chat/completions` `model: gpt-5.5`; for Chutes, exact live `Org/Model` |
| Chutes single call | Direct Chutes | `POST /v1/scillm/chutes/completions` `model: Qwen/Qwen3.6-27B-TEE` |
| Batch Chutes — semaphore + retry + as_completed | Direct Chutes batch | `POST /v1/scillm/chutes/batch` with exact live `Org/Model` IDs |
| "Describe this PNG" (Kimi) | One-shot | `model: moonshot-text` + `image_url` |
| "Generate a PNG/icon/mockup from detailed spec" | **Image (OAuth default)** | `python scripts/generate_image.py --prompt-file … --out …` (`codex login`) |
| DAG gate: classify this text | Exec | `POST /v1/scillm/exec` `type: kimi_exec` |
| "Fix the failing test" | Subagent serve | `POST /v1/scillm/opencode/runs` `agent: build` |
| Reviewer across 3 turns | Subagent standing | `/v1/scillm/agents/{worker}/turn` |

---

## Setup (one-time)

| Provider | Env / auth | One-shot model examples |
|----------|------------|-------------------------|
| Claude | `~/.claude/.credentials.json` | `claude-sonnet-4-6` |
| Codex | `codex login` | `gpt-5.5` |
| Gemini | `GEMINI_API_KEY` | `text-gemini`, `gemini-2.5-flash` |
| Moonshot Kimi | `MOONSHOT_API_KEY` | `moonshot-text` |
| Kimi CLI (exec only) | `KIMI_API_KEY` + `kimi-cli` | exec profile `kimi-k2.6` |
| OpenCode Go | `OPENCODE_GO_API_KEY` | `oc-kimi`, `opencode-go/deepseek-v4-pro` |
| Chutes | `CHUTES_API_KEY` | Exact live `Org/Model` selected with `ops-chutes` |
| OpenCode serve | `SCILLM_OPENCODE_SERVE_ENABLED=1` | subagent `agent: build` |
| Ollama | `ollama pull model:tag` | `qwen2.5:7b` |

Rebuild: `docker compose -p scillm -f deploy/docker/compose.scillm.core.yml up -d --build`

Check auth: `GET /v1/scillm/auth` · Discover models: `GET /v1/scillm/providers`

---

## Critical operating rules

- **Pick the call type first** — one-shot vs exec vs subagent. Wrong surface = wrong artifacts.
- **`X-Caller-Skill` is required** on chat completions (400 if missing).
- **Batch calls:** `httpx.AsyncClient` + `asyncio.create_task` + `asyncio.as_completed` — not default `gather`. Each item needs `scillm_metadata.batch_id` and `item_id`.
- **Subagent output is evidence** — validate receipts/diffs before merge. Executors do not own truth.
- **Orchestrator / harness** — only for multi-step delegated runs with DAG state. See [references/opencode-transport.md](references/opencode-transport.md). Not for rename-one-function work.

---

## Reference map

| Topic | File |
|-------|------|
| Chat, JSON, message shapes | [references/chat-calls.md](references/chat-calls.md) |
| Images, PDF, ZIP | [references/files-multimodal.md](references/files-multimodal.md) |
| Model routing tables | [references/models-and-routing.md](references/models-and-routing.md) |
| Exec profiles | [references/exec-workers.md](references/exec-workers.md) |
| OpenCode serve | [references/opencode-serve.md](references/opencode-serve.md) |
| Standing agents | [references/standing-agents.md](references/standing-agents.md) |
| Transport / harness | [references/opencode-transport.md](references/opencode-transport.md) |
| Batch / pools | [references/batch-calls.md](references/batch-calls.md) |
| Ops endpoints | [references/ops-endpoints.md](references/ops-endpoints.md) |

Specialized lanes (load only when needed): pdf-lab → [docs/SCILLM_PDF_LAB_OPENCODE_SERVE_HARDENING_PLAN.md](../../docs/SCILLM_PDF_LAB_OPENCODE_SERVE_HARDENING_PLAN.md)

---

## Ops (quick)

| Endpoint | Purpose |
|----------|---------|
| `GET /health/liveliness` | Proxy alive |
| `GET /v1/scillm/health` | Groups, fallbacks |
| `GET /v1/scillm/auth` | OAuth / key health |
| `POST /v1/scillm/batch/completions` | Server-side batch |
| `POST /v1/scillm/opencode/runs` | Subagent serve |
| `GET /v1/scillm/agents/registry` | Standing workers |
| `GET /v1/scillm/chutes/models` | List available Chutes models |
| `POST /v1/scillm/chutes/completions` | Direct Chutes (no middleware) |
| `POST /v1/scillm/chutes/batch` | Direct Chutes batch (SSE as_completed) |

Composable skills must not instruct project agents to call
**`http://localhost:4001`** directly. If a provider/model call is needed, route
it through Tau or keep it inside the skill's owned runtime with explicit proof
boundaries.
