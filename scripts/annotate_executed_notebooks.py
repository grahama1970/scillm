#!/usr/bin/env python3
from __future__ import annotations
import json, os, time
from pathlib import Path

NB_DIR = Path('notebooks')
META_VARS = [
  'SCILLM_FORCE_HTTPX_STREAM', 'LITELLM_MAX_RETRIES', 'LITELLM_RETRY_AFTER',
  'LITELLM_TIMEOUT', 'SCILLM_ALLOW_TOOLS_SMOKE', 'SCILLM_ALLOW_STREAM_SMOKE'
]

def annotate(path: Path):
    nb = json.loads(path.read_text())
    ts = time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())
    env_lines = [f"- {k}={os.environ.get(k,'')}" for k in META_VARS]
    cell = {
        'cell_type':'markdown', 'metadata':{},
        'source':[f"Run metadata (generated {ts})\n\n" + "\n".join(env_lines)]
    }
    cells = nb.get('cells', [])
    cells.insert(0, cell)
    for c in cells:
        if c.get('cell_type')=='code':
            c.setdefault('outputs', [])
            c.setdefault('execution_count', None)
    nb['cells'] = cells
    path.write_text(json.dumps(nb, indent=2))

def main():
    # annotate any executed notebooks in root or notebooks/executed/
    for p in NB_DIR.glob('*_executed.ipynb'):
        annotate(p)
        print('annotated', p)
    exec_dir = NB_DIR / 'executed'
    if exec_dir.exists():
        for p in exec_dir.glob('*_executed.ipynb'):
            annotate(p)
            print('annotated', p)

if __name__ == '__main__':
    main()
