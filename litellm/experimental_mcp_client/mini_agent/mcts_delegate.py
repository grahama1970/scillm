"""
Mini-agent MCP-style tool delegating to CodeWorld MCTS (optional).

Disabled unless MINI_AGENT_ENABLE_MCTS=1.

This module provides a simple function to call the CodeWorld bridge with
provider.args.strategy="mcts". It intentionally avoids auto-registration
to keep the mini-agent core deterministic by default.
"""

from __future__ import annotations

import os
from typing import Any, Dict

try:
    import httpx  # type: ignore
except Exception:  # pragma: no cover - optional dep
    httpx = None  # type: ignore


def is_enabled() -> bool:
    return os.getenv("MINI_AGENT_ENABLE_MCTS", "0") == "1"


def plan_with_mcts(task: str, code_variants: Dict[str, str], *, api_base: str | None = None) -> Dict[str, Any]:
    if not is_enabled():
        return {"ok": False, "error": "MCTS tool disabled (set MINI_AGENT_ENABLE_MCTS=1)"}
    if httpx is None:
        return {"ok": False, "error": "httpx not available"}
    base = (api_base or os.getenv("CODEWORLD_BASE", "http://127.0.0.1:8887")).rstrip("/")
    payload = {
        "messages": [{"role": "user", "content": "Plan with MCTS"}],
        "items": [{"task": task, "context": {"code_variants": code_variants}}],
        "provider": {
            "name": "codeworld",
            "args": {
                "strategy": "mcts",
                "strategy_config": {"name": "mcts", "rollouts": 32, "depth": 6, "uct_c": 1.3},
            },
        },
        "options": {"session_id": "mini-agent-mcts", "track_id": "loop-1", "max_seconds": 10},
    }
    try:
        r = httpx.post(f"{base}/bridge/complete", json=payload, timeout=30.0)  # type: ignore
        if r.status_code != 200:
            return {"ok": False, "status": r.status_code, "error": (r.text or "")[:200]}
        data = r.json()
        mcts = ((data.get("results") or [{}])[0] or {}).get("mcts")
        return {"ok": True, "mcts": mcts, "run_manifest": data.get("run_manifest")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}

