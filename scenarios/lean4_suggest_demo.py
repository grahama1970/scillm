#!/usr/bin/env python3
"""Lean4 suggest demo (live E2E).

Runs `python -m lean4_prover.cli_mini run` with a single requirement.
This is a live scenario and expects your Lean4 environment to be configured for
LLM/Docker. It may fail if providers are unavailable.

Environment:
- `LEAN4_REPO` (required): absolute path to the Lean4 repo (expects `src/` within).
- Override text via `LEAN4_REQUIREMENT_TEXT`.
- Optional flags via `LEAN4_SUGGEST_FLAGS` (e.g., `--best-of --max-refinements 2`).
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

LEAN4_REPO = Path(os.getenv("CERTAINLY_REPO", os.getenv("LEAN4_REPO", "/home/graham/workspace/experiments/lean4"))).resolve()
if not LEAN4_REPO.exists():
    print("Skipping Lean4 suggest scenario (set LEAN4_REPO to your Lean4 repo).")
    sys.exit(0)

PYTHON = sys.executable
TEXT = os.getenv(
    "LEAN4_REQUIREMENT_TEXT",
    "The sum of two even natural numbers is even.",
)
DEFAULT_FLAGS = os.getenv("LEAN4_SUGGEST_FLAGS", "")
FLAGS = DEFAULT_FLAGS


def main() -> None:
    cmd = [
        PYTHON,
        "-m",
        "lean4_prover.cli_mini",
        "run",
        TEXT,
    ] + shlex.split(FLAGS)

    env = os.environ.copy()
    env["PYTHONPATH"] = f"{LEAN4_REPO / 'src'}:{env.get('PYTHONPATH','')}"

    print(
        json.dumps(
            {
                "example_request": {
                    "command": "python -m lean4_prover.cli_mini run",
                    "text": TEXT,
                    "flags": shlex.split(FLAGS) if FLAGS else [],
                }
            },
            indent=2,
        )
    )
    proc = subprocess.run(cmd, cwd=str(LEAN4_REPO), env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        print(
            json.dumps(
                {
                    "error": {
                        "returncode": proc.returncode,
                        "stdout": proc.stdout[-2000:],
                        "stderr": proc.stderr[-2000:],
                    },
                    "recommendation": "Verify providers and container are available, then retry with stronger model/flags via LEAN4_SUGGEST_FLAGS.",
                },
                indent=2,
            )
        )
        raise SystemExit(1)
    print(proc.stdout[-4000:] or "{}")


if __name__ == "__main__":
    main()
