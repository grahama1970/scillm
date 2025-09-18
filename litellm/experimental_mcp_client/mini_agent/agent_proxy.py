from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .litellm_mcp_mini_agent import AgentConfig, EchoMCP, arun_mcp_mini_agent

try:
    from .http_tools_invoker import HttpToolsInvoker
except Exception:  # pragma: no cover - soft dependency in tests
    HttpToolsInvoker = None  # type: ignore


app = FastAPI(title="LiteLLM MCP Mini-Agent")


class AgentRunReq(BaseModel):
    messages: List[Dict[str, Any]]
    model: str
    max_iterations: int = 8
    max_wallclock_seconds: int = 60
    max_tools_per_iter: int = 4
    stagnation_window: int = 3
    temperature: float = 0.2
    tool_allowlist: Optional[List[str]] = None
    tool_choice: str = "auto"
    tool_concurrency: int = 1
    tool_backend: str = "local"  # "local" (default), "echo", "http", "research"
    tool_http_base_url: Optional[str] = None
    tool_http_headers: Optional[Dict[str, str]] = None
    # litellm passthrough
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None


@app.get("/healthz")
async def healthz():  # pragma: no cover - trivial
    return {"ok": True}


@app.post("/agent/run")
async def agent_run(req: AgentRunReq):
    cfg = AgentConfig(
        model=req.model,
        max_iterations=req.max_iterations,
        max_wallclock_seconds=req.max_wallclock_seconds,
        max_tools_per_iter=req.max_tools_per_iter,
        stagnation_window=req.stagnation_window,
        temperature=req.temperature,
        tool_allowlist=req.tool_allowlist,
        tool_choice=req.tool_choice,
        tool_concurrency=req.tool_concurrency,
    )

    llm_kwargs: Dict[str, Any] = {}
    if req.api_key:
        llm_kwargs["api_key"] = req.api_key
    if req.api_base:
        llm_kwargs["api_base"] = req.api_base
    if req.extra:
        llm_kwargs.update(req.extra)

    if req.tool_backend == "http":
        if HttpToolsInvoker is None:
            raise HTTPException(status_code=400, detail="httpx required for tool_backend=http")
        if not req.tool_http_base_url:
            raise HTTPException(status_code=400, detail="tool_http_base_url required for tool_backend=http")
        mcp = HttpToolsInvoker(req.tool_http_base_url, headers=req.tool_http_headers)  # type: ignore[call-arg]
    elif req.tool_backend == "research":
        try:
            from .research_tools import ResearchPythonInvoker  # type: ignore
        except Exception as e:  # pragma: no cover
            raise HTTPException(status_code=500, detail=f"research backend not available: {e}")
        mcp = ResearchPythonInvoker()  # type: ignore
    elif req.tool_backend == "echo":
        mcp = EchoMCP()
    else:
        # default self-contained
        from .litellm_mcp_mini_agent import LocalMCPInvoker  # circular-safe
        mcp = LocalMCPInvoker()

    out = await arun_mcp_mini_agent(req.messages, mcp=mcp, cfg=cfg, **llm_kwargs)
    return {
        "final_answer": out.final_answer,
        "stopped_reason": out.stopped_reason,
        "iterations": [i.__dict__ for i in out.iterations],
    }
