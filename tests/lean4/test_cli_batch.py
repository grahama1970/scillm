import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(
    not Path(os.getenv("LEAN4_REPO", "/home/graham/workspace/experiments/lean4")).exists(),
    reason="LEAN4_REPO not available",
)
def test_cli_batch_deterministic(tmp_path: Path) -> None:
    repo = Path(os.getenv("LEAN4_REPO", "/home/graham/workspace/experiments/lean4")).resolve()

    items = [
        {"requirement_text": "0 + n = n", "context": {"section_id": "S1"}},
        {"requirement_text": "m + n = n + m", "context": {"section_id": "S2"}},
    ]

    inp = tmp_path / "in.json"
    out = tmp_path / "out.json"
    inp.write_text(json.dumps(items))

    cmd = [
        sys.executable,
        "-m",
        "lean4_prover.cli_mini",
        "batch",
        "--input-file",
        str(inp),
        "--output-file",
        str(out),
        "--deterministic",
        "--no-llm",
        "--auto-prune-strategies",
    ]

    env = os.environ.copy()
    env["PYTHONPATH"] = f"{repo / 'src'}:{env.get('PYTHONPATH','')}"
    proc = subprocess.run(cmd, cwd=str(repo), env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    data = json.loads(out.read_text())
    stats = data.get("statistics", {})
    assert isinstance(stats, dict)
    assert "successful_proofs" in stats
    assert "failed_proofs" in stats
    assert "unproved" in stats

