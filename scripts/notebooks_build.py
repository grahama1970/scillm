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
        new_code_cell("""
import os, litellm
resp = litellm.completion(
  model=os.environ['CHUTES_MODEL'],
  api_base=os.environ['CHUTES_API_BASE'],
  api_key=None,
  custom_llm_provider='openai_like',
  # JSON paved path: use x-api-key (no Bearer)
  extra_headers={'x-api-key': os.environ['CHUTES_API_KEY']},
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
from litellm import Router
router = Router(default_litellm_params={
  'api_base': os.environ['CHUTES_API_BASE'],
  'api_key': None,
  'custom_llm_provider': 'openai_like',
  # JSON paved path: x-api-key only
  'extra_headers': {'x-api-key': os.environ['CHUTES_API_KEY']},
})
prompts = ['Say OK-A','Say OK-B','Say OK-C']
reqs = [{'model': os.environ['CHUTES_MODEL'], 'messages':[{'role':'user','content':p}], 'kwargs': {'max_tokens': 8, 'temperature': 0}} for p in prompts]
async def run():
  out = await router.parallel_acompletions(requests=reqs, concurrency=3)
  print([o.get('choices',[{}])[0].get('message',{}).get('content','') for o in out])
asyncio.run(run())
        """.strip()),
    ]
    write_nb("02_router_parallel_batch.ipynb", cells)

def nb_model_list():
    cells = env_preamble_cells() + [
        new_markdown_cell("""
        # completion(model_list=...) — First Success
        """.strip()),
        new_code_cell("""
import os, litellm
base=os.environ['CHUTES_API_BASE']; key=os.environ['CHUTES_API_KEY']; model=os.environ['CHUTES_MODEL']
model_list=[
  {'model_name':'m1','litellm_params':{'model':model,'api_base':base,'api_key':None,'custom_llm_provider':'openai_like','extra_headers':{'x-api-key':key}}},
  {'model_name':'m2','litellm_params':{'model':model,'api_base':base,'api_key':None,'custom_llm_provider':'openai_like','extra_headers':{'x-api-key':key}}},
]
resp = litellm.completion(model='m1', model_list=model_list, messages=[{'role':'user','content':'Say OK'}], max_tokens=8, temperature=0)
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
import os, asyncio, litellm
async def demo_stream():
  stream = await litellm.acompletion(
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
asyncio.run(demo_stream())
        """.strip()),
        new_code_cell("""
import os, json, litellm
tools=[{'type':'function','function':{'name':'ack','description':'Acknowledge','parameters':{'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok']}}}]
resp = litellm.completion(
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

def nb_codex_agent_doctor():
    cells = [
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
import os, litellm
resp = litellm.completion(
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

if __name__ == "__main__":
    nb_chutes_openai(); nb_router_parallel(); nb_model_list(); nb_advanced()
    nb_codex_agent_doctor(); nb_mini_agent_doctor(); nb_codeworld_mcts(); nb_certainly_bridge()
    print("viewer notebooks written in notebooks/")
