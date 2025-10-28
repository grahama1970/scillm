#!/usr/bin/env python3
"""
Container + HTTP preflight for SciLLM sidecars and bridges.

Checks (best‑effort, non‑fatal):
- docker compose ps for known compose files (if present)
- HTTP health endpoints (source of truth)

Exit code:
- 0: script ran; see JSON for per‑service status
- non‑zero only on unexpected internal error (e.g., Python exception)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib import request as rq


@dataclass
class Service:
    name: str
    health_url: str
    compose_file: Optional[str] = None
    compose_services: Optional[List[str]] = None


SERVICES: List[Service] = [
    Service(
        name="codex-sidecar",
        health_url=os.getenv("CODEX_AGENT_API_BASE", "http://127.0.0.1:8089").rstrip("/") + "/healthz",
        compose_file="local/docker/compose.agents.yml",
        compose_services=["codex-sidecar"],
    ),
    Service(
        name="mini-agent",
        health_url=os.getenv("MINI_AGENT_BASE", "http://127.0.0.1:8788").rstrip("/") + "/ready",
        compose_file="local/docker/compose.agents.yml",
        compose_services=["mini-agent"],
    ),
    Service(
        name="codeworld-bridge",
        health_url=os.getenv("CODEWORLD_BASE", "http://127.0.0.1:8887").rstrip("/") + "/healthz",
        compose_file="local/docker/compose.codeworld.bridge.yml",
        compose_services=["codeworld-bridge"],
    ),
    Service(
        name="certainly-bridge",
        health_url=os.getenv("CERTAINLY_BRIDGE_BASE", "http://127.0.0.1:8791").rstrip("/") + "/healthz",
        compose_file="local/docker/compose.certainly.bridge.yml",
        compose_services=["certainly_bridge"],
    ),
]


def _run(cmd: List[str]) -> Dict[str, Any]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return {"rc": p.returncode, "out": p.stdout.strip(), "err": p.stderr.strip()}
    except Exception as e:  # noqa: BLE001
        return {"rc": 127, "err": str(e)}


def _http_get(url: str, timeout: float = 4.0) -> Dict[str, Any]:
    try:
        with rq.urlopen(url, timeout=timeout) as resp:  # nosec B310
            body = resp.read().decode("utf-8", "ignore")
            return {"status": int(getattr(resp, "status", 0) or 0), "body_preview": body[:200]}
    except Exception as e:  # noqa: BLE001
        return {"status": 0, "error": str(e)}


def check_service(s: Service) -> Dict[str, Any]:
    res: Dict[str, Any] = {"name": s.name, "health_url": s.health_url}
    # Compose PS (optional)
    if s.compose_file and os.path.exists(s.compose_file):
        res["compose_file"] = s.compose_file
        ps = _run(["docker", "compose", "-f", s.compose_file, "ps"])
        res["compose_ps"] = ps
    else:
        if s.compose_file:
            res["compose_file_missing"] = True
    # HTTP
    res["http"] = _http_get(s.health_url)
    return res


def main() -> int:
    out = {"services": [check_service(s) for s in SERVICES]}
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

