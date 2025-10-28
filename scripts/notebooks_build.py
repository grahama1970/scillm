#!/usr/bin/env python3
"""
Generate viewer notebooks (no helper logic). Each notebook calls public SciLLM APIs
exactly as a user would. Smokes should be run first to produce JSON artifacts.
"""
import nbformat
from nbformat.v4 import new_notebook, new_markdown_cell, new_code_cell
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NB_DIR = ROOT/"notebooks"
NB_DIR.mkdir(exist_ok=True)

def write_nb(name: str, cells):
    nb = new_notebook(cells=cells)
    (NB_DIR/name).write_text(nbformat.writes(nb))

def env_preamble_cells():
    """Common first cells to ensure retries/transport are enabled in live runs.
    These are no-ops if envs already set; prints a hint if tenacity is missing.
    """
    return [
        new_markdown_cell(
            (
                "### Runtime setup\n"
                "The following envs enable stable retries and quiet streaming.\n\n"
                "- `SCILLM_FORCE_HTTPX_STREAM=1`\n"
                "- `LITELLM_MAX_RETRIES=3`, `LITELLM_RETRY_AFTER=1`, `LITELLM_TIMEOUT=45`\n"
                "- Requires `tenacity` installed for backoff."
            )
        ),
        new_code_cell(
            (
                "import os\n"
                "os.environ.setdefault('SCILLM_FORCE_HTTPX_STREAM','1')\n"
                "os.environ.setdefault('LITELLM_MAX_RETRIES','3')\n"
                "os.environ.setdefault('LITELLM_RETRY_AFTER','1')\n"
                "os.environ.setdefault('LITELLM_TIMEOUT','45')\n"
                "try:\n"
                "    import tenacity  # noqa: F401\n"
                "    print('tenacity: ok')\n"
                "except Exception:\n"
                "    print('tenacity missing — run: pip install tenacity')\n"
            )
        ),
    ]

def nb_chutes_openai():
    cells = env_preamble_cells() + [
        new_markdown_cell("""
        # Chutes — OpenAI-Compatible

        Minimal chat using the OpenAI-compatible path.
        """.strip()),
        new_markdown_cell("""
        ## 1) Sync completion

        Minimal, blocking call using `scillm.completion`. Good for quick sanity.
        """.strip()),
        new_code_cell("""
import os
from scillm import completion
resp = completion(
  model=os.environ['CHUTES_MODEL'],
  api_base=os.environ['CHUTES_API_BASE'],
  api_key=None,
  custom_llm_provider='openai_like',
  # JSON for this tenant: Authorization Bearer
  extra_headers={'Authorization': f"Bearer {os.environ['CHUTES_API_KEY']}"},
  messages=[{'role':'user','content':'Say OK'}],
  max_tokens=8,
  temperature=0,
)
print(resp.choices[0].message.get('content',''))
        """.strip()),
    ]
    write_nb("01_chutes_openai_compatible.ipynb", cells)

def nb_router_parallel():
    cells = env_preamble_cells() + [
        new_markdown_cell("""
        # Router.parallel_acompletions
        """.strip()),
        new_code_cell("""
import os, asyncio
import nest_asyncio; nest_asyncio.apply()
from scillm import Router
router = Router(default_litellm_params={
  'api_base': os.environ['CHUTES_API_BASE'],
  'api_key': None,
  'custom_llm_provider': 'openai_like',
  'extra_headers': {'Authorization': f"Bearer {os.environ['CHUTES_API_KEY']}"},
})
prompts = ['OK-A','OK-B','OK-C']
reqs = [{
  'model': os.environ['CHUTES_MODEL'],
  'messages': [{'role':'user','content': p}],
  'kwargs': {'max_tokens': 8, 'temperature': 0, 'timeout': 20}
} for p in prompts]
async def run():
  outs = await router.parallel_acompletions(requests=reqs, concurrency=2)
  print([ (o.get('choices',[{}])[0].get('message',{}).get('content','') or '').strip() for o in outs ])
loop = asyncio.get_event_loop()
loop.run_until_complete(run())
        """.strip()),
    ]
    write_nb("02_router_parallel_batch.ipynb", cells)

def nb_model_list():
    cells = env_preamble_cells() + [
        new_markdown_cell("""
        # completion(model_list=...) — First Success
        """.strip()),
        new_code_cell("""
import os
from scillm import completion
base=os.environ['CHUTES_API_BASE']; key=os.environ['CHUTES_API_KEY']; model=os.environ['CHUTES_MODEL']
model_list=[
  {'model_name':'m1','litellm_params':{'model':model,'api_base':base,'api_key':None,'custom_llm_provider':'openai_like','extra_headers':{'Authorization': f"Bearer {key}"}}},
  {'model_name':'m2','litellm_params':{'model':model,'api_base':base,'api_key':None,'custom_llm_provider':'openai_like','extra_headers':{'Authorization': f"Bearer {key}"}}},
]
resp = completion(model='m1', model_list=model_list, messages=[{'role':'user','content':'Say OK'}], max_tokens=8, temperature=0)
print(resp.choices[0].message.get('content',''))
        """.strip()),
    ]
    write_nb("03_model_list_first_success.ipynb", cells)

def nb_advanced():
    cells = env_preamble_cells() + [
        new_markdown_cell("""
        # Advanced — Streaming and Tools
        """.strip()),
        new_markdown_cell("""
        ## Curl sanity (documentation)

        ```bash
        curl -X POST \\
          $CHUTES_API_BASE/chat/completions \\
          -H "Authorization: Bearer $CHUTES_API_KEY" \\
          -H "Content-Type: application/json" \\
          -d '{
            "model": "$CHUTES_VLM_MODEL",
            "messages": [{"role":"user","content":"Tell me a 250 word story."}],
            "stream": true,
            "max_tokens": 256,
            "temperature": 0.7
          }'
        ```
        """.strip()),
        new_code_cell("""
import os, asyncio
from scillm import acompletion
async def demo_stream():
  stream = await acompletion(
    model=os.environ.get('CHUTES_VLM_MODEL', os.environ['CHUTES_MODEL']),
    api_base=os.environ['CHUTES_API_BASE'],
    api_key=None,
    custom_llm_provider='openai_like',
    extra_headers={'Authorization': f"Bearer {os.environ['CHUTES_API_KEY']}"},
    messages=[{'role':'user','content':'In one word, say OK'}],
    temperature=0,
    max_tokens=8,
    stream=True,
  )
  async for ev in stream:
    d = getattr(ev,'delta',None) or ev
    text = (d.get('content') if isinstance(d,dict) else getattr(d,'content',None)) or ''
    if text: print(text,end='')
  print()
import sys
loop = asyncio.get_event_loop()
try:
  if loop.is_running():
    fut = asyncio.run_coroutine_threadsafe(demo_stream(), loop)
    fut.result()
  else:
    loop.run_until_complete(demo_stream())
except RuntimeError:
  asyncio.run(demo_stream())
        """.strip()),
        new_code_cell("""
import os, json
from scillm import completion
tools=[{'type':'function','function':{'name':'ack','description':'Acknowledge','parameters':{'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok']}}}]
resp = completion(
  model=os.environ.get('CHUTES_TOOLS_MODEL', os.environ.get('CHUTES_MODEL_ADVANCED', os.environ['CHUTES_MODEL'])),
  api_base=os.environ['CHUTES_API_BASE'],
  api_key=None,
  custom_llm_provider='openai_like',
  extra_headers={'Authorization': f"Bearer {os.environ['CHUTES_API_KEY']}"},
  messages=[{'role':'user','content':'Call ack with ok=true'}],
  tools=tools,
  temperature=0,
  max_tokens=32,
)
print(getattr(resp.choices[0], 'tool_calls', None))
        """.strip()),
    ]
    write_nb("04_advanced_streaming_tools.ipynb", cells)

def nb_fallback_infer():
    cells = env_preamble_cells() + [
        new_markdown_cell("""
        # Fallback Inference with Attribution

        Demonstrates automatic dynamic selection, fallbacks, and `scillm_meta` attribution.
        """.strip()),
        new_code_cell("""
import os
from scillm.extras import infer_with_fallback

resp, meta = infer_with_fallback(
    messages=[{'role':'user','content':'Return only {"ok":true} as JSON.'}],
    kind='text',
    require_json=True,
    max_retries=3, retry_after=1, timeout=45,
)
print('content:', resp.choices[0].message.get('content',''))
print('meta:', meta)
        """.strip()),
    ]
    write_nb("09_fallback_infer_with_meta.ipynb", cells)

def nb_auto_router_one_liner():
    cells = env_preamble_cells() + [
        new_markdown_cell("""
        # Auto Router — One Liner

        Create a Router from env automatically; discover, rank, route.
        """.strip()),
        new_code_cell("""
from scillm.extras import auto_router_from_env
router = auto_router_from_env(kind='text', require_json=True)
resp = router.completion(
  model=router.model_list[0]['model_name'],
  messages=[{'role':'user','content':'Return only {"ok":true} as JSON.'}],
  response_format={'type':'json_object'},
  max_retries=3, retry_after=1, timeout=45,
)
print(resp.choices[0].message.get('content',''))
        """.strip()),
    ]
    write_nb("10_auto_router_one_liner.ipynb", cells)

def nb_perplexity_provider():
    cells = env_preamble_cells() + [
        new_markdown_cell("""
        # Litellm Provider — Perplexity

        Demonstrates calling a Litellm-native provider (Perplexity) via SciLLM.
        Requires `PERPLEXITY_API_KEY`. If `PERPLEXITY_MODEL` is unset, defaults to `sonar`.
        """.strip()),
        new_code_cell("""
import os
from scillm import completion
model = os.environ.get('PERPLEXITY_MODEL','sonar')
key = os.environ.get('PERPLEXITY_API_KEY','')
if not key:
  print('PERPLEXITY_API_KEY not set — skipping live call')
else:
  resp = completion(
    model=model,
    custom_llm_provider='perplexity',
    api_key=key,
    messages=[{'role':'user','content':'In one word, say OK'}],
    max_tokens=8,
    temperature=0,
  )
  print(resp.choices[0].message.get('content',''))
        """.strip()),
        new_markdown_cell("""
        ## 2) Async acompletion

        Preferred for live apps and notebooks with other async work.
        """.strip()),
        new_code_cell("""
import os, asyncio, scillm
model = os.environ.get('PERPLEXITY_MODEL','sonar')
key = os.environ.get('PERPLEXITY_API_KEY','')
if not key:
  print('PERPLEXITY_API_KEY not set — skipping live call')
else:
  async def main():
    resp = await scillm.acompletion(
      model=model,
      custom_llm_provider='perplexity',
      api_key=key,
      messages=[{'role':'user','content':'In one word, say OK'}],
      max_tokens=8,
      temperature=0,
      timeout=45,
    )
    print(resp.choices[0].message.get('content',''))
  import nest_asyncio, asyncio as _asyncio
  nest_asyncio.apply()
  loop = _asyncio.get_event_loop()
  loop.run_until_complete(main())
        """.strip()),
        new_markdown_cell("""
        ## 3) Router.parallel_acompletions (batch of 3)

        Fan out three requests concurrently. Always set `timeout` in `kwargs`.
        """.strip()),
        new_code_cell("""
import os, asyncio, scillm
model = os.environ.get('PERPLEXITY_MODEL','sonar')
key = os.environ.get('PERPLEXITY_API_KEY','')
if not key:
  print('PERPLEXITY_API_KEY not set — skipping live call')
else:
  router = scillm.Router(model_list=[{
    'model_name': 'ppx',
    'litellm_params': {
      'custom_llm_provider': 'perplexity',
      'model': model,
      'api_key': key,
    }
  }])
  prompts = ['Say OK-A','Say OK-B','Say OK-C']
  reqs = [{
    'model': 'ppx',
    'messages': [{'role':'user','content': p}],
    'kwargs': {'max_tokens': 8, 'temperature': 0, 'timeout': 30}
  } for p in prompts]
  async def run():
    outs = await router.parallel_acompletions(requests=reqs, concurrency=3)
    print([o.get('choices',[{}])[0].get('message',{}).get('content','') for o in outs])
  import nest_asyncio, asyncio as _asyncio
  nest_asyncio.apply()
  loop = _asyncio.get_event_loop()
  loop.run_until_complete(run())
        """.strip()),
    ]
    write_nb("11_provider_perplexity.ipynb", cells)

def nb_openai_provider():
    cells = env_preamble_cells() + [
        new_markdown_cell("""
        # Litellm Provider — OpenAI (normal models)

        Minimal OpenAI call using Bearer auth to api.openai.com.
        Requires `OPENAI_API_KEY` and optional `OPENAI_MODEL` (default `gpt-4o-mini`).
        """.strip()),
        new_code_cell("""
import os
from scillm import completion
key = os.environ.get('OPENAI_API_KEY','')
model = os.environ.get('OPENAI_MODEL','gpt-4o-mini')
if not key:
  print('OPENAI_API_KEY not set — skipping live call')
else:
  resp = completion(
    model=model,
    custom_llm_provider='openai',
    api_key=key,
    messages=[{'role':'user','content':'In one word, say OK'}],
    max_tokens=8,
    temperature=0,
  )
  print(resp.choices[0].message.get('content',''))
        """.strip()),
    ]
    write_nb("12_provider_openai.ipynb", cells)

def nb_anthropic_provider():
    cells = env_preamble_cells() + [
        new_markdown_cell("""
        # Litellm Provider — Anthropic (normal models)

        Minimal Anthropic call. Requires `ANTHROPIC_API_KEY` and optional `ANTHROPIC_MODEL`
        (default `claude-3-haiku-20240307`).
        """.strip()),
        new_code_cell("""
import os, litellm
key = os.environ.get('ANTHROPIC_API_KEY','')
model = os.environ.get('ANTHROPIC_MODEL','claude-3-haiku-20240307')
if not key:
  print('ANTHROPIC_API_KEY not set — skipping live call')
else:
  resp = litellm.completion(
    model=model,
    custom_llm_provider='anthropic',
    api_key=key,
    messages=[{'role':'user','content':'In one word, say OK'}],
    max_tokens=8,
    temperature=0,
  )
  print(resp.choices[0].message.get('content',''))
        """.strip()),
    ]
    write_nb("13_provider_anthropic.ipynb", cells)

def nb_codex_agent_doctor():
    cells = [
        new_code_cell(
            (
                "import os, pathlib\n"
                "# Ensure working directory is repo root for relative paths\n"
                "p=pathlib.Path().resolve()\n"
                "if (p/'Makefile').exists():\n"
                "    pass\n"
                "elif p.name=='notebooks' and (p.parent/'Makefile').exists():\n"
                "    os.chdir(p.parent)\n"
                "print('cwd:', os.getcwd())\n"
            ).strip()
        ),
        new_markdown_cell("""
        # Codex‑Agent — Doctor

        Verifies the Codex Agent surface is healthy and can complete a JSON chat.
        """.strip()),
        new_code_cell("""
# 1) Health
import os, json, subprocess, sys
base = os.environ.get('CODEX_AGENT_API_BASE', 'http://127.0.0.1:8790')
code = subprocess.run(['bash','-lc', f"curl -fsS {base}/healthz"], capture_output=True, text=True)
print('healthz:', code.stdout.strip())
""".strip()),
        new_code_cell("""
# 2) Doctor
import runpy
out = runpy.run_path('debug/codex_agent_doctor.py')
print(out.get('status','doctor: unknown'))
""".strip()),
        new_code_cell("""
# 3) Minimal JSON chat via scillm
import os
from scillm import completion
resp = completion(
  model=os.environ.get('CHUTES_MODEL'),
  api_base=os.environ.get('CHUTES_API_BASE'),
  api_key=None,
  custom_llm_provider='openai_like',
  extra_headers={'x-api-key': os.environ.get('CHUTES_API_KEY',''), 'Authorization': os.environ.get('CHUTES_API_KEY','')},
  messages=[{'role':'user','content':'Return only {"ok":true} as JSON.'}],
  response_format={'type':'json_object'},
  temperature=0,
  max_tokens=8,
)
print(resp.choices[0].message.get('content',''))
""".strip()),
    ]
    write_nb("05_codex_agent_doctor.ipynb", cells)

def nb_mini_agent_doctor():
    cells = [
        new_code_cell(
            (
                "import os, pathlib\n"
                "p=pathlib.Path().resolve()\n"
                "if (p/'Makefile').exists():\n"
                "    pass\n"
                "elif p.name=='notebooks' and (p.parent/'Makefile').exists():\n"
                "    os.chdir(p.parent)\n"
                "print('cwd:', os.getcwd())\n"
            ).strip()
        ),
        new_markdown_cell("""
        # Mini‑Agent — Doctor

        Runs the mini‑agent verification script and prints a minimal JSON chat result.
        """.strip()),
        new_code_cell("""
import runpy
out = runpy.run_path('debug/verify_mini_agent.py')
print(out)
""".strip()),
    ]
    write_nb("06_mini_agent_doctor.ipynb", cells)

def nb_codeworld_mcts():
    cells = [
        new_code_cell(
            (
                "import os, pathlib\n"
                "p=pathlib.Path().resolve()\n"
                "if (p/'Makefile').exists():\n"
                "    pass\n"
                "elif p.name=='notebooks' and (p.parent/'Makefile').exists():\n"
                "    os.chdir(p.parent)\n"
                "print('cwd:', os.getcwd())\n"
            ).strip()
        ),
        new_markdown_cell("""
        # CodeWorld — MCTS Scenario

        Starts the bridge (local uvicorn) and runs the MCTS scenario end‑to‑end.
        """.strip()),
        new_code_cell("""
import subprocess, sys, time, os
# Start bridge
p = subprocess.Popen([sys.executable,'-m','uvicorn','src.codeworld.bridge.server:app','--port','8887','--host','127.0.0.1'])
time.sleep(1.5)
try:
  env = os.environ.copy()
  env['CODEWORLD_BASE'] = 'http://127.0.0.1:8887'
  code = subprocess.run([sys.executable,'scenarios/codeworld_mcts_chutes_autogen.py'], env=env)
  print('mcts exit:', code.returncode)
finally:
  p.terminate()
""".strip()),
    ]
    write_nb("07_codeworld_mcts.ipynb", cells)

def nb_certainly_bridge():
    cells = [
        new_code_cell(
            (
                "import os, pathlib\n"
                "p=pathlib.Path().resolve()\n"
                "if (p/'Makefile').exists():\n"
                "    pass\n"
                "elif p.name=='notebooks' and (p.parent/'Makefile').exists():\n"
                "    os.chdir(p.parent)\n"
                "print('cwd:', os.getcwd())\n"
            ).strip()
        ),
        new_markdown_cell("""
        # Certainly — Bridge Doctor

        Brings up the bridge container and runs the doctor script.
        """.strip()),
        new_code_cell("""
import subprocess
print('bringing up bridge…')
subprocess.run(['bash','-lc','make certainly-bridge-up'], check=False)
print('doctor…')
subprocess.run(['bash','-lc','bash debug/certainly_bridge_doctor.sh'], check=False)
""".strip()),
    ]
    write_nb("08_certainly_bridge.ipynb", cells)

def nb_provider_matrix():
    cells = env_preamble_cells() + [
        new_markdown_cell("""
        # Provider Matrix — OpenAI, Anthropic, Perplexity, Chutes

        Runs a minimal inference for common providers and a Chutes call with attribution.
        Only executes sections where required env vars are present.
        """.strip()),
        new_code_cell("""
import os, json
from scillm import completion
from scillm.extras import infer_with_fallback

def _print_resp(tag, resp):
    try:
        content = resp.choices[0].message.get('content','')
    except Exception:
        content = str(resp)
    served = getattr(resp,'model', None) or (resp.get('model') if isinstance(resp,dict) else None)
    print(f"[{tag}] content=", content)
    if served:
        print(f"[{tag}] served_model=", served)

# OpenAI
ok = os.environ.get('OPENAI_API_KEY')
if ok:
    try:
        r = completion(
            model=os.environ.get('OPENAI_MODEL','gpt-4o-mini'),
            custom_llm_provider='openai',
            api_key=ok,
            messages=[{'role':'user','content':'In one word, say OK'}],
            max_tokens=8, temperature=0,
        )
        _print_resp('openai', r)
    except Exception as e:
        print('[openai] error —', str(e)[:160])
else:
    print('[openai] OPENAI_API_KEY not set — skipped')

# Anthropic
ak = os.environ.get('ANTHROPIC_API_KEY')
if ak:
    try:
        r = completion(
            model=os.environ.get('ANTHROPIC_MODEL','claude-3-haiku-20240307'),
            custom_llm_provider='anthropic',
            api_key=ak,
            messages=[{'role':'user','content':'In one word, say OK'}],
            max_tokens=8, temperature=0,
        )
        _print_resp('anthropic', r)
    except Exception as e:
        print('[anthropic] error —', str(e)[:160])
else:
    print('[anthropic] ANTHROPIC_API_KEY not set — skipped')

# Perplexity
pk = os.environ.get('PERPLEXITY_API_KEY')
if pk:
    try:
        r = completion(
            model=os.environ.get('PERPLEXITY_MODEL','sonar'),
            custom_llm_provider='perplexity',
            api_key=pk,
            messages=[{'role':'user','content':'In one word, say OK'}],
            max_tokens=8, temperature=0,
        )
        _print_resp('perplexity', r)
    except Exception as e:
        print('[perplexity] error —', str(e)[:160])
else:
    print('[perplexity] PERPLEXITY_API_KEY not set — skipped')

# Chutes (OpenAI-compatible) with attribution-first fallback
cb = os.environ.get('CHUTES_API_BASE_1') or os.environ.get('CHUTES_API_BASE')
ck = os.environ.get('CHUTES_API_KEY_1') or os.environ.get('CHUTES_API_KEY')
if cb and ck:
    r, meta = infer_with_fallback(
        messages=[{'role':'user','content':'Return only {"ok":true} as JSON.'}],
        kind='text', require_json=True,
        max_retries=3, retry_after=1, timeout=45,
    )
    _print_resp('chutes', r)
    print('[chutes] meta=', json.dumps(meta, indent=2))
else:
    print('[chutes] CHUTES_API_BASE/CHUTES_API_KEY not set — skipped')
        """.strip()),
    ]
    write_nb("14_provider_matrix.ipynb", cells)

if __name__ == "__main__":
    nb_chutes_openai(); nb_router_parallel(); nb_model_list(); nb_advanced(); nb_fallback_infer()
    nb_auto_router_one_liner(); nb_perplexity_provider(); nb_openai_provider(); nb_anthropic_provider()
    nb_provider_matrix();
    nb_codex_agent_doctor(); nb_mini_agent_doctor(); nb_codeworld_mcts(); nb_certainly_bridge()
    print("viewer notebooks written in notebooks/")
