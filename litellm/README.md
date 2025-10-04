<p align="center">
  <img src="../local/artifacts/logo/SciLLM_balanced_outlined.svg" alt="SciLLM" width="140" />
  <br/>
  <img src="../local/artifacts/logo/SciLLM_icon.svg" alt="SciLLM Icon" width="44" />
  <br/>
  <em>Balanced wordmark (default) + icon (logo‑only). The favicon (.ico) uses the icon only.</em>
</p>

<h1 align="center">🔬 SciLLM — a Scientific/Engineering fork of LiteLLM</h1>

<p align="center">
  <a href="https://github.com/grahama1970/scillm/actions/workflows/nightly-parity-stress.yml"><img src="https://github.com/grahama1970/scillm/actions/workflows/nightly-parity-stress.yml/badge.svg" alt="SciLLM: Nightly Parity & Stress"></a>
  <a href="https://github.com/grahama1970/scillm/actions/workflows/weekly-streaming-stress.yml"><img src="https://github.com/grahama1970/scillm/actions/workflows/weekly-streaming-stress.yml/badge.svg" alt="SciLLM: Weekly Streaming Stress"></a>
  <a href="https://github.com/grahama1970/scillm/actions/workflows/manual-stress.yml"><img src="https://img.shields.io/badge/SciLLM%20Manual%20Stress-%E2%86%92-blue" alt="SciLLM: Manual Stress"></a>
</p>

<p align="center"><i>API‑compatible with LiteLLM. Adds optional modules for: (1) formal methods via a prover bridge (“Certainly”, Lean4 in beta), (2) code strategy orchestration with dynamic scoring (“CodeWorld”), and (3) small live agent flows (mini‑agent, codex‑agent). See the root <a href="../QUICK_START.md">QUICK_START.md</a> and scenarios/ for runnable demos. Enable with SCILLM_ENABLE_* or LITELLM_ENABLE_*.</i></p>

## TL;DR (30 seconds)

```bash
# Bring up bridges + proxy + deps (from repo root)
docker compose -f deploy/docker/compose.scillm.stack.yml up --build -d

# Two live scenarios (skip‑friendly)
python ../scenarios/codeworld_judge_live.py
LITELLM_ENABLE_CERTAINLY=1 CERTAINLY_BRIDGE_BASE=http://127.0.0.1:8787 \
  python ../scenarios/certainly_router_release.py
```

---

## Why SciLLM

SciLLM targets practitioners who need reproducible, inspectable end‑to‑end workflows that combine LLMs with verifiable computation.

- Who it’s for
  - Scientists/engineers building proof‑of‑concepts around formal verification (Lean4 today), algorithm selection, and agent tool‑use loops.
  - Teams who want OpenAI‑compatible ergonomics, local‑first bring‑up (Docker), and “one way to green” readiness gates.
  - Educators/tinkerers who want runnable scenarios and artifacts they can inspect.

- What you get
  - Any LLM model LiteLLM supports (local or cloud) behind the same OpenAI‑style surface.
  - Certainly (Lean4 umbrella, beta): convert natural‑language + structured requirements to Lean4, return proofs or structured guidance/diagnostics.
  - CodeWorld: run multiple concurrent strategies safely; add dynamic scoring; judge/rank winners.
  - codex‑agent: code‑centric agent surface; run multi‑iteration plans and call MCP tools via your sidecar.
  - mini‑agent: tiny deterministic agent for quick tool‑use experiments.
  - Reproducibility by design: per‑run artifacts (run_id, request_id, item_ids, session/track) and strict readiness gates.

---

## Modules at a Glance

| Module | What it is | When to use | How it works |
| --- | --- | --- | --- |
| Certainly (Lean4 umbrella) | Prover bridge under a stable provider alias | Batch‑check obligations/lemmas; keep client code stable while swapping backends | Router posts `{messages, items, options}` to Lean4 bridge; returns `summary + results + run_manifest` with diagnostics |
| CodeWorld | Strategy orchestrator with dynamic judge | Compare multiple algorithms on the same inputs; evaluate under your metrics; rank winners | Execute strategies with RLIMITs; optional no‑net; dynamic Python `score()`; built‑in weighted/lex judge |
| mini‑agent | Tiny agent loop for tool‑use | Local, deterministic experiments with Python/Rust/Go/JS tools | In‑process shim or Docker tools sidecar; emits parsed tool calls and metrics |
| codex‑agent | Code‑oriented agent provider | Sidecar/HTTP codex flows exposed via LiteLLM | Env‑gated provider; OpenAI‑compatible; integrates via Router |

---

## Real‑World Scenarios

- Multi‑heuristic selection (CodeWorld): package DP/heuristics as variants; run both; supply a domain‑specific `score()`; judge with correctness/speed/brevity; keep winner + provenance.
- Spec compliance verification (Certainly): send `messages + lean4_requirements` (or canonical `items`); get proof results with stable item_ids and diagnostics; rerun with the same manifest.
- Inner‑loop bug fix (mini‑agent): run tool invocations deterministically; capture final answer + telemetry.
- Code refactor planning (codex‑agent): code‑centric agent via OpenAI‑compatible provider; health‑checkable sidecar.

See <a href="../QUICK_START.md">QUICK_START.md</a> for runnable commands.

---

## Installation and Compatibility

- pip install: `pip install litellm` (SciLLM remains API‑compatible with LiteLLM)
- Env flags (preferred): `SCILLM_ENABLE_*` (aliases supported: `LITELLM_ENABLE_*`)
- CLI aliases: `scillm`, `scillm-proxy` (mirrors LiteLLM commands)
- Deployment: `deploy/docker/compose.scillm.stack.yml` provides bridges + local tooling

---

## Quick Start

See the root quick start: <a href="../QUICK_START.md">QUICK_START.md</a>

---

## Strategy Search: MCTS (Experimental)

Add decision-time stochastic search for CodeWorld variants.

- Enable (bridge side): `CODEWORLD_ENABLE_MCTS=1` (default)
- Deterministic runs: set `SCILLM_DETERMINISTIC_SEED=42`
- Example:

```bash
CODEWORLD_BASE=http://127.0.0.1:8887 python ../scenarios/mcts_codeworld_demo.py
```

Response extras:
- `results[i].mcts`: `{best_variant, best_value, visits, explored, rollouts, depth, uct_c, seed}`
- `run_manifest.mcts_stats`: summary at the run level

Security posture: Phase‑1 MCTS uses a hash‑based pseudo value for rollouts (no extra code execution) to avoid expanding the attack surface. Future extensions may enable partial evaluation per rollout behind a separate flag.

---

<details>
  <summary>Logo variants</summary>
  <p>
    <img src="../local/artifacts/logo/SciLLM_balanced_outlined.svg" alt="SciLLM Balanced (default, outlined)" height="36" />
    &nbsp;&nbsp;
    <img src="../local/artifacts/logo/SciLLM_icon.svg" alt="SciLLM Icon" height="36" />
    &nbsp;&nbsp;
    <img src="../local/artifacts/logo/SciLLM_balanced_dark.svg" alt="SciLLM Balanced Dark" height="36" />
    &nbsp;&nbsp;
    <img src="../local/artifacts/logo/SciLLM_balanced_mono.svg" alt="SciLLM Balanced Mono" height="36" />
  </p>
  <p>Use <code>make logo-export</code> to produce outlined SVGs and favicons in <code>local/artifacts/logo/</code>. The generated <code>favicon.ico</code> uses the icon only (no text).</p>
</details>
