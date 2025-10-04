# Quick Start (SciLLM fork)

This fork adds an opt‑in mini‑agent, an env‑gated `codex-agent` provider, and bridges for CodeWorld and Certainly (Lean4). Everything below mirrors runnable assets in `../scenarios/` so you can reproduce a green run end‑to‑end.

## 1) Prereqs
- Python 3.10+
- Optional but recommended: Docker + Docker Compose
- Optional: provider keys (e.g., `OPENAI_API_KEY`, `GEMINI_API_KEY`)
- Put them in `.env` (every script loads it via `dotenv`).

## 2) Install
- Local editable install for iteration:
```bash
pip install -e .
```

## 3) Bring up the local stack (Beta)
Recommended bring‑up (CodeWorld + Lean4 + Redis + Ollama + Proxy):
```bash
docker compose -f ../deploy/docker/compose.scillm.stack.yml up --build -d
```
Notes:
- CodeWorld bridge runs on `:8887` with `CODEWORLD_SCORING_NONET=1` and `CODEWORLD_STRATEGY_NONET=1`.
- Lean4 (Certainly) bridge runs on `:8787`.
- For reproducible experiments, pass `options.session_id` and `options.track_id`.

Health & readiness:
- Non‑strict: `python ../scenarios/run_all.py` (skip‑friendly)
- Strict: `READINESS_LIVE=1 STRICT_READY=1 READINESS_EXPECT=codeworld,certainly make project-ready-live`

## 4) One‑command smoke run
```bash
make run-scenarios
```
Wraps `../scenarios/run_all.py`.

## 4.1) TL;DR module demos
```bash
# CodeWorld judge demo (shows speed effect)
python ../scenarios/codeworld_judge_live.py

# Certainly (Lean4) via Router alias
LITELLM_ENABLE_CERTAINLY=1 CERTAINLY_BRIDGE_BASE=http://127.0.0.1:8787 \
  python ../scenarios/certainly_router_release.py

# codex‑agent quick check (OpenAI-compatible)
export LITELLM_ENABLE_CODEX_AGENT=1
python ../scenarios/codex_agent_router.py
```

For more details, see the root <a href="../QUICK_START.md">QUICK_START.md</a>.

## 5) MCTS Demo (CodeWorld, experimental)

Run adaptive variant selection using CodeWorld's MCTS strategy policy (bridge must be running):

```bash
CODEWORLD_BASE=http://127.0.0.1:8887 python ../scenarios/mcts_codeworld_demo.py
```

Deterministic run:

```bash
export SCILLM_DETERMINISTIC_SEED=7
python ../scenarios/mcts_codeworld_demo.py
```

Inspect `additional_kwargs["codeworld"]["results"][0]["mcts"]` for visit distribution and the selected `best_variant`.
