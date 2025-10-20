#!/usr/bin/env python3
"""
Demo: spawn multiple codex‑agents and judge the best (SciLLM style).

This starts a tiny in‑process HTTP server that mimics a codex‑agent
OpenAI‑compatible surface (healthz, /v1/models, /v1/chat/completions).
Then it calls scillm.extras.multi_agents.answer_text_multi() and prints the result.

Run:
  python debug/demo_scillm_text_multi.py

Expected output (example):
  best_index=0
  answers[0]=answer from modelA: hello world
  answers[1]=answer from modelB: hello world
"""
from __future__ import annotations

import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class CodexStub(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.endswith("/healthz"):
            self.send_response(200); self.end_headers(); self.wfile.write(b"OK"); return
        if self.path.endswith("/v1/models"):
            body = {"data": [{"id": "modelA"}, {"id": "modelB"}, {"id": "judge"}]}
            self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps(body).encode("utf-8")); return
        self.send_response(404); self.end_headers()

    def do_POST(self):  # noqa: N802
        if not self.path.endswith("/v1/chat/completions"):
            self.send_response(404); self.end_headers(); return
        ln = int(self.headers.get("content-length", "0") or "0")
        raw = self.rfile.read(ln)
        try:
            req = json.loads(raw)
        except Exception:
            req = {}
        msgs = req.get("messages") or []
        sys0 = (msgs[0].get("content") if msgs and isinstance(msgs[0], dict) else "")
        if isinstance(sys0, str) and "You are a strict judge" in sys0:
            # judge request → choose the longest answer
            try:
                payload = json.loads([m for m in msgs if m.get("role") == "user"][0]["content"])  # type: ignore[index]
                answers = payload.get("answers") or []
                best = max(range(len(answers)), key=lambda i: len(answers[i] or "")) if answers else 0
            except Exception:
                best = 0
            out = {"choices": [{"message": {"content": json.dumps({"best_index": best, "rationale_short": "len"})}}]}
        else:
            mid = req.get("model") or "unknown"
            content = f"answer from {mid}: hello world"
            out = {"choices": [{"message": {"content": content}}]}
        self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps(out).encode("utf-8"))


def main() -> None:
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), CodexStub)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    os.environ["CODEX_AGENT_API_BASE"] = f"http://127.0.0.1:{port}"

    # Call SciLLM helper
    from scillm.extras.multi_agents import answer_text_multi

    messages = [{"role": "user", "content": "Say hi"}]
    out = answer_text_multi(
        messages=messages,
        model_ids=["modelA", "modelB"],
        judge_model="judge",
        codex_api_base=os.environ["CODEX_AGENT_API_BASE"],
        judge_via_codex=True,
    )
    # Print concise result
    print(f"best_index={out['best_index']}")
    for i, ans in enumerate(out["answers"]):
        print(f"answers[{i}]={ans}")


if __name__ == "__main__":
    main()

