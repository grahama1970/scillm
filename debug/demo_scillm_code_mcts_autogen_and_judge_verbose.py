#!/usr/bin/env python3
"""
Run Option A (autogen + MCTS + judge) with maximum debug turned on and print
key fields. Useful for smoke‑testing generator connectivity and log surfaces.

Usage:
  SCILLM_ENABLE_CODEWORLD=1 SCILLM_DEBUG=1 \
  CODEX_AGENT_API_BASE=http://127.0.0.1:8089 \
  CODEWORLD_AUTOGEN_HTTP_TIMEOUT_S=120 \
  python debug/demo_scillm_code_mcts_autogen_and_judge_verbose.py
"""
from __future__ import annotations

import os
import socket
import threading
import time
import sys
import uvicorn


def _free_port() -> int:
    import socket as _s
    s = _s.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def main() -> None:
    os.environ.setdefault("SCILLM_ENABLE_CODEWORLD", "1")
    os.environ.setdefault("SCILLM_DEBUG", "1")
    # Start the real bridge served from this repo (litellm/src)
    port = _free_port()
    src_path = os.path.join(os.path.dirname(__file__), "..", "src")
    src_path = os.path.abspath(src_path)
    if src_path not in sys.path:
        sys.path.insert(0, src_path)
    from codeworld.bridge.server import app  # type: ignore
    base = f"http://127.0.0.1:{port}"
    os.environ["CODEWORLD_BASE"] = base
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start(); time.sleep(0.8)

    from scillm.extras import ensure_codex_agent, answer_code_mcts_autogen_and_judge
    ensure_codex_agent()
    items = [{
        "task": "Propose six improved variants of fast inverse square root suitable for a real‑time gaming plugin (C/C++), each trading speed vs accuracy differently and safe for SIMD.",
        "context": {}
    }]
    res = answer_code_mcts_autogen_and_judge(
        items,
        n_variants=6,
        rollouts=12,
        depth=4,
        uct_c=1.3,
        temperature=0.0,
        codeworld_base=base,
        judge_model="gpt-5",
        timeout=120.0,
    )
    cw = res.get("codeworld") or {}
    r0 = (cw.get("results") or [{}])[0]
    print("OK", bool(cw) and bool(r0.get("mcts")))
    print("variants_present", bool(r0.get("code_variants")))
    print("mcts_best_value", (r0.get("mcts") or {}).get("best_value"))
    print("mcts_best_variant_preview", (r0.get("mcts") or {}).get("best_variant", "")[:120])
    print("judge", res.get("judge"))


if __name__ == "__main__":
    main()

