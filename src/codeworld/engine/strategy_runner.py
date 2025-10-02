from __future__ import annotations
"""
Minimal Python strategy runner (alpha).
Executes contestant strategy code in a constrained subprocess.
Contract: code defines `solve(...)` or `main(context)`; returns JSON-serializable result.
stdin: {"context": {...}}
stdout: {"result": <value>, "loc": <int>}
"""
import json, sys, ast, math

def _limit_resources() -> None:
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (1, 1))
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
        # stdio + one extra for reading the strategy file
        resource.setrlimit(resource.RLIMIT_NOFILE, (4, 4))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        pass
    try:
        sys.setrecursionlimit(1000)
    except Exception:
        pass

_DISALLOWED_CALLS = {"__import__", "eval", "exec", "open", "compile", "breakpoint", "input", "globals", "locals", "vars"}
_DISALLOWED_NODES = {ast.Import, ast.ImportFrom, ast.With, ast.AsyncWith, ast.Try, ast.Raise, ast.ClassDef, ast.Lambda, ast.Global, ast.Nonlocal, ast.Await, ast.Yield, ast.YieldFrom}
_ALLOWED_TOPLEVEL = {ast.FunctionDef, ast.Assign, ast.AnnAssign, ast.Expr, ast.If}

def _validate_source(code_str: str) -> None:
    tree = ast.parse(code_str, mode="exec")
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

def main() -> int:
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: strategy_runner.py <path_to_strategy_py>"}))
        return 2
    path = sys.argv[1]
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:
        print(json.dumps({"error": f"invalid payload: {exc}"}))
        return 2
    context = payload.get("context") or {}
    _limit_resources()
    ns: dict[str, object] = {}
    try:
        code = open(path, "r", encoding="utf-8").read()
        _validate_source(code)
        SAFE_BUILTINS = {
            "abs": abs, "min": min, "max": max, "sum": sum, "len": len,
            "float": float, "int": int, "bool": bool, "round": round, "range": range,
            "enumerate": enumerate, "zip": zip, "map": map, "filter": filter,
        }
        safe_globals = {"__builtins__": SAFE_BUILTINS, "math": math}
        exec(code, safe_globals, ns)
        fn = ns.get("solve") or ns.get("main")
        if callable(fn):
            # Common arg conventions
            arg = context.get("input", context.get("xs", context))
            try:
                result = fn(arg)  # type: ignore[misc]
            except TypeError:
                result = fn()     # type: ignore[misc]
        else:
            raise RuntimeError("strategy file must define callable 'solve' or 'main'")
    except Exception as exc:
        print(json.dumps({"error": f"strategy_failed: {exc}"}))
        return 1
    try:
        loc = len(code.splitlines())
        print(json.dumps({"result": result, "loc": loc}))
        return 0
    except Exception as exc:
        print(json.dumps({"error": f"result_encoding_failed: {exc}"}))
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
