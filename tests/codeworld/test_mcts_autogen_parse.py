from __future__ import annotations

import json

from src.codeworld.bridge.server import _mcts_extract_variants_from_raw


def test_extract_variants_strict_json():
    raw = json.dumps(
        {
            "variants": [
                {"id": "v1", "title": "t1", "code": "def solve(ctx): return 1"},
                {"id": "v2", "title": "t2", "code": "def solve(ctx): return 2"},
            ]
        }
    )
    out = _mcts_extract_variants_from_raw(raw)
    assert isinstance(out, list)
    assert len(out) == 2
    assert out[0]["id"] == "v1"


def test_extract_variants_from_wrapped_text():
    # Simulate stray text with JSON inside
    raw = "Here are your variants:\n" + json.dumps({"variants": [{"id": "x", "code": ""}]}) + "\nThanks."
    out = _mcts_extract_variants_from_raw(raw)
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0]["id"] == "x"

