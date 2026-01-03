#!/usr/bin/env python3
from __future__ import annotations

"""
scillm_core_check: Local CI-style smoke that a project agent can run to
quickly see what is broken. Produces a single JSON report and a non-zero
exit code when core checks fail.

Checks (gated by env):
  - import/preflight pytest subset (no secrets required)
  - Chutes doctor (models + strict JSON chat)
  - VLM sanity (curl + helper) when CHUTES_VLM_MODEL is set

Usage:
  source .venv/bin/activate
  set -a; [ -f .env ] && source .env; set +a
  PYTHONPATH=$(pwd)/src python scripts/ci/scillm_core_check.py
"""

import json
import os
import shlex
import subprocess
import sys
from typing import Any, Dict


def _run(cmd: str, env: dict | None = None, timeout: int = 180) -> dict:
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=env, timeout=timeout)
    return {
        "cmd": cmd,
        "rc": p.returncode,
        "out": p.stdout.strip(),
        "err": p.stderr.strip(),
    }


def main() -> None:
    base = (os.getenv("CHUTES_API_BASE") or "").strip()
    key = (os.getenv("CHUTES_API_KEY") or "").strip()
    vlm = (os.getenv("CHUTES_VLM_MODEL") or "").strip()
    py_path = os.environ.get("PYTHONPATH") or (os.getcwd() + "/src")
    env = dict(os.environ)
    env["PYTHONPATH"] = py_path

    report: Dict[str, Any] = {"env": {"base": base, "has_key": bool(key), "has_vlm": bool(vlm)}}

    # 1) Minimal pytest subset (no secrets required)
    rep_tests = _run("pytest -q tests/test_import_no_extras.py tests/test_preflight.py -q", env)
    report["pytest_subset"] = {"rc": rep_tests["rc"], "ok": rep_tests["rc"] == 0}

    # 2) Chutes doctor (models + strict JSON chat)
    if base and key:
        rep_doc = _run("python scripts/tools/scillm_quick_doctor.py", env)
        ok = False
        content = rep_doc["out"].splitlines()[-1:] or [""]
        try:
            last = json.loads(content[0]) if content[0].startswith("{") else {}
            ok = bool(last.get("ok"))
        except Exception:
            ok = False
        report["doctor"] = {"rc": rep_doc["rc"], "ok": ok, "tail": content[0]}
    else:
        report["doctor"] = {"skipped": True, "reason": "set CHUTES_API_BASE and CHUTES_API_KEY"}

    # 3) VLM sanity (optional)
    if base and key and vlm:
        rep_vlm = _run(f"python scripts/tools/scillm_multimodal_sanity.py --model {shlex.quote(vlm)} --run-curl", env)
        vlm_ok = rep_vlm["out"].strip().endswith('{"ok": true}')
        report["vlm"] = {"rc": rep_vlm["rc"], "ok": vlm_ok}
    else:
        report["vlm"] = {"skipped": True, "reason": "set CHUTES_VLM_MODEL to test vision"}

    # Overall
    overall = True
    overall &= report["pytest_subset"]["ok"]
    if not report["doctor"].get("skipped"):
        overall &= report["doctor"].get("ok", False)
    if not report["vlm"].get("skipped"):
        overall &= report["vlm"].get("ok", False)
    report["ok"] = bool(overall)

    print(json.dumps(report, indent=2))
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    main()

