# SCILLM Chat UI – Plan (MVP)

## Goals
- One web UI to exercise SCILLM surfaces (Chutes/openai_like, codex-agent, local Ollama) and bridges (CodeWorld, Lean4/“Certainly”).
- Show structured meta: budget headers, retries, invalid/repaired JSON, error types.
- Make debugging first-class (request/response/headers/meta inspector).
- Ship as an optional service in `deploy/docker/compose.scillm.stack.yml`.

## Scope (MVP)
- Chat pane with conversation list (local-only memory).
- Model selector: Chutes models (from `/models`), codex-agent, local Ollama (manual entry).
- Toggles: `strict_json`, `repair_invalid_json`, `response_format=json_object`, temperature, max_tokens, tool choice (off/auto).
- Meta panel per message: headers (budget, rate), scillm_meta (repaired, retries, error_type), timing.
- Health widget: ping redis, arangodb, ollama, codex-agent, chutes `/models`.
- Modes: CodeWorld “strategy” (POST to codeworld bridge), Lean4 “prove” (call certainly bridge) — exposed as secondary actions, not in initial chat stream.

## Non-goals (MVP)
- Auth/users/roles.
- Persistence of conversations beyond local storage.
- Complex workflow builders or notebooks.

## UX baseline to emulate
- Overall layout similar to OpenAI/Gemini chat: left settings drawer + main chat + right inspector.
- Keep messages compact; inline chips for model, latency, tokens, cost/budget.
- Toasts for errors; expandable raw JSON for each response.

## Stack
- React + Vite + TypeScript.
- Tailwind + shadcn/ui for components.
- State: React Query for calls; local storage for session list.
- Testing: Playwright smoke; Vitest for utils.
- Build to static assets served by a tiny FastAPI/Node proxy or nginx inside the compose service.

## API usage
- Primary: SCILLM proxy (openai_like) `POST /v1/chat/completions`.
- Codex-agent: base from env (no `/v1`), model list from `/v1/models`.
- Ollama: optional; allow manual base/model input.
- CodeWorld: `POST /bridge/complete` (expose strategy presets).
- Lean4: `POST /bridge/units/normalize` or existing certainly bridge endpoint.
- Health: `/models` (Chutes), `/healthz` for bridges, redis PING via a tiny backend helper.

## Env/config
- `SCILLM_UI_API_BASE` (default to proxy 4000).
- `SCILLM_UI_CODEWORLD_BASE`, `SCILLM_UI_LEAN4_BASE`, `SCILLM_UI_OLLAMA_BASE`.
- `SCILLM_UI_ENABLE_*` flags to toggle features.

## Compose integration
- Add `scillm-ui` service to `deploy/docker/compose.scillm.stack.yml`:
  - build from `ui/scillm-chat/Dockerfile` (Vite static -> nginx).
  - depends_on: scillm-proxy, redis, codeworld-bridge, lean4-bridge, ollama.
  - optional host port (e.g., 4300); internal network only by default.

## Logging & debugging
- Show raw request/response + headers + scillm_meta per message.
- Display budget headers (`x-ratelimit-*`, `x-budget-*`).
- Surface `error_type` (invalid_json, repaired, provider_error, timeout).
- Health widget for fast triage (redis/arangodb/ollama/codex-agent/chutes).

## Tasks (MVP)
1) Scaffold UI (`ui/scillm-chat/`): Vite/React/TS, Tailwind, shadcn.
2) Build layout: left settings drawer, chat panel, right inspector.
3) Implement chat call wrapper with meta capture; supports openai_like/codex/ollama.
4) Add toggles (strict_json, repair_invalid_json, response_format json_object, temperature, max_tokens).
5) Meta panel + raw viewer; budget header display.
6) Health widget hitting `/models` and bridge health endpoints.
7) Compose service for `scillm-ui`; docs snippet in README/QUICKSTART.
8) Tests: minimal Playwright smoke; Vitest for parsing/meta extraction.

## Open decisions
- Auth: skip for MVP; optional bearer passthrough later.
- Persistence: localStorage only for now.
- CodeWorld/Lean4 UX: start as secondary buttons that fire separate calls and render results inline, not part of chat history.

## References
- Current proxies and bridges already in `deploy/docker/compose.scillm.stack.yml`.
- Existing curl sanities: `debug/chutes/kimi_sanity_curl.sh`, `debug/chutes/parallel_acompletions_sanity.py` (mirror those endpoints in UI health checks).
