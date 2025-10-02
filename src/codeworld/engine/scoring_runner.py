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
  - No imports are provided to scoring code by default; only the provided args.
  - Network is not disabled at OS level here; callers must ensure no-net where required.
"""

import json
import sys
from types import SimpleNamespace
import ast


def _limit_resources() -> None:  # best-effort on Unix
    try:
        import resource

        # CPU time: 0.5s
        resource.setrlimit(resource.RLIMIT_CPU, (1, 1))
        # Address space: ~256MB
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
        # File size 0 (disallow writes)
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        # Processes: 0 new
        resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
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

        # AST validation: allow a single FunctionDef 'score' and safe expressions only
        tree = ast.parse(code, filename=scoring_path)

        allowed_nodes = (
            ast.Module, ast.FunctionDef, ast.arguments, ast.arg,
            ast.Return, ast.Assign, ast.AnnAssign, ast.If,
            ast.Expr, ast.Name, ast.Load, ast.Store,
            ast.Constant, ast.Num, ast.Str, ast.Dict, ast.List, ast.Tuple,
            ast.BinOp, ast.BoolOp, ast.UnaryOp, ast.Compare,
            ast.Subscript, ast.Slice, ast.Index,
            ast.And, ast.Or, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow,
            ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Not,
        )
        forbidden = (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.ClassDef, ast.With,
                     ast.AsyncFunctionDef, ast.Await, ast.Try, ast.Raise, ast.While, ast.For, ast.Lambda,
                     ast.Call, ast.Attribute, ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp,
                     ast.Yield, ast.YieldFrom)

        # Allow module docstring as first Expr(Constant)
        def _check(node: ast.AST) -> None:
            if isinstance(node, forbidden):
                raise ValueError(f"forbidden syntax: {node.__class__.__name__}")
            if not isinstance(node, allowed_nodes):
                # permit docstring Expr(Constant)
                if isinstance(node, ast.Expr) and isinstance(getattr(node, 'value', None), ast.Constant):
                    return
                # allow type ignores
                if isinstance(node, ast.Load) or isinstance(node, ast.Store):
                    return
                raise ValueError(f"unsupported syntax: {node.__class__.__name__}")
            for child in ast.iter_child_nodes(node):
                _check(child)

        _check(tree)

        # Restrict builtins and globals; provide no imports by default
        safe_globals = {"__builtins__": {}}
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
