from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

try:  # soft dependency for tests to monkeypatch
    import httpx  # type: ignore
except Exception as e:  # pragma: no cover - import error path
    httpx = None  # type: ignore
    _IMPORT_ERR = e
else:
    _IMPORT_ERR = None

from .litellm_mcp_mini_agent import MCPInvoker


class HttpToolsInvoker(MCPInvoker):
    """Minimal HTTP adapter for a tools gateway exposing:

    - GET  {base_url}/tools   -> List[OpenAI function tool]
    - POST {base_url}/invoke  -> { name, arguments } -> { text }
    """

    def __init__(self, base_url: str, *, timeout: float = 15.0, headers: Optional[Dict[str, str]] = None) -> None:
        if httpx is None:
            raise ImportError(
                "httpx is required for HttpToolsInvoker. Install with: pip install httpx"
            ) from _IMPORT_ERR
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = dict(headers or {})

    async def list_openai_tools(self) -> List[Dict[str, Any]]:
        async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:  # type: ignore[attr-defined]
            r = await client.get(f"{self.base_url}/tools")
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                raise ValueError("Invalid /tools response; expected a list")
            return data  # assume OpenAI function tools

    async def call_openai_tool(self, openai_tool: Dict[str, Any]) -> str:
        fn = openai_tool.get("function", {}) or {}
        name = fn.get("name")
        args = fn.get("arguments", "{}")
        if not isinstance(args, str):
            args = json.dumps(args)

        payload = {"name": name, "arguments": args}
        async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:  # type: ignore[attr-defined]
            r = await client.post(f"{self.base_url}/invoke", json=payload)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid /invoke response; expected an object")
            return str(data.get("text", ""))
