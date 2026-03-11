# scillm CONTEXT — CLAUDE.md

## Initial Steps
- cd into the project directory
- check for inter-agent messages: `agent-inbox check`

## Project Context
**Purpose:** Thin OpenAI-compatible proxy for scientists and engineers. Ground-up rewrite — zero litellm imports, all providers called via openai SDK.
**Type:** Core Infrastructure
**Status:** Active — Docker proxy running on port 4001
**Note:** Project registered as "scillm" in agent-inbox
**Docker:** `docker compose -p scillm -f deploy/docker/compose.scillm.core.yml up -d --build` (network_mode: host, port 4001)
**Port 4000 unavailable:** NoMachine NX server (system daemon, UID 135) — all services use port 4001

## Architecture
- `src/scillm/proxy/` — FastAPI app, Router, Middleware, Streaming, Errors, Config (~1,400 lines)
- `src/scillm/batch.py` + `src/scillm/batch_wrappers.py` — parallel completions via openai.AsyncOpenAI (~900 lines)
- `src/scillm/paved/chat.py` — convenience chat wrappers (~300 lines)
- `chutes/middleware/` — JSON guard, concurrency guard, VLM router, cache init, budget guard
- `deploy/docker/compose.scillm.core.yml` — production compose
- `local/proxy_server_config.yaml` — proxy config (env var resolution via os.environ/VAR_NAME)

## Smoke Tests
- `make smokes-cli-fast` — proxy-only tests (quality gate target)
- `make smokes` — full suite including Ollama preflight
- smoke-demo-pricing `ok:false` for local-text is expected (zero cost model)

## Known Issues
- Ollama runner hangs periodically — needs `sudo systemctl restart ollama` or kill root-owned PIDs
- smoke_ollama.py has unload-then-test pattern to mitigate hung runners
