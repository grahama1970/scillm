import os
from contextlib import contextmanager

from litellm.llms.codeworld import CodeWorldLLM


@contextmanager
def _env(**kvs):
    old = {k: os.environ.get(k) for k in kvs}
    try:
        for k, v in kvs.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_alias_injects_mcts_and_defaults():
    llm = CodeWorldLLM()
    with _env(CODEWORLD_MCTS_AUTO_N=None, CODEWORLD_MCTS_AUTO_TEMPERATURE=None):
        payload = llm._build_payload("codeworld/mcts:auto", [{"role": "user", "content": "x"}], {})
    args = payload["provider"]["args"]
    assert args["strategy"] == "mcts"
    assert args["autogenerate"] is True
    assert args["n_variants"] == 6
    assert args["temperature"] == 0.0


def test_alias_plus_auto_synonym():
    llm = CodeWorldLLM()
    payload = llm._build_payload("codeworld/mcts+auto", [{"role": "user", "content": "x"}], {})
    args = payload["provider"]["args"]
    assert args["strategy"] == "mcts"
    assert args["autogenerate"] is True


def test_env_overrides_defaults_for_auto():
    llm = CodeWorldLLM()
    with _env(
        CODEWORLD_MCTS_AUTO_N="8",
        CODEWORLD_MCTS_AUTO_TEMPERATURE="0.25",
        CODEWORLD_MCTS_AUTO_MODEL="gpt-4o-mini",
        CODEWORLD_MCTS_AUTO_MAX_TOKENS="1024",
    ):
        payload = llm._build_payload("codeworld/mcts:auto", [{"role": "user", "content": "x"}], {})
    args = payload["provider"]["args"]
    assert args["n_variants"] == 8
    assert abs(float(args["temperature"]) - 0.25) < 1e-9
    assert args["generator_model"] == "gpt-4o-mini"
    assert args["max_tokens"] == 1024


def test_exploration_alias_dropped_when_uct_c_present():
    llm = CodeWorldLLM()
    payload = llm._build_payload(
        "codeworld",
        [{"role": "user", "content": "x"}],
        {"strategy": "mcts", "uct_c": 1.2, "exploration_constant": 2.0},
    )
    args = payload["provider"]["args"]
    assert "uct_c" in args
    assert "exploration_constant" not in args

