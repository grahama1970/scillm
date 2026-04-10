# scillm CONTEXT — CLAUDE.md

## Initial Steps
- cd into the project directory
- check for inter-agent messages: `agent-inbox check`

## Project Context
**Purpose:** Thin OpenAI-compatible proxy for scientists and engineers. All providers called via openai SDK + custom OAuth providers (Claude, Codex).
**Type:** Core Infrastructure
**Status:** Active — Docker proxy running on port 4001
**Note:** Project registered as "scillm" in agent-inbox
**Docker:** `docker compose -p scillm -f deploy/docker/compose.scillm.core.yml up -d --build` (network_mode: host, port 4001)
**Port 4000 unavailable:** NoMachine NX server (system daemon, UID 135) — all services use port 4001

## Architecture (Bifrost P1+P2)
**Request flow:** Client → scillm :4001 → Bifrost :4002 → provider (or → utls-proxy :8444 for Codex)

**Components:**
- **scillm** (Python, :4001): `src/scillm/proxy/` — FastAPI, validation, JSON guard, VLM auto-routing, OAuth injection
- **Bifrost** (Go, :4002): High-perf routing gateway, built from [fork](https://github.com/grahama1970/bifrost)
- **utls-proxy** (Go, :8444): Chrome TLS fingerprint for Cloudflare bypass on chatgpt.com

**Key files:**
- `deploy/docker/Dockerfile.scillm` — multi-stage: builds Bifrost Go + Python scillm
- `deploy/docker/supervisord.conf` — runs both processes in one container
- `deploy/docker/generate_bifrost_config.py` — generates bifrost.json from proxy config at startup
- `deploy/utls-proxy/` — TLS fingerprint sidecar for Codex
- `local/proxy_server_config.yaml` — single source of truth (env var resolution via os.environ/VAR_NAME)
- `chutes/middleware/` — JSON guard, concurrency guard, VLM router, cache init, budget guard
- `src/scillm/batch.py` — parallel completions via openai.AsyncOpenAI

## Smoke Tests
- `make smokes-cli-fast` — proxy-only tests (quality gate target)
- `make smokes` — full suite including Ollama preflight
- smoke-demo-pricing `ok:false` for local-text is expected (zero cost model)

## Known Issues
- Ollama runner hangs periodically — needs `sudo systemctl restart ollama` or kill root-owned PIDs
- smoke_ollama.py has unload-then-test pattern to mitigate hung runners
