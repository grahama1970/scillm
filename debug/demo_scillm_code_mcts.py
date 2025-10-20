#!/usr/bin/env python3
"""
Demo: apply MCTS for code tasks via the SciLLM CodeWorld provider.

This starts a tiny FastAPI stub that mimics the CodeWorld bridge /bridge/complete
endpoint and returns an MCTS-like payload. Then it calls
scillm.extras.multi_agents.answer_code_mcts() and prints the best_variant.

Run:
  python debug/demo_scillm_code_mcts.py

Expected output (example):
  status=200
  best_variant=a
"""
from __future__ import annotations

import json
import os
import socket
import threading
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def start_codeworld_stub(port: int) -> threading.Thread:
    app = FastAPI()

    @app.post("/bridge/complete")
    async def complete(body: Dict[str, Any]):  # type: ignore[override]
        # Minimal echo + fake mcts fields
        items = body.get("items") or [{"task": "t", "context": {"code_variants": {"a": "", "b": ""}}}]
        # Pick the first key as best to keep deterministic
        variants = (items[0].get("context") or {}).get("code_variants") or {"a": "", "b": ""}
        best = list(variants.keys())[0]
        result = {
            "summary": {"items": len(items), "succeeded": 1, "failed": 0},
            "results": [
                {
                    "mcts": {
                        "best_variant": best,
                        "best_value": 0.9,
                        "visits": {best: 10},
                        "explored_nodes": 2,
                        "rollouts": int((body.get("provider") or {}).get("args", {}).get("rollouts", 24)),
                        "depth": int((body.get("provider") or {}).get("args", {}).get("depth", 5)),
                        "uct_c": float((body.get("provider") or {}).get("args", {}).get("uct_c", 1.25)),
                        "seed": int((body.get("provider") or {}).get("args", {}).get("seed", 0)),
                    }
                }
            ],
            "run_manifest": {"mcts_stats": {"best_variant": best}},
        }
        return JSONResponse(result)

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    return t


def main() -> None:
    port = _free_port()
    start_codeworld_stub(port)
    base = f"http://127.0.0.1:{port}"
    # IMPORTANT: enable provider before importing scillm/litellm
    os.environ["SCILLM_ENABLE_CODEWORLD"] = "1"
    os.environ["CODEWORLD_BASE"] = base

    from scillm.extras.multi_agents import answer_code_mcts

    items = [{"task": "t", "context": {"code_variants": {"a": "def solve(ctx): return 1", "b": "def solve(ctx): return 2"}}}]
    resp = answer_code_mcts(items, codeworld_base=base, rollouts=24, depth=5, uct_c=1.25)

    # Response is an OpenAI-shaped ModelResponse; extract attached payload
    payload = getattr(resp, "additional_kwargs", {}).get("codeworld") or {}
    res0 = (payload.get("results") or [{}])[0]
    mcts = res0.get("mcts") or {}
    best = mcts.get("best_variant")
    print("status=200")
    print(f"best_variant={best}")


if __name__ == "__main__":
    main()
