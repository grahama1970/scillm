from src.codeworld.engine.mcts import run_mcts


def test_mcts_deterministic_and_visits_sum():
    task = "demo"
    ctx = {"n": 10}
    variants = [
        {"id": "A", "code": "def solve(ctx): return 1"},
        {"id": "B", "code": "def solve(ctx): return 2"},
        {"id": "C", "code": "def solve(ctx): return 3"},
    ]
    r1 = run_mcts(task, ctx, variants, rollouts=20, depth=4, uct_c=1.25, seed=1234, timeout_ms=0)
    r2 = run_mcts(task, ctx, variants, rollouts=20, depth=4, uct_c=1.25, seed=1234, timeout_ms=0)
    assert r1["error"] is None and r2["error"] is None
    assert r1["best_variant"] == r2["best_variant"]
    assert r1["visits"] == r1["rollouts"]
    assert r2["visits"] == r2["rollouts"]


def test_mcts_explores_all_children_initially():
    task = "x"
    ctx = {}
    variants = [{"v": i} for i in range(4)]
    r = run_mcts(task, ctx, variants, rollouts=4, depth=3, uct_c=1.0, seed=42, timeout_ms=0)
    assert r["explored"] == len(variants)
