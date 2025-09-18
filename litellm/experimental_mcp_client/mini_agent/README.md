# LiteLLM Mini‑Agent (experimental_mcp_client/mini_agent)

A minimal, production‑sane agent loop that builds on the standard LiteLLM Router. It supports tool calling (MCP‑style OpenAI function tools), guarded subprocess execution, self‑repair on failure, and “research on uncertainty” using Python HTTP tools or a Node MCP tools gateway.

This directory contains everything needed for an external code review of the mini‑agent.

## Contents (code under review)

- `litellm_mcp_mini_agent.py` — the core loop (iterate → run tools → observe → repair → repeat). Includes:
  - `AgentConfig` (guardrails and behaviors)
  - `arun_mcp_mini_agent` / `run_mcp_mini_agent`
  - Local tools: `LocalMCPInvoker` with `exec_python` and `exec_shell`
- `call_wrapper.py` — minimal wrapper around `Router.acompletion()` with proper shutdown
- `http_tools_invoker.py` — HTTP adapter for a simple Node tools gateway (`GET /tools`, `POST /invoke`)
- `agent_proxy.py` — tiny FastAPI endpoint (`POST /agent/run`) to drive the agent by URL (optional extra)
- `research_tools.py` — Python research tools (Perplexity Ask, Context7 docs) via httpx
- `__init__.py` — consolidated exports for convenience

Related smokes live under `tests/smoke/`.

## Why a mini‑agent instead of a one‑shot LLM call?

One‑shot calls are great for static prompts. Agents shine when the plan depends on outcomes:

- Run tools and inspect stdout/stderr/rc deterministically
- Self‑repair: if a step fails, generate a compact observation + directive and try again
- Escalate to research when uncertain or after repeated failures
- Keep context tight with bounded history and optional summaries

This mini‑agent does exactly that with minimal surface area and strong defaults.

## Key behaviors

- Deterministic guardrails: `max_iterations`, `max_wallclock_seconds`, per‑iteration tool cap, stagnation detection
- Self‑repair: observations include `rc`, `stdout` and `stderr` tails and add a directive to fix or use research tools
- Research on uncertainty: the agent nudges the model to call research tools when its answer appears unsure
- Safe subprocess tools:
  - `exec_python(code, timeout_s?)` — runs Python in a child process
  - `exec_shell(cmd, timeout_s?)` — allowlist‑guarded shell (default prefixes: `echo`, `python`, `pip`)
- Multiple tool backends:
  - Local (Python): `LocalMCPInvoker`, `ResearchPythonInvoker`
  - HTTP gateway: `HttpToolsInvoker` (Node MCP tools via `/tools` + `/invoke`)
  - HTTP headers supported (e.g., `Authorization`) when pointing at a secured gateway

## Installation (extras)

Optional extras are declared in `pyproject.toml`:

- `mini_agent`: `fastapi`, `uvicorn`, `httpx`, `mcp`
- `images`: `Pillow`, `urlextract`
- `redis_cache`: `redis`

Example:

```bash
pip install -e .[mini_agent,images]
```

## Quick start (in Python)

```python
from litellm.experimental_mcp_client.mini_agent.litellm_mcp_mini_agent import (
    AgentConfig, LocalMCPInvoker, run_mcp_mini_agent
)

cfg = AgentConfig(
    model="gpt-4o-mini",
    max_iterations=5,
    enable_repair=True,
    research_on_unsure=True,
    max_failures_before_research=2,
)

messages = [{"role": "user", "content": "Run python: print('hello') then summarize the result."}]
result = run_mcp_mini_agent(messages, mcp=LocalMCPInvoker(), cfg=cfg)
print(result.final_answer, result.stopped_reason)
```

Self‑contained: The example above requires no Node runtime, no external MCP SDK, and no FastAPI. It runs tools locally (`exec_python`, `exec_shell`) and uses the LiteLLM Router underneath.

## Quick start (HTTP endpoint)

```bash
uvicorn litellm.experimental_mcp_client.mini_agent.agent_proxy:app --host 127.0.0.1 --port 8080
```

Create a run:

```bash
curl -s http://127.0.0.1:8080/agent/run -H 'Content-Type: application/json' -d '{
  "messages": [{"role":"user","content":"Say hi with exec_python then finish."}],
  "model": "gpt-4o-mini",
  "tool_backend": "echo"  // or "http" with tool_http_base_url
}'
```

## Using a Node MCP tools gateway (HTTP) — Optional

If you expose tools over HTTP (`GET /tools` → OpenAI tools array; `POST /invoke` → `{text}`), you can point the agent to it:

```python
from litellm.experimental_mcp_client.mini_agent.http_tools_invoker import HttpToolsInvoker
mcp = HttpToolsInvoker("http://127.0.0.1:8787")
result = run_mcp_mini_agent(messages, mcp=mcp, cfg=cfg)
```

You can wire your MCP servers (e.g., launched via `npx`) behind that gateway. A typical config lives in `~/.codex/config.toml`; the gateway should discover those servers and expose an HTTP facade. This is optional — the mini‑agent is fully usable without Node or external MCP components.

## Research tools (Python)

Enable Perplexity Ask:

- Env: `PPLX_API_KEY` (required), `PPLX_API_BASE` (optional), `PPLX_MODEL` (optional)

Enable Context7 docs search:

- Env: `C7_API_BASE` (required), `C7_API_KEY` (optional)

```python
from litellm.experimental_mcp_client.mini_agent.research_tools import ResearchPythonInvoker
mcp = ResearchPythonInvoker()
result = run_mcp_mini_agent(messages, mcp=mcp, cfg=cfg)
```

## Wiring External MCP Servers (npx) via an HTTP Gateway

You can run one or more MCP servers externally (e.g., via `npx`) and expose them to the mini‑agent through a tiny HTTP gateway. The Python agent only needs a simple contract:

- `GET /tools` → returns an array of OpenAI‑format function tools
- `POST /invoke` → accepts `{ name: string, arguments: string|object }`, returns `{ text?: string, error?: string, data?: object }`

Example shapes

```http
GET /tools
[
  {
    "type": "function",
    "function": {
      "name": "search_docs",
      "description": "Search docs",
      "parameters": {
        "type": "object",
        "properties": { "query": { "type": "string" } },
        "required": ["query"],
        "additionalProperties": false
      }
    }
  }
]

POST /invoke
{ "name": "search_docs", "arguments": { "query": "auth tokens" } }

200 OK
{ "text": "Found 3 relevant docs...", "data": { "hits": [...] } }
```

Point the mini‑agent at the gateway

```python
from litellm.experimental_mcp_client.mini_agent.http_tools_invoker import HttpToolsInvoker
mcp = HttpToolsInvoker("http://127.0.0.1:8787")  # optional: headers={"Authorization": "Bearer ..."}
result = run_mcp_mini_agent(messages, mcp=mcp, cfg=cfg)
```

Using npx + ~/.codex/config.toml (outline)

1) Define your MCP servers (names, commands, and env) in `~/.codex/config.toml`. This file is used by your own Node gateway to discover and (re)start servers. Example outline:

```toml
# ~/.codex/config.toml (example; adapt to your MCP servers)
[servers.perplexity]
command = "npx"
args = ["<your-perplexity-mcp-package>", "--api-key", "${PPLX_API_KEY}"]

[servers.context7]
command = "npx"
args = ["<your-context7-mcp-package>", "--base", "${C7_API_BASE}"]
```

2) Implement a minimal Node gateway (or reuse your existing one) that:
- Loads `~/.codex/config.toml`
- Spawns the configured MCP servers (e.g., with `child_process.spawn`)
- Exposes `GET /tools` (merged view) and `POST /invoke` (route to the right server by `name`)

3) Run the gateway:

```bash
node server.mjs # listens on http://127.0.0.1:8787
```

Security
- If exposing beyond localhost, add a bearer token check on the gateway and pass headers via `HttpToolsInvoker(..., headers={...})`.
- Apply per‑tool allowlists and execution timeouts on the gateway side as well.

## Configuration (AgentConfig)

- `max_iterations`, `max_wallclock_seconds`, `max_tools_per_iter`, `stagnation_window`
- `enable_repair` (default True): append observation + directive to fix/research
- `research_on_unsure`, `max_research_hops`: steer to research when the model is uncertain
- `research_after_failures`, `max_failures_before_research`: escalate to research after repeated failures
- `max_history_messages`: keep last N non‑system messages; preserves latest tool_call pair (recommend ≥ 4)
- `summarize_every`: optional periodic [Context Summary] system message

## Safety & Limits

- Subprocess tools are time‑bounded; shell tool is allowlist‑restricted
- Messages are pruned; observations are truncated to avoid context bloat
- Add auth and 4xx/5xx error semantics to `agent_proxy.py` before exposing publicly

## Tests / Smokes

Run targeted smokes:

```bash
PYTHONPATH=$(pwd) pytest -q tests/smoke/test_mini_agent.py::test_mini_agent_echo_tool_roundtrip
PYTHONPATH=$(pwd) pytest -q tests/smoke/test_mini_agent_repair_loop.py::test_mini_agent_repair_loop_exec_python
PYTHONPATH=$(pwd) pytest -q tests/smoke/test_mini_agent_research_on_unsure.py::test_mini_agent_research_on_unsure
PYTHONPATH=$(pwd) pytest -q tests/smoke/test_exec_shell_tool.py::test_exec_shell_tool_allowlist
PYTHONPATH=$(pwd) pytest -q tests/smoke/test_http_tools_invoker.py::test_http_tools_invoker_monkeypatch
PYTHONPATH=$(pwd) pytest -q tests/smoke/test_research_python_tools.py::test_research_python_tools_basic
```

## Roadmap (MVP‑friendly)

- Optional retries/backoff for research tools
- Router reuse within one agent run to reduce setup overhead
- Better unsure classification (small prompt or a heuristic model)
- Endpoint auth + standardized error schema

---

This README is intentionally concise and focused on usage and purpose—so reviewers can quickly understand how the mini‑agent differs from a one‑shot call and how to exercise it.

## References & Further Reading

- OpenAI Agents SDK & Swarm (lightweight orchestration)
  - https://openai.github.io/openai-agents-python/
  - https://github.com/openai/swarm
- Hugging Face Smolagents (barebones agents that "think in code")
  - https://github.com/huggingface/smolagents
- LangChain tool‑calling agents (background reference)
  - https://python.langchain.com/docs/concepts/tool_calling/
- Practical agent best practices
  - Vellum: The ultimate LLM agent build guide
  - Anthropic: Building effective agents
  - OpenAI: A practical guide to building agents

See also: `local/mini_agent/docs/reports/mini_agent_reassessment.md` for a concise external reassessment aligned with the Happy Path Guide.
