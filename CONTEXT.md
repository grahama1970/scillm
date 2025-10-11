# Session Context — SciLLM (feat/final-polish)

This document captures the exact state of our work so we can resume smoothly.

## Repo / Branch
- Fork: `grahama1970/scillm`
- Branch: `feat/final-polish`
- Path: `git@github.com:grahama1970/scillm.git#feat/final-polish`

## High‑value commits (latest first)
- 17b6a3f468 — make: bridge ops targets + scenarios; CI bridges workflow
- 13a0edff24 — ops: Docker healthchecks + restart; watchdog script
- 66e62caab9 — docker(stack) fixes; Lean4 echo fallback; verified scenarios
- b128131ac9 — scenarios: codex_agent_regression_check
- 050f2857d0 — retries meta; debug/retry_meta_probe
- e7b2346556 — parallel exceptions → scillm_router; codex-agent fast‑path
- 119881e2d3 — codex sidecar auth mount; echo off
- 6eb9f9c74b — docs: exact env + probes

## Components — Current Status
- Mini‑Agent (OpenAI‑compat shim)
  - Docker: `litellm-mini-agent` on `127.0.0.1:8788`
  - Health: `debug/verify_mini_agent.py` (OK earlier)
- Codex‑Agent Sidecar
  - Docker: `litellm-codex-agent` on `127.0.0.1:8077`
  - Auth: host `${HOME}/.codex/auth.json` mounted to `/root/.codex/auth.json`
  - Base rule: `CODEX_AGENT_API_BASE` must NOT include `/v1`
  - Regression script: `scenarios/codex_agent_regression_check.py`
- CodeWorld Bridge
  - Docker service: `codeworld-bridge` (host `:8887`)
  - Health: `curl -sSf http://127.0.0.1:8887/healthz`
  - Scenario: `PYTHONPATH=$(pwd) python scenarios/codeworld_bridge_release.py`
- Lean4 (Certainly) Bridge
  - Docker service: `lean4-bridge` (host `:8787`)
  - Env set: `LEAN4_REPO=/app`, `CERTAINLY_REPO=/app`
  - Echo fallback: set `LEAN4_BRIDGE_ECHO=1` to avoid 500s when CLI missing
  - Health: `curl -sSf http://127.0.0.1:8787/healthz`
  - Scenario: `PYTHONPATH=$(pwd) LEAN4_BRIDGE_ECHO=1 python scenarios/lean4_bridge_release.py`

## Router / Parallel invariants (now enforced)
- `parallel_acompletions` always returns an OpenAI‑shaped dict and attaches `scillm_router` (even on exceptions).
- Schema‑first preserves content and classifies non‑JSON as `invalid_json` (json_valid=false).
- Fast‑path provider: `codex-agent` avoids unintended OpenAI client init.
- Optional: `SCILLM_RETRY_META=1` surfaces `scillm_router.retries` (codex-agent path).

## Key Scripts
- Debug (root `debug/`):
  - `verify_mini_agent.py`, `verify_codex_agent_docker.py`, `check_codex_auth.py`
  - `codex_parallel_probe.py`, `retry_meta_probe.py`
- Scenarios (root `scenarios/`):
  - `codex_agent_regression_check.py` (regression guard)
  - `codeworld_bridge_release.py`, `lean4_bridge_release.py`
- Ops:
  - `scripts/watch_bridges.py` (one‑shot or loop restart on unhealthy)

## Make Targets
- `make bridge-up` / `bridge-down` / `bridge-restart`
- `make bridge-watch` (watchdog loop @ 30s)
- `make codeworld-live` / `lean4-live`
- `make codex-regression`

## CI
- `.github/workflows/bridges.yml` — starts bridges, runs health checks, one‑shot watchdog, and scenarios (Lean4 in echo mode).

## Environment — Quick Exports
- Codex‑Agent (local sidecar):
  - `export LITELLM_ENABLE_CODEX_AGENT=1`
  - `export CODEX_AGENT_API_BASE=http://127.0.0.1:8077`   # no `/v1` (use actual printed port; e.g., 8089)
  - `# export CODEX_AGENT_API_KEY=…` (if gateway enforces)
  - Optional (retries): `export SCILLM_RETRY_META=1 CODEX_AGENT_ENABLE_METRICS=1`

### Extractor Pipeline — Zero‑Ambiguity Mapping

Use codex‑agent as an OpenAI‑compatible base with no `/v1` in the URL.

```bash
# Map envs expected by OpenAI clients (HTTP extractor)
export OPENAI_BASE_URL="$CODEX_AGENT_API_BASE"    # do NOT append /v1
export OPENAI_API_KEY="${CODEX_AGENT_API_KEY:-none}"

# Discover a model id and make a high‑reasoning call
curl -sS "$OPENAI_BASE_URL/healthz" || true
curl -sS "$OPENAI_BASE_URL/v1/models" | jq -r '.data[].id'
curl -sS "$OPENAI_BASE_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5","reasoning":{"effort":"high"},"messages":[{"role":"user","content":"ping"}]}' \
  | jq -r '.choices[0].message.content'
```

Troubleshooting
- 404 on chat → wrong `model`; use one from `/v1/models`.
- Connection refused → check actual sidecar port with `docker ps` (common: 8089).
- Base contains `/v1` → remove it.
- Bridges:
  - CodeWorld up: `docker compose -f deploy/docker/compose.scillm.stack.yml up -d codeworld-bridge`
  - Lean4 up: `docker compose -f deploy/docker/compose.scillm.stack.yml up -d lean4-bridge`
  - Lean4 echo scenario: `LEAN4_BRIDGE_ECHO=1`

## What to run first (tomorrow)
1) Start/verify bridges:
   - `make bridge-up`
   - `make bridge-watch` (optional continuous guard)
   - `make codeworld-live` and `make lean4-live`
2) Codex regression:
   - `make codex-regression`
3) Optional probes:
   - `python debug/codex_parallel_probe.py`
   - `SCILLM_RETRY_META=1 CODEX_AGENT_ENABLE_METRICS=1 python debug/retry_meta_probe.py`

## Open TODOs / Next Steps
- Mini‑agent scenario uses an OLLAMA model tag; confirm model presence in local OLLAMA or swap to a known tag.
- Lean4 real CLI path: mount a proper Lean4 CLI repo into the container and unset `LEAN4_BRIDGE_ECHO` to test real proofs.
- Optional CI: add a codex regression job; upload artifacts (content + scillm_router) for traceability.

## Known Pitfalls (captured in runbook)
- Set provider env before import; omit `/v1` in bases; vision messages must be a list of parts; mount `~/.codex/auth.json` for codex sidecar when echo is off.
- See memory: `codex-agent-runbook` for full lessons learned.
