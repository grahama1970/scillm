#!/usr/bin/env python3
from __future__ import annotations
import json
from pathlib import Path

NB = Path('notebooks')

TARGETS = {
  '01_chutes_openai_compatible.ipynb': '# Chutes — OpenAI-Compatible',
  '02_router_parallel_batch.ipynb': '# Router.parallel_acompletions',
  '03_model_list_first_success.ipynb': '# completion(model_list=...)',
  '04a_tools_only.ipynb': '# Advanced — Tools',
  '04b_streaming_demo.ipynb': '# Advanced — Streaming',
}

CURL_BLOCK = """
### Troubleshooting with curl (Chutes)

If a SciLLM call fails, verify the tenant directly:

1) List models
```bash
curl -sS -H "Authorization: Bearer $CHUTES_API_KEY" \
  "$CHUTES_API_BASE/models" | jq '. | {count:(.data//[])|length} // length'
```

2) Minimal JSON chat (non-stream)
```bash
curl -sS -X POST -H "Authorization: Bearer $CHUTES_API_KEY" \
  -H "Content-Type: application/json" \
  "$CHUTES_API_BASE/chat/completions" \
  -d '{
    "model": "'$CHUTES_TEXT_MODEL'",
    "messages": [{"role":"user","content":"Return only {\"ok\":true} as JSON."}],
    "response_format": {"type":"json_object"},
    "max_tokens": 16,
    "temperature": 0
  }' | jq '.choices[0].message.content // empty'
```

3) Streaming (text) — watch for data: lines
```bash
curl -sN -X POST -H "Authorization: Bearer $CHUTES_API_KEY" \
  -H "Content-Type: application/json" \
  "$CHUTES_API_BASE/chat/completions" \
  -d '{
    "model": "'$CHUTES_TEXT_MODEL'",
    "messages": [{"role":"user","content":"Tell me a 10 word story."}],
    "stream": true
  }'
```

4) Multimodal (image_url)
```bash
IMG_URL=${SCILLM_DEMO_IMAGE:-https://upload.wikimedia.org/wikipedia/commons/thumb/3/3f/Fronalpstock_big.jpg/320px-Fronalpstock_big.jpg}
curl -sS -X POST -H "Authorization: Bearer $CHUTES_API_KEY" \
  -H "Content-Type: application/json" \
  "$CHUTES_API_BASE/chat/completions" \
  -d '{
    "model": "'$CHUTES_VLM_MODEL'",
    "messages": [{
      "role":"user",
      "content": [
        {"type":"text","text":"Say OK and a color in the image."},
        {"type":"image_url","image_url": {"url": "'"$IMG_URL"'"}}
      ]
    }],
    "max_tokens": 32,
    "temperature": 0
  }' | jq '.choices[0].message.content // empty'
```
"""

def insert_curl(nb_path: Path, title_marker: str):
    nb = json.loads(nb_path.read_text())
    cells = nb.get('cells', [])
    # avoid duplicate insert
    for c in cells:
        if c.get('cell_type')=='markdown' and 'Troubleshooting with curl' in ''.join(c.get('source') or []):
            return
    # find title cell
    idx = None
    for i,c in enumerate(cells):
        if c.get('cell_type')=='markdown' and title_marker in ''.join(c.get('source') or []):
            idx = i+1
            break
    md_cell = {"cell_type":"markdown","metadata":{},"source":[CURL_BLOCK]}
    if idx is None:
        cells.append(md_cell)
    else:
        cells.insert(idx, md_cell)
    # normalize code cells
    for c in cells:
        if c.get('cell_type')=='code':
            c.setdefault('outputs', [])
            c.setdefault('execution_count', None)
    nb['cells']=cells
    nb_path.write_text(json.dumps(nb, indent=2))

def main():
    for name, marker in TARGETS.items():
        p = NB/name
        if p.exists():
            insert_curl(p, marker)
            print('patched', name)
        else:
            print('missing', name)

if __name__ == '__main__':
    main()

