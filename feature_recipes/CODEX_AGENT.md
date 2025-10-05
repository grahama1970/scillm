# codex‑agent (OpenAI‑compatible provider)

## Quick Start

```bash
export LITELLM_ENABLE_CODEX_AGENT=1
python scenarios/codex_agent_router.py
```

## Switching From Baseline

No special strategy modes. To “disable” extras, call with a standard codex‑agent model (e.g., `model="codex-agent/mini"`) and omit experimental params. Determinism follows the global seed precedence.

## Determinism

See Determinism & Reproducibility for seed precedence (per‑request > provider override > SCILLM_DETERMINISTIC_SEED > unset):
`docs/policies/DETERMINISM.md`.

