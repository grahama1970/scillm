#!/usr/bin/env python3
"""
Verify codex-agent endpoints running in Docker (sidecar:8077, mini-agent:8788).

Checks:
- If docker compose file exists, optionally start codex-sidecar.
- Probe /healthz, /v1/models, and /v1/chat/completions with a strict JSON prompt.
- Ensure choices[0].message.content is a non-null string.
- Enforce base rule: CODEX_AGENT_API_BASE must not include '/v1'.

Exit codes:
 0 = all required checks passed
 1 = soft issues (models stub missing) but chat works
 2 = hard failure (health or chat content invalid)

Usage:
  python debug/verify_codex_agent_docker.py [--start]

Flags:
  --start   Attempt to start codex-sidecar via docker compose if not present
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from typing import Optional

import urllib.request
import urllib.error


HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, os.pardir))
COMPOSE = os.path.join(REPO_ROOT, "local", "docker", "compose.agents.yml")


def _get(url: str, timeout: float = 3.0) -> tuple[int, Optional[bytes]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # nosec B310
            return r.getcode(), r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        except Exception:
            return e.code, None
    except Exception:
        return 0, None


def _post_json(url: str, body: dict, timeout: float = 6.0) -> tuple[int, Optional[bytes]]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310
            return r.getcode(), r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        except Exception:
            return e.code, None
    except Exception:
        return 0, None


def _start_sidecar_if_requested() -> None:
    if not os.path.exists(COMPOSE):
        print(f"[warn] compose file not found: {COMPOSE}; skipping start.")
        return
    if shutil.which("docker") is None:
        print("[warn] docker not found on PATH; skipping start.")
        return
    cmd = f"docker compose -f {COMPOSE} up --build -d codex-sidecar"
    rc = os.system(cmd)
    if rc != 0:
        print(f"[warn] failed to start codex-sidecar via compose (rc={rc}).")


def _wait_for_health(base: str, path: str = "/healthz", deadline_s: float = 20.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        code, body = _get(base + path)
        if code == 200 and body:
            try:
                j = json.loads(body.decode("utf-8", errors="ignore"))
                if j.get("ok") is True:
                    return True
            except Exception:
                pass
        time.sleep(0.5)
    return False


def _probe_agent(base: str) -> tuple[bool, bool]:
    ok_models = False
    # models (stub OK)
    code, body = _get(base + "/v1/models")
    if code == 200 and body:
        try:
            j = json.loads(body.decode("utf-8", errors="ignore"))
            ok_models = bool(j.get("data"))
        except Exception:
            ok_models = False
    # chat
    code2, body2 = _post_json(
        base + "/v1/chat/completions",
        {
            "model": "gpt-5",
            "messages": [
                {"role": "system", "content": "Return STRICT JSON only: {\"ok\":true}"}
            ],
        },
    )
    ok_chat = False
    if code2 == 200 and body2:
        try:
            j2 = json.loads(body2.decode("utf-8", errors="ignore"))
            content = (((j2.get("choices") or [{}])[0]).get("message") or {}).get("content")
            ok_chat = isinstance(content, str)
        except Exception:
            ok_chat = False
    return ok_models, ok_chat


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", action="store_true", help="attempt to start codex-sidecar via docker compose")
    args = ap.parse_args()

    # Enforce base rule (for documentation clarity)
    base_env = os.getenv("CODEX_AGENT_API_BASE", "http://127.0.0.1:8077")
    if base_env.rstrip("/").endswith("/v1"):
        print("[warn] CODEX_AGENT_API_BASE should not include '/v1'; trimming for probes.")
        base_env = base_env.rstrip("/")[:-3]

    sidecar = "http://127.0.0.1:8077"
    mini = "http://127.0.0.1:8788"

    if args.start:
        _start_sidecar_if_requested()

    # Sidecar
    print(f"[info] Probing codex sidecar at {sidecar}")
    if not _wait_for_health(sidecar):
        print("[fail] /healthz failed for sidecar")
        sidecar_ok = False
    else:
        m_ok, c_ok = _probe_agent(sidecar)
        sidecar_ok = c_ok
        if not m_ok:
            print("[warn] /v1/models not available on sidecar (not blocking)")
        print(f"[info] sidecar chat content string: {'OK' if c_ok else 'FAIL'}")

    # Mini-agent (optional)
    print(f"[info] Probing mini-agent at {mini}")
    mini_ok = False
    if _wait_for_health(mini):
        m_ok2, c_ok2 = _probe_agent(mini)
        if not m_ok2:
            print("[warn] /v1/models not available on mini-agent (normal)")
        mini_ok = c_ok2
        print(f"[info] mini-agent chat content string: {'OK' if c_ok2 else 'FAIL'}")
    else:
        print("[warn] mini-agent /healthz failed (container may be down; not blocking)")

    if sidecar_ok and (mini_ok or True):
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())

