import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(
    not Path(os.getenv("LEAN4_REPO", "/home/graham/workspace/experiments/lean4")).exists(),
    reason="LEAN4_REPO not available",
)
def test_cli_single_deterministic() -> None:
    repo = Path(os.getenv("LEAN4_REPO", "/home/graham/workspace/experiments/lean4")).resolve()
    text = "The sum of two even natural numbers is even."
    cmd = [
        sys.executable,
        "-m",
        "lean4_prover.cli_mini",
        "run",
        text,
        "--deterministic",
        "--no-llm",
        "--max-refinements",
        "1",
        "--workers",
        "1",
    ]

    env = os.environ.copy()
    env["PYTHONPATH"] = f"{repo / 'src'}:{env.get('PYTHONPATH','')}"
    # Expect exit code 0 even if unproved (the CLI reports JSON and diagnostics deterministically)
    proc = subprocess.run(cmd, cwd=str(repo))
    assert proc.returncode == 0

