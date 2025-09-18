---
id: mini-agent
title: Experimental Mini‑Agent (In‑Code)
sidebar_position: 10
---

import Tabs from '@theme/Tabs';
import TabItem from '@theme/TabItem';

> Status: Experimental (opt‑in). Off by default. No Proxy changes.

The mini‑agent is a small, **in‑code** loop for iterative tool use (iterate → tool → observe → repair → repeat). It’s self‑contained and designed for MVP‑level reliability: guardrails, safe subprocess tools, and bounded context — without adopting a large framework.

Key features
- Deterministic guardrails: `max_iterations`, wall‑clock cap, stagnation handling
- Self‑repair: compact observations (rc/stdout/stderr tails) + directive to fix or research
- Context control: preserves latest tool‑call pair; truncation budget to cap context
- Local tools (no deps): `exec_python`, `exec_shell` (allowlist + timeouts)
- Optional: HTTP tools gateway (no FastMCP) and Python research tools (env‑gated)

Where
- Code: `litellm/experimental_mcp_client/mini_agent`
- README: `litellm/experimental_mcp_client/mini_agent/README.md`

Quick start

```python title="in-code"
from litellm.experimental_mcp_client.mini_agent.litellm_mcp_mini_agent import (
  AgentConfig, LocalMCPInvoker, run_mcp_mini_agent
)

cfg = AgentConfig(model="gpt-4o-mini", max_iterations=5, enable_repair=True)
messages = [{"role": "user", "content": "Run python: print('hello') then finish."}]
res = run_mcp_mini_agent(messages, mcp=LocalMCPInvoker(), cfg=cfg)
print(res.final_answer)
```

Optional tools backends

<Tabs>
<TabItem value="http" label="HTTP Tools Gateway">

```python title="point to a simple /tools + /invoke gateway"
from litellm.experimental_mcp_client.mini_agent.http_tools_invoker import HttpToolsInvoker
mcp = HttpToolsInvoker("http://127.0.0.1:8787")
res = run_mcp_mini_agent(messages, mcp=mcp, cfg=cfg)
```

> Note: The repo includes a tiny Node gateway example under `mini_agent/node_tools_gateway/` for local dev only. It is **not** required and is excluded from packaging/CI. Tests skip if `node` is not installed.

</TabItem>
<TabItem value="research" label="Python Research Tools">

```python title="Perplexity/Context7 (env-gated)"
from litellm.experimental_mcp_client.mini_agent.research_tools import ResearchPythonInvoker
# export PPLX_API_KEY=... or set C7_API_BASE=...
mcp = ResearchPythonInvoker()
res = run_mcp_mini_agent(messages, mcp=mcp, cfg=cfg)
```

</TabItem>
</Tabs>

Smokes (dev)

```bash
PYTHONPATH=$(pwd) pytest -q tests/smoke/test_mini_agent.py::test_mini_agent_echo_tool_roundtrip
PYTHONPATH=$(pwd) pytest -q tests/smoke/test_mini_agent_repair_loop.py::test_mini_agent_repair_loop_exec_python
PYTHONPATH=$(pwd) pytest -q tests/smoke/test_http_tools_invoker.py::test_http_tools_invoker_monkeypatch
```

Scope / Non-goals
- Not a full orchestrator; does not change the Proxy or defaults
- No FastMCP; Node gateway is optional and for local dev only
- Focused on **Happy Path** principles: minimal surface, paved road, deterministic guardrails

