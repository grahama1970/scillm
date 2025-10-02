from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile


def run_scoring(code: str, payload: dict) -> tuple[int, str]:
    with tempfile.NamedTemporaryFile("w", suffix="_score.py", delete=False) as sf:
        sf.write(code)
        sf.flush()
        scoring_path = sf.name
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "codeworld.engine.scoring_runner", scoring_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out, err = proc.communicate(json.dumps(payload), timeout=3)
        return proc.returncode, out.strip()
    finally:
        try:
            os.unlink(scoring_path)
        except Exception:
            pass


def test_scoring_allows_simple_math():
    code = """
def score(task, context, outputs, timings):
    expected = context.get('expected')
    result = outputs.get('result')
    correctness = 1.0 if (expected is not None and result == expected) else 0.0
    duration_ms = float(timings.get('duration_ms', 0))
    speed = max(0.0, min(1.0, 1.0 - duration_ms/1000.0))
    return {"correctness": correctness, "speed": speed}
"""
    payload = {"task": "t", "context": {"expected": 10}, "outputs": {"result": 10}, "timings": {"duration_ms": 100}}
    rc, out = run_scoring(code, payload)
    assert rc == 0
    obj = json.loads(out)
    assert obj["correctness"] == 1.0


def test_scoring_rejects_imports():
    code = """
import os
def score(task, context, outputs, timings):
    return {"x": 1.0}
"""
    payload = {"task": "t", "context": {}, "outputs": {}, "timings": {}}
    rc, out = run_scoring(code, payload)
    assert rc != 0
    assert "forbidden" in out or "scoring_failed" in out

