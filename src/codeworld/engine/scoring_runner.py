from __future__ import annotations

"""
Minimal scoring microkernel for CodeWorld.

Executes a human-authored scoring function in a constrained Python subprocess.
Contract:
  - scoring code must define: def score(task: str, context: dict, outputs: dict, timings: dict) -> dict
  - returns a dict of metric_name -> float (0..1); may include 'aggregate'.

This runner is intended to be invoked as a module with a path to a scoring file
that defines `score`, and a JSON-encoded payload on stdin:

  python -m codeworld.engine.scoring_runner /tmp/scoring.py < payload.json

The payload must be a JSON object with keys: task, context, outputs, timings.

Safety (alpha):
  - Sets CPU and address space limits where available (Linux/Unix).
  - Strict AST validation: no imports, no with/try/raise/class/lambda/global/nonlocal.
  - Builtins restricted to numeric-safe helpers; math module allowed.
  - Network not disabled here; callers may run this under a no-net namespace.
"""

import json
import sys
from types import SimpleNamespace
import ast
import math


def _limit_resources() -> None:  # best-effort on Unix
    try:
        import resource
        # CPU time: 1s (integer granularity)
        resource.setrlimit(resource.RLIMIT_CPU, (1, 1))
        # Address space: ~256MB
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
        # File size 0 (disallow writes)
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        # Processes: 0 new
        resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
        # Limit open fds to stdio + one extra for reading the scoring file
        resource.setrlimit(resource.RLIMIT_NOFILE, (4, 4))
        # No core dumps
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        pass
    try:
        import sys as _sys
        _sys.setrecursionlimit(1000)
    except Exception:
        pass


def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: scoring_runner.py <path_to_scoring_py>"}))
        return 2
    scoring_path = sys.argv[1]
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:
        print(json.dumps({"error": f"invalid payload: {exc}"}))
        return 2

    task = payload.get("task")
    context = payload.get("context") or {}
    outputs = payload.get("outputs") or {}
    timings = payload.get("timings") or {}

    _limit_resources()

    # Load and validate scoring code
    ns: dict[str, object] = {}
    try:
        with open(scoring_path, "r", encoding="utf-8") as f:
            code = f.read()

        # AST validation with denylist/allowlist
        _DISALLOWED_CALLS = {"__import__", "eval", "exec", "open", "compile", "breakpoint", "input", "globals", "locals", "vars"}
        _DISALLOWED_NODES = {ast.Import, ast.ImportFrom, ast.With, ast.AsyncWith, ast.Try, ast.Raise, ast.ClassDef, ast.Lambda, ast.Global, ast.Nonlocal, ast.Await, ast.Yield, ast.YieldFrom}
        _ALLOWED_TOPLEVEL = {ast.FunctionDef, ast.Assign, ast.AnnAssign, ast.Expr, ast.If}

        tree = ast.parse(code, filename=scoring_path)
        for node in ast.walk(tree):
            if type(node) in _DISALLOWED_NODES:
                raise ValueError(f"node_forbidden:{type(node).__name__}")
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Name) and fn.id in _DISALLOWED_CALLS:
                    raise ValueError(f"call_forbidden:{fn.id}")
            if isinstance(node, ast.Attribute) and isinstance(node.attr, str) and node.attr.startswith("__"):
                raise ValueError("dunder_attribute_forbidden")
        for stmt in tree.body:
            if type(stmt) not in _ALLOWED_TOPLEVEL:
                raise ValueError(f"toplevel_forbidden:{type(stmt).__name__}")

        # Restrict builtins and globals; allow math only
        SAFE_BUILTINS = {
            "abs": abs, "min": min, "max": max, "sum": sum, "len": len,
            "float": float, "int": int, "bool": bool, "round": round, "range": range,
            "enumerate": enumerate, "zip": zip, "map": map, "filter": filter,
        }
        safe_globals = {"__builtins__": SAFE_BUILTINS, "math": math}
        exec(compile(tree, scoring_path, "exec"), safe_globals, ns)
        fn = ns.get("score")
        if not callable(fn):
            raise RuntimeError("scoring file must define callable 'score' function")
        result = fn(task, context, outputs, timings)  # type: ignore[misc]
    except Exception as exc:
        print(json.dumps({"error": f"scoring_failed: {exc}"}))
        return 1

    try:
        print(json.dumps(result))
        return 0
    except Exception as exc:
        print(json.dumps({"error": f"result_encoding_failed: {exc}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
