#!/usr/bin/env python3
"""Watchdog for CodeWorld and Lean4 bridges.

Checks /healthz and restarts the service if unhealthy/down.

Usage:
  python scripts/watch_bridges.py               # one-shot check
  python scripts/watch_bridges.py --loop 10     # check every 10s

Env:
  STACK_COMPOSE=deploy/docker/compose.scillm.stack.yml
  CODEWORLD_URL=http://127.0.0.1:8887/healthz
  LEAN4_URL=http://127.0.0.1:8787/healthz
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request


def _get(url: str, timeout: float = 3.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # nosec B310
            return r.getcode(), r.read()
    except Exception as e:
        return 0, str(e).encode()


def _healthy(data: bytes) -> bool:
    try:
        j = json.loads(data.decode("utf-8", errors="ignore"))
        return bool(j.get("ok"))
    except Exception:
        return False


def _restart(service: str, compose: str) -> None:
    cmd = ["docker", "compose", "-f", compose, "up", "-d", service]
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError as e:
        print(f"[watch] restart failed for {service}: rc={e.returncode}", file=sys.stderr)


def check_once() -> int:
    compose = os.getenv("STACK_COMPOSE", "deploy/docker/compose.scillm.stack.yml")
    ok = True
    items = [
        ("codeworld-bridge", os.getenv("CODEWORLD_URL", "http://127.0.0.1:8887/healthz")),
        ("lean4-bridge", os.getenv("LEAN4_URL", "http://127.0.0.1:8787/healthz")),
    ]
    for svc, url in items:
        rc, body = _get(url, timeout=4.0)
        if rc != 200 or not _healthy(body or b""):
            print(f"[watch] unhealthy: {svc} (rc={rc}). restarting…")
            _restart(svc, compose)
            ok = False
        else:
            print(f"[watch] healthy: {svc}")
    return 0 if ok else 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="seconds between checks (0=one-shot)")
    args = ap.parse_args()
    if args.loop <= 0:
        return check_once()
    try:
        while True:
            check_once()
            time.sleep(args.loop)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

