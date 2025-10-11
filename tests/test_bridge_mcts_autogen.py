import os
import json
import pytest

import src.codeworld.bridge.server as server


def _mock_call_llm_for_variants(prompt: str, **kwargs):
    raw = json.dumps({
        "variants": [
            {"id": "A", "title": "Simple", "complexity_tier": "basic", "rationale": "A", "code": "def f(): return 1"},
            {"id": "B", "title": "Alt", "complexity_tier": "moderate", "rationale": "B", "code": "def f(): return 2"},
        ]
    })
    return {"raw": raw}


@pytest.mark.parametrize("env_gate", [None, "1"])  # allowed
def test_autogen_creates_variants_then_mcts(monkeypatch, env_gate):
    monkeypatch.setattr(server, "_mcts_call_llm_for_variants", _mock_call_llm_for_variants)
    if env_gate is None:
        os.environ.pop("CODEWORLD_ENABLE_MCTS_GENERATE", None)
    else:
        os.environ["CODEWORLD_ENABLE_MCTS_GENERATE"] = env_gate

    entry = {"run_manifest": {}}
    provider_args = {"args": {"strategy": "mcts", "strategy_config": {"autogenerate": {"enabled": True, "n": 2}, "rollouts": 8, "depth": 3, "uct_c": 1.0}}}
    server.apply_mcts_strategy(entry, provider_args, task="choose best", context={})
    assert isinstance(entry.get("code_variants"), list) and len(entry["code_variants"]) == 2
    assert "mcts" in entry and entry["mcts"]["error"] is None
    gen = entry["run_manifest"].get("strategy_generator", {})
    assert gen.get("enabled") is True
    assert gen.get("skipped_by_env") is False


def test_autogen_respects_env_gate(monkeypatch):
    monkeypatch.setattr(server, "_mcts_call_llm_for_variants", _mock_call_llm_for_variants)
    os.environ["CODEWORLD_ENABLE_MCTS_GENERATE"] = "0"

    entry = {"run_manifest": {}}
    provider_args = {"args": {"strategy": "mcts", "strategy_config": {"autogenerate": {"enabled": True, "n": 2}}}}
    server.apply_mcts_strategy(entry, provider_args, task="t", context=None)
    assert not entry.get("code_variants")
    gen = entry["run_manifest"].get("strategy_generator", {})
    assert gen.get("enabled") is True and gen.get("skipped_by_env") is True
    assert "mcts" not in entry
