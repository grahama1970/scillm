from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

try:
    import httpx  # type: ignore
except Exception:  # pragma: no cover
    httpx = None  # type: ignore

from .litellm_mcp_mini_agent import MCPInvoker, arouter_call


class ResearchPythonInvoker(MCPInvoker):
    """Python-based research tools (Perplexity Ask, Context7 docs) via HTTP.

    Env vars:
      - Perplexity: PPLX_API_KEY (required), PPLX_API_BASE (optional), PPLX_MODEL (optional)
      - Context7:  C7_API_BASE (required), C7_API_KEY (optional)
    """

    def __init__(self) -> None:
        # httpx is only needed for Context7; Perplexity uses litellm Router.
        self._httpx_available = httpx is not None

    async def list_openai_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "research_perplexity",
                    "description": "Ask Perplexity for web-backed answers with citations.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "focus": {"type": "string"},
                            "top_k": {"type": "number"},
                            "max_tokens": {"type": "number"},
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "research_context7_docs",
                    "description": "Fetch docs/snippets from Context7 (internal).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "library": {"type": "string"},
                            "topic": {"type": "string"},
                            "max_snippets": {"type": "number"},
                        },
                        "required": ["library", "topic"],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    async def call_openai_tool(self, openai_tool: Dict[str, Any]) -> str:
        fn = openai_tool.get("function", {}) or {}
        name = fn.get("name")
        args_str = fn.get("arguments", "{}")
        try:
            args = json.loads(args_str) if isinstance(args_str, str) else (args_str or {})
        except Exception:
            args = {}

        if name == "research_perplexity":
            # Call Perplexity via litellm Router, not direct HTTP.
            # Prefer an explicit litellm model name; allow env override.
            model = os.getenv("PPLX_MODEL", "perplexity/sonar-pro")
            query = str(args.get("query", ""))
            try:
                resp = await arouter_call(model=model, messages=[{"role": "user", "content": query}])
                answer = (resp.get("choices", [{}])[0].get("message", {}) or {}).get("content", "") or ""
                # Citations are not available via generic Router call; return a minimal placeholder for compatibility.
                citations = [{"source": "perplexity", "note": "via litellm"}]
                return json.dumps({"ok": True, "answer": answer, "citations": citations}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"ok": False, "error": str(e)[:400]})

        if name == "research_context7_docs":
            base = os.getenv("C7_API_BASE")
            if not base:
                return json.dumps({"ok": False, "error": "C7_API_BASE not set"})
            if not self._httpx_available:
                return json.dumps({"ok": False, "error": "httpx not installed"})
            key = os.getenv("C7_API_KEY")
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            qs = {
                "library": str(args.get("library", "")),
                "topic": str(args.get("topic", "")),
                "n": int(args.get("max_snippets", 5)),
            }
            try:
                async with httpx.AsyncClient(timeout=20.0) as client:  # type: ignore
                    r = await client.get(f"{base.rstrip('/')}/search", params=qs, headers=headers)
                    r.raise_for_status()
                    data = r.json()
                    snippets = data.get("snippets") if isinstance(data, dict) else data
                    if not isinstance(snippets, list):
                        snippets = []
                    return json.dumps({"ok": True, "snippets": snippets}, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"ok": False, "error": str(e)[:400]})

        raise ValueError(f"tool_not_found:{name}")
