#!/usr/bin/env python3
"""Lean4 batch demo (live E2E).

Runs `python -m lean4_prover.cli_mini batch` against two sample requirements
and prints a compact summary. This is a live scenario: it expects your Lean4
environment to be configured for LLM/Docker, and may fail if not available.

Environment:
- `LEAN4_REPO` (required): absolute path to the Lean4 repo (expects `src/` within).
- `LEAN4_BATCH_FLAGS` to pass additional flags (e.g., `--best-of --max-refinements 2`).
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

LEAN4_REPO = Path(os.getenv("CERTAINLY_REPO", os.getenv("LEAN4_REPO", "/home/graham/workspace/experiments/lean4"))).resolve()
if not LEAN4_REPO.exists():
    print("Skipping Lean4 batch scenario (set LEAN4_REPO to your Lean4 repo).")
    sys.exit(0)

PYTHON = sys.executable
# Live by default; keep flags empty unless user overrides via env
DEFAULT_FLAGS = os.getenv("LEAN4_BATCH_FLAGS", "")
FLAGS = DEFAULT_FLAGS


def main() -> None:
    items = [
        {"requirement_text": "0 + n = n", "context": {"section_id": "S1"}},
        {"requirement_text": "m + n = n + m", "context": {"section_id": "S2"}},
    ]

    with tempfile.NamedTemporaryFile("w", suffix="_lean4_in.json", delete=False) as fin:
        json.dump(items, fin)
        fin.flush()
        in_path = Path(fin.name)

    with tempfile.NamedTemporaryFile("w", suffix="_lean4_out.json", delete=False) as fout:
        out_path = Path(fout.name)

    try:
        cmd = [
            PYTHON,
            "-m",
            "lean4_prover.cli_mini",
            "batch",
            "--input-file",
            str(in_path),
            "--output-file",
            str(out_path),
        ] + shlex.split(FLAGS)

        # Ensure Lean4 package is importable
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{LEAN4_REPO / 'src'}:{env.get('PYTHONPATH','')}"

        print(
            json.dumps(
                {
                    "example_request": {
                        "command": "python -m lean4_prover.cli_mini batch",
                        "flags": shlex.split(FLAGS) if FLAGS else [],
                        "items": items,
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
                        "recommendation": "Ensure Lean4 env is live (Docker/model), or pass appropriate flags via LEAN4_BATCH_FLAGS.",
                    },
                    indent=2,
                )
            )
            raise SystemExit(1)

        data = json.loads(out_path.read_text())
        stats = data.get("statistics", {}) if isinstance(data, dict) else {}
        proved = stats.get("successful_proofs")
        failed = stats.get("failed_proofs")
        unproved = stats.get("unproved")
        print(json.dumps({"example_response": data, "summary": {"proved": proved, "failed": failed, "unproved": unproved}}, indent=2))
    finally:
        try:
            in_path.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            out_path.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
