from __future__ import annotations

from codeworld.engine.judge import lexicographic_aggregate


def test_lex_aggregate_shape_and_order():
    m1 = {"correctness": 1.0, "speed": 0.9, "brevity": 0.5}
    m2 = {"correctness": 1.0, "speed": 0.8, "brevity": 0.9}
    k1 = lexicographic_aggregate(m1)
    k2 = lexicographic_aggregate(m2)
    assert isinstance(k1, list) and isinstance(k2, list)
    assert len(k1) == 3 and len(k2) == 3
    # correctness ties, speed breaks tie: m1 should outrank m2
    assert k1[0] == k2[0] == 1.0
    assert k1[1] > k2[1]

