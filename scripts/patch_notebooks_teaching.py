#!/usr/bin/env python3
from __future__ import annotations
import json, sys
from pathlib import Path

NB_DIR = Path('notebooks')

TEACH = {
  '01_chutes_openai_compatible.ipynb': (
    '# Chutes — OpenAI-Compatible',
    '\n'.join([
      '#### Why use this',
      '- Single JSON response from your Chutes tenant, no Router needed.',
      '#### When to choose',
      '- One-off calls, simple scripts, synchronous pipelines.',
      '#### Prereqs',
      '- CHUTES_API_BASE=https://llm.chutes.ai/v1',
      '- CHUTES_API_KEY, CHUTES_TEXT_MODEL',
      '#### Minimal call',
      '- scillm.completion with custom_llm_provider="openai_like", api_key=None,',
      '  extra_headers={"Authorization": "Bearer …"}, response_format={"type":"json_object"}, temperature=0.',
      '#### Gotchas',
      '- Auth: /v1/chat/completions requires Authorization: Bearer for this tenant; x-api-key alone returns 401.',
      '- Timeouts: set timeout=45 on long prompts.',
      '- JSON: some models may fence JSON; set SCILLM_JSON_SANITIZE=1 if needed.',
      '#### Troubleshooting',
      '- 401 → header mismatch. Verify curl -H "Authorization: Bearer $CHUTES_API_KEY" POST /chat/completions.',
    ]),
  ),
  '02_router_parallel_batch.ipynb': (
    '# Router.parallel_acompletions',
    '\n'.join([
      '#### Why use this',
      '- Issue N calls concurrently with a simple result shape.',
      '#### When to choose',
      '- Batch jobs, speedups on independent prompts.',
      '#### Prereqs',
      '- Same env as 01.',
      '#### Minimal pattern',
      '- Router(default_litellm_params={…, extra_headers={Authorization: Bearer …}})',
      '- requests: list of dicts with model, messages, kwargs (set timeout per item).',
      '- await router.parallel_acompletions(requests, concurrency=K)',
      '#### Gotchas',
      '- Always set per-item timeout in kwargs; otherwise a single stall delays completion.',
      '- Choose a sane concurrency (e.g., 3–8).',
      '- Jupyter: cell uses nest_asyncio; avoid asyncio.run directly.',
      '#### Troubleshooting',
      '- "hangs" → missing per-item timeout or event loop issue; reduce concurrency and inspect stderr.',
    ]),
  ),
  '03_model_list_first_success.ipynb': (
    '# completion(model_list=...) — First Success',
    '\n'.join([
      '#### Why use this',
      '- Prefer the first healthy deployment; automatic fallback if the first fails.',
      '#### When to choose',
      '- “Try primary then backup” without Router.',
      '#### Prereqs',
      '- Two or more deployments in model_list (same model, different bases).',
      '#### Minimal pattern',
      '- completion(model="m1", model_list=[…], messages=[…])',
      '- Each deployment includes extra_headers={"Authorization": "Bearer …"}.',
      '#### Gotchas',
      '- Auth must be present on every deployment.',
      '- Per-deployment headers must be merged into headers.',
      '#### Troubleshooting',
      '- 401 → fix headers on every model_list entry.',
    ]),
  ),
  '04_advanced_streaming_tools.ipynb': (
    '# Advanced — Streaming and Tools',
    '\n'.join([
      '#### Why use this',
      '- Demonstrate SSE streaming and function/tool-calling.',
      '#### When to choose',
      '- Interactive apps, live transcription/long responses with early rendering.',
      '#### Prereqs',
      '- CHUTES_*; stream = True for streaming cell.',
      '#### Minimal patterns',
      '- Streaming with scillm.acompletion + async for chunk in stream',
      '- Tools with scillm.completion, tools=[…], tool_choice="auto"',
      '#### Gotchas',
      '- Jupyter: the cell uses nest_asyncio; avoid asyncio.run directly.',
      '- Streaming chunk shapes vary; example normalizes to text.',
      '#### Troubleshooting',
      '- No chunks → check Bearer header and model supports streaming.',
    ]),
  ),
  '11_provider_perplexity.ipynb': (
    '# Litellm Provider — Perplexity',
    '\n'.join([
      '#### Why use this',
      '- Litellm-native provider; show sync, async, and batch.',
      '#### When to choose',
      '- Non-Chutes calls; quick provider sanity checks.',
      '#### Prereqs',
      '- PERPLEXITY_API_KEY; PERPLEXITY_MODEL defaults to "sonar".',
      '#### Gotchas',
      '- Model names drift; "sonar" is a safe default; set PERPLEXITY_MODEL if needed.',
      '#### Troubleshooting',
      '- Invalid model → check docs; set a concrete model id.',
    ]),
  ),
  '14_provider_matrix.ipynb': (
    '# Provider Matrix — OpenAI, Anthropic, Perplexity, Chutes',
    '\n'.join([
      '#### Why use this',
      '- Validate keys and see minimal “hello world” across providers.',
      '#### When to choose',
      '- First-time setup, environment triage.',
      '#### Notes',
      '- Each section skips if its API key is missing.',
    ]),
  ),
}


def patch_one(nb_path: Path, sel_text: str, teach_md: str):
    nb = json.loads(nb_path.read_text())
    cells = nb.get('cells', [])
    # insert after title cell that contains sel_text
    for i,c in enumerate(cells):
        if c.get('cell_type')=='markdown' and sel_text in ''.join(c.get('source') or []):
            cells.insert(i+1, { 'cell_type':'markdown', 'metadata':{}, 'source':[teach_md] })
            break
    nb_path.write_text(json.dumps(nb, indent=2))


def fix_model_list_headers(nb_path: Path):
    nb = json.loads(nb_path.read_text())
    changed = False
    for c in nb.get('cells', []):
        if c.get('cell_type')=='code':
            src = ''.join(c.get('source') or [])
            if 'model_list' in src and "'x-api-key'" in src:
                src = src.replace("'x-api-key': key", "'Authorization': f'Bearer {key}'")
                src = src.replace("'x-api-key':key", "'Authorization': f'Bearer {key}'")
                src = src.replace("extra_headers':{'x-api-key':", "extra_headers':{'Authorization': f'Bearer ")
                c['source'] = [src]
                changed = True
    if changed:
        nb_path.write_text(json.dumps(nb, indent=2))


def main():
    for fname,(sel, md) in TEACH.items():
        path = NB_DIR/fname
        if not path.exists():
            print('skip missing', path)
            continue
        patch_one(path, sel, md)
    # Specific code fix for 03
    fix_model_list_headers(NB_DIR/'03_model_list_first_success.ipynb')
    print('patched teaching blocks')


if __name__ == '__main__':
    main()
