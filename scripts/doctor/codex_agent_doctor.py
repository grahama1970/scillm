#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import json
import time
import httpx
import argparse


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=os.getenv("CODEX_AGENT_API_BASE", "http://127.0.0.1:8089"))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("CODEX_DOCTOR_TIMEOUT_S", "10") or "10"))
    args = parser.parse_args()

    base = (args.base or "http://127.0.0.1:8089").rstrip("/")
    if base.endswith("/v1"):
        print("[doctor] note: stripping trailing /v1 from base")
        base = base[:-3]
    model = os.getenv("CODEX_AGENT_MODEL")
    errs = 0
    with httpx.Client(timeout=args.timeout) as c:
        # /healthz
        try:
            r = c.get(base + "/healthz")
            ok = r.status_code == 200
            print(f"[doctor] healthz: {r.status_code} {'OK' if ok else 'FAIL'}")
            if not ok:
                errs += 1
        except Exception as e:  # noqa: BLE001
            print(f"[doctor] healthz: EXC {e}")
            errs += 1

        # /v1/models
        try:
            r = c.get(base + "/v1/models")
            ok = r.status_code == 200
            ids: list[str] = []
            if ok:
                data = r.json()
                ids = [d.get("id") for d in (data.get("data") or []) if isinstance(d, dict)]
                print("[doctor] models:", ", ".join(ids) or "<none>")
                if not model and ids:
                    model = ids[0]
                if model and model not in ids:
                    print(f"[doctor] note: requested model '{model}' not in /v1/models; prefer a returned id")
            else:
                print(f"[doctor] models: FAIL {r.status_code}")
                errs += 1
        except Exception as e:  # noqa: BLE001
            print(f"[doctor] models: EXC {e}")
            errs += 1

        # chat ping (reasoning high; optional)
        if model:
            try:
                body = {
                    "model": model,
                    "reasoning": {"effort": "high"},
                    "messages": [{"role": "user", "content": "ping"}],
                }
                r = c.post(base + "/v1/chat/completions", json=body)
                ok = r.status_code == 200
                content = None
                if ok:
                    content = r.json()["choices"][0]["message"]["content"]
                    print("[doctor] chat: OK —", (content or "").strip()[:60])
                else:
                    print(f"[doctor] chat: FAIL {r.status_code}")
                    errs += 1
            except Exception as e:  # noqa: BLE001
                print(f"[doctor] chat: EXC {e}")
                errs += 1
        else:
            print("[doctor] chat: SKIP — no model id")

    sys.exit(0 if errs == 0 else 2)


if __name__ == "__main__":
    main()
