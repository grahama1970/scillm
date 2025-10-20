#!/usr/bin/env python3
"""
Demo (live): run the real CodeWorld bridge app from this repo and call it via scillm.

This starts uvicorn with src/codeworld/bridge/server.py:app on a free port in-process,
then performs a SciLLM completion(model='codeworld/mcts', ...) with provided variants.

No external gateways are required (no autogen); this exercises the real bridge code.
"""
from __future__ import annotations

import os
import socket
import threading
import time

import uvicorn
import sys


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def main() -> None:
    port = _free_port()
    # Gate provider before importing scillm
    os.environ["SCILLM_ENABLE_CODEWORLD"] = "1"
    os.environ["SCILLM_DEBUG"] = os.getenv("SCILLM_DEBUG", "1")
    base = f"http://127.0.0.1:{port}"
    os.environ["CODEWORLD_BASE"] = base

    # Ensure repo src/ is importable, then import the real app object
    src_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
    if src_path not in sys.path:
        sys.path.insert(0, src_path)
    from codeworld.bridge.server import app  # type: ignore

    # Start the real bridge app
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    # Give it a moment
    time.sleep(0.8)

    from scillm import completion

    items = [{
        "task": "t",
        "context": {
            "code_variants": {
                "a": "def solve(ctx):\n    return 1",
                "b": "def solve(ctx):\n    return 2",
            }
        }
    }]
    resp = completion(model="codeworld/mcts", custom_llm_provider="codeworld", messages=[], items=items, api_base=base)
    payload = getattr(resp, "additional_kwargs", {}).get("codeworld") or {}
    res0 = (payload.get("results") or [{}])[0]
    mcts = res0.get("mcts") or {}
    print("LIVE_OK", bool(mcts))
    print("BEST", mcts.get("best_variant"))


if __name__ == "__main__":
    main()
