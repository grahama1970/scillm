# Codex Agent (Experimental, Env‑Gated)

Integrate an experimental “codex‑agent” via the LiteLLM Router for iterative, tool‑using workflows.
It is opt‑in and disabled by default. Use through Router like any other model.

- Provider slug: `codex-agent` (alias: `codex_cli_agent`)
- Status: Experimental; disabled by default (env‑gated)

## Enabling (Environment‑Gated)

Set the feature flag to opt‑in explicitly:

```bash
export LITELLM_ENABLE_CODEX_AGENT=1
```

Add a Router model entry that points to the provider alias (no client changes):

```python
from litellm import Router
router = Router(model_list=[
  {"model_name": "codex-agent-1", "litellm_params": {"model": "codex-agent/mini"}},
])
```

## Usage

```python
from litellm import Router
import os

os.environ["LITELLM_ENABLE_CODEX_AGENT"] = "1"
router = Router(model_list=[
  {"model_name": "codex-agent-1", "litellm_params": {"model": "codex-agent/mini"}},
])

resp = await router.acompletion(
  model="codex-agent-1",
  messages=[{"role": "user", "content": "Plan steps and use tools as needed."}],
  reasoning_effort="high",  # maps to {"reasoning":{"effort":"high"}}
)
print(resp.choices[0].message.content)
```

### Configure HTTP endpoint (required) and auth (optional)

This provider is **HTTP-only**. Point it at an OpenAI-compatible Chat Completions base:

```bash
export LITELLM_ENABLE_CODEX_AGENT=1
export CODEX_AGENT_API_BASE=http://127.0.0.1:8788    # e.g., mini-agent shim /v1/chat/completions
export CODEX_AGENT_API_KEY=sk-your-key               # optional; becomes Authorization: Bearer ...
curl -sS "$CODEX_AGENT_API_BASE/v1/models" | jq .   # fetch valid model ids (e.g., "gpt-5")
```

For a local stack with the bundled toolchains (mini-agent shim + codex sidecar + Ollama), run:

```bash
docker compose -f local/docker/compose.agents.yml up --build -d
export CODEX_AGENT_API_BASE=http://127.0.0.1:8077
# optional: export CODEX_AGENT_API_KEY=...
# if echo is disabled, mount ~/.codex/auth.json → /root/.codex/auth.json:ro in the container
```

See `scenarios/codex_agent_docker_release.py` for a ready-to-run validation script that targets the Docker sidecar.

Then wire the Router (note the `api_base`/`api_key` in `litellm_params`):

```python
from litellm import Router
import os

os.environ["LITELLM_ENABLE_CODEX_AGENT"] = "1"
router = Router(model_list=[
  {"model_name":"codex-agent-1","litellm_params":{
      "model":"codex-agent/gpt-5",
      "api_base": os.getenv("CODEX_AGENT_API_BASE"),
      "api_key":  os.getenv("CODEX_AGENT_API_KEY","")
  }},
])

resp = await router.acompletion(
  model="codex-agent-1",
  messages=[{"role":"user","content":"Plan steps and finish."}],
  reasoning_effort="high",
)
print(resp.choices[0].message.content)
```

## Notes

- Experimental surface; subject to change.
- Off by default; enable only via `LITELLM_ENABLE_CODEX_AGENT=1`.
- Use through Router like any other model; keep CI guarded by the flag.
- Local vs Docker:
  - Local mini‑agent: `uvicorn litellm.experimental_mcp_client.mini_agent.agent_proxy:app --host 127.0.0.1 --port 8788`
  - Docker sidecar: `docker compose -f local/docker/compose.agents.yml up --build -d codex-sidecar` (base `http://127.0.0.1:8077`)
  - Always set `CODEX_AGENT_API_BASE` without `/v1` and fetch a model id via `/v1/models`.

### Disable

Unset the flag or remove the Router model entry:

```bash
unset LITELLM_ENABLE_CODEX_AGENT
```

Follow the provider guide for repo conventions: https://docs.litellm.ai/docs/provider_registration/
