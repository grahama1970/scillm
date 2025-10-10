#!/usr/bin/env python3
"""Check that /root/.codex/auth.json exists inside the codex sidecar container.

Usage:
  python debug/check_codex_auth.py [--container litellm-codex-agent]

Exit codes:
  0 = auth.json present (readable)
  2 = missing or unreadable
  3 = docker not available or container missing
"""
import argparse
import json
import os
import subprocess
import sys


def run(cmd: list[str]) -> tuple[int, str]:
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        return 0, out.decode()
    except subprocess.CalledProcessError as e:
        return e.returncode, e.output.decode()
    except FileNotFoundError:
        return 127, "not found"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", default="litellm-codex-agent")
    args = ap.parse_args()

    if not shutil.which("docker"):
        print("[fail] docker not found on PATH")
        return 3

    rc, out = run(["docker", "ps", "--format", "{{.Names}}"])
    if rc != 0:
        print("[fail] docker ps failed:", out.strip())
        return 3
    names = out.strip().splitlines()
    if args.container not in names:
        print(f"[fail] container {args.container} not found (running: {names})")
        return 3

    rc, home = run(["docker", "exec", args.container, "printenv", "HOME"])
    if rc != 0:
        print("[fail] cannot read HOME in container:", home.strip())
        return 3
    home = home.strip() or "/root"
    auth_path = f"{home}/.codex/auth.json"

    rc, listing = run(["docker", "exec", args.container, "sh", "-lc", f"ls -l {auth_path}"])
    if rc != 0:
        print(f"[fail] missing: {auth_path}")
        return 2

    rc, content = run(["docker", "exec", args.container, "sh", "-lc", f"cat {auth_path}"])
    if rc != 0:
        print(f"[fail] unreadable: {auth_path}")
        return 2
    try:
        json.loads(content)
    except Exception:
        print(f"[fail] invalid JSON in {auth_path}")
        return 2
    print(f"[ok] found {auth_path}")
    return 0


if __name__ == "__main__":
    import shutil
    sys.exit(main())

