#!/usr/bin/env python3
from __future__ import annotations
import json
from pathlib import Path

NB = Path('notebooks')

BLOCKS = {
  '12_provider_openai.ipynb': (
    'Provider — OpenAI',
    '\n'.join([
      '### Why/When/Gotchas/Troubleshooting',
      '- Why: litellm-native OpenAI provider through SciLLM for standard chat.',
      '- When: you need baseline OpenAI behavior (JSON/chat/stream).',
      '- Gotchas: 401 → check OPENAI_API_KEY; org/project scoping may block; rate limits → set max_retries/retry_after; base defaults to api.openai.com.',
      '- Troubleshooting: run a curl to /v1/models with Bearer; verify model id and account access.'
    ]),
  ),
  '13_provider_anthropic.ipynb': (
    'Provider — Anthropic',
    '\n'.join([
      '### Why/When/Gotchas/Troubleshooting',
      '- Why: litellm-native Anthropic provider via SciLLM for Claude family.',
      '- When: Claude models (haiku/sonnet/opus) for fast, low-cost, or high-quality responses.',
      "- Gotchas: 401 'invalid x-api-key' → check ANTHROPIC_API_KEY validity/region; model availability varies by account; set timeouts/retries for long prompts.",
      '- Troubleshooting: curl /v1/models or a minimal chat; ensure the selected model is enabled for your key.'
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
    for c in nb['cells']:
        if c.get('cell_type')=='code':
            c.setdefault('outputs', [])
            c.setdefault('execution_count', None)
    nb_path.write_text(json.dumps(nb, indent=2))

def main():
    for name,(sel,md) in BLOCKS.items():
        path = NB/name
        if path.exists():
            insert_teach(path, sel, md)
            print('patched', name)
        else:
            print('missing', name)

if __name__ == '__main__':
    main()

