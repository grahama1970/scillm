# SciLLM Deployment Profiles

SciLLM is a scientific/engineering‑focused fork of LiteLLM. Core features work
out of the box; modules (Lean4, CodeWorld) are optional. Use these compose files
for common setups.

## Profiles

- Core proxy only (no modules)
  - File: `deploy/docker/compose.scillm.core.yml` (or `local/docker/compose.scillm.core.yml`)
  - Runs the LiteLLM proxy (`scillm-proxy`) on port 4000.
  - When you just need Router/proxy without bridges.

- Modules only (bridges for development)
  - File: `deploy/docker/compose.scillm.modules.yml` (or `local/docker/compose.scillm.modules.yml`)
  - Services: `codeworld-bridge` (8887), `lean4-bridge` (8787) — project package is named `certainly`; env aliases `CERTAINLY_*` are accepted.
  - Start bridges locally to exercise scenarios without running the proxy.
  - Lean4 bridge expects a Lean4 repo inside the container if you want real proofs.

- Full stack (proxy + bridges)
  - File: `deploy/docker/compose.scillm.full.yml` (or `local/docker/compose.scillm.full.yml`)
  - Services: `scillm-proxy`, `codeworld-bridge`, `lean4-bridge`
  - Sets `LITELLM_ENABLE_CODEWORLD=1` and `LITELLM_ENABLE_LEAN4=1` for the proxy.

## Commands

```bash
# Core proxy
docker compose -f deploy/docker/compose.scillm.core.yml up --build -d

# Bridges only
docker compose -f deploy/docker/compose.scillm.modules.yml up --build -d

# Full stack
docker compose -f deploy/docker/compose.scillm.full.yml up --build -d
```

## Env flags (preferred)
- `SCILLM_ENABLE_LEAN4=1` / `LITELLM_ENABLE_LEAN4=1`
- `SCILLM_ENABLE_CODEWORLD=1` / `LITELLM_ENABLE_CODEWORLD=1`
- `SCILLM_ENABLE_MINI_AGENT=1` / `LITELLM_ENABLE_MINI_AGENT=1`

## Next steps
- Add a Coq bridge/provider when you’re ready to integrate a real Coq CLI/daemon.
- Mount a host Lean4 repo into the `lean4-bridge` container if you want real proofs.
- Use scenarios in `scenarios/` to verify your stack; they are skip‑friendly when services are down.
