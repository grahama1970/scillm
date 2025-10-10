# Mini‑Agent (Deterministic MCP‑style)

## Quick Start

```bash
# Local, in‑process tools (deterministic):
python scenarios/mini_agent_http_release.py

# Docker tool backend (optional):
python scenarios/mini_agent_docker_release.py
```

## Switching From Baseline

Remove additional tool language or Docker‑related params to revert to the minimal in‑process agent. No other parameter changes required.

## Determinism

See Determinism & Reproducibility for seed precedence (per‑request > provider override > SCILLM_DETERMINISTIC_SEED > unset):
`docs/policies/DETERMINISM.md`.

