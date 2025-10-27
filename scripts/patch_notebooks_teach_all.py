#!/usr/bin/env python3
from __future__ import annotations
import json
from pathlib import Path

NB = Path('notebooks')

TEACH_BLOCKS = {
  '04a_tools_only.ipynb': (
    '# Advanced — Tools',
    '\n'.join([
      '### Why/When/Gotchas/Troubleshooting',
      '- Why: call function tools (OpenAI-format) with SciLLM.',
      '- When: structured actions, deterministic JSON I/O.',
      '- Gotchas: rate limits (429) — set max_retries/retry_after/timeout; annotate tool schema clearly.',
      '- Troubleshooting: set SCILLM_ALLOW_TOOLS_SMOKE=1 to run the cell; inspect printed tool_calls.'
    ]),
  ),
  '04b_streaming_demo.ipynb': (
    'Advanced — Streaming',
    '\n'.join([
      '### Why/When/Gotchas/Troubleshooting',
      '- Why: stream partial tokens for interactive UIs.',
      '- When: long responses, live rendering.',
      '- Gotchas: notebook event loops — use nest_asyncio or loop-aware pattern; throttle to a few chunks.',
      '- Troubleshooting: set SCILLM_ALLOW_STREAM_SMOKE=1 to execute; check Bearer header + model supports stream.'
    ]),
  ),
  '05_codex_agent_doctor.ipynb': (
    'Codex‑Agent — Doctor',
    '\n'.join([
      '### What this checks',
      '- Health endpoint, doctor script, and a minimal JSON chat via SciLLM.',
      '### When it fails',
      '- Healthz down: start the agent or set CODEX_AGENT_API_BASE.',
      '- JSON chat error: confirm CHUTES_* and model id; verify Bearer header.'
    ]),
  ),
  '06_mini_agent_doctor.ipynb': (
    'Mini‑Agent — Doctor',
    '\n'.join([
      '### What this checks',
      '- Mini‑agent verification script and minimal JSON chat.',
      '### When it fails',
      '- Missing env or network issue: re-run debug/verify_mini_agent.py and inspect output.'
    ]),
  ),
  '07_codeworld_mcts.ipynb': (
    'CodeWorld — MCTS',
    '\n'.join([
      '### What this does',
      '- Starts the local bridge (uvicorn) and runs an MCTS scenario end‑to‑end.',
      '### Keys to verify',
      '- Response includes run_manifest.mcts_stats with best_variant and seed.',
      '### Troubleshooting',
      '- Address in use: kill prior uvicorn; missing keys: set CHUTES_* if required by generators.'
    ]),
  ),
  '08_certainly_bridge.ipynb': (
    'Certainly Bridge — Doctor',
    '\n'.join([
      '### What this indicates',
      '- Confirms the bridge is reachable and responding with expected summary signals.',
      '### Troubleshooting',
      '- Ensure docker/compose services are up if required; check base URL and keys.'
    ]),
  ),
  '09_fallback_infer_with_meta.ipynb': (
    'Fallback Inference',
    '\n'.join([
      '### Why/When',
      '- Reliability: return first success across deployments and record served_model.',
      '### Read the meta',
      '- meta["attempts"] shows errors per target; use it to triage availability.'
    ]),
  ),
  '10_auto_router_one_liner.ipynb': (
    'Auto Router — One Liner',
    '\n'.join([
      '### What it does',
      '- Builds a Router from CHUTES_* envs, ranks by availability/utilization, routes JSON by default.',
      '### Troubleshooting',
      '- No candidates: check numbered CHUTES_API_BASE_n/CHUTES_API_KEY_n; verify /v1/models 200.'
    ]),
  ),
  '14_provider_matrix.ipynb': (
    'Provider Matrix',
    '\n'.join([
      '### Purpose',
      '- Minimal “hello world” across providers; skips when the API key is missing; prints error snippet otherwise.',
      '### Tip',
      '- Use this first to validate env before deeper flows.'
    ]),
  ),
}

def insert_teach(nb_path: Path, title_contains: str, md: str):
    nb = json.loads(nb_path.read_text())
    cells = nb.get('cells', [])
    idx = None
    for i,c in enumerate(cells):
        if c.get('cell_type')=='markdown' and title_contains in ''.join(c.get('source') or []):
            idx = i+1
            break
    cell = {"cell_type":"markdown","metadata":{},"source":[md]}
    if idx is None:
        cells.insert(0, cell)
    else:
        cells.insert(idx, cell)
    nb['cells'] = cells
    # normalize code cell required fields
    for c in nb['cells']:
        if c.get('cell_type')=='code':
            c.setdefault('outputs', [])
            c.setdefault('execution_count', None)
    nb_path.write_text(json.dumps(nb, indent=2))

def main():
    for name,(sel,md) in TEACH_BLOCKS.items():
        path = NB/name
        if path.exists():
            insert_teach(path, sel, md)
            print('patched', name)
        else:
            print('missing', name)

if __name__ == '__main__':
    main()

