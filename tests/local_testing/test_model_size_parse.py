from scripts.chutes_auto_peers import _approx_params, _pick_peers


def test_param_parse_basic():
    assert _approx_params("Qwen3-235B-Instruct") == 235.0
    assert _approx_params("InternVL3-78B") == 78.0
    assert _approx_params("X-1T") == 1000.0
    assert _approx_params("unknown") == 0.0


def test_pick_peers_by_size_desc():
    ids = [
        "foo-24B",
        "bar-72B",
        "baz-235B",
        "qux-7B",
    ]
    peers = _pick_peers("text", ids, None, k=3)
    assert peers[:2] == ["baz-235B", "bar-72B"]

