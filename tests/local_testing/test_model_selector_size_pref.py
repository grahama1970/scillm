import os
from scillm.extras.model_selector import _approx_params


def test_approx_params_variants():
    assert _approx_params("Qwen3-235B-Instruct") == 235.0
    assert _approx_params("InternVL3-78B") == 78.0
    assert _approx_params("foo-1T") == 1000.0
    assert _approx_params("") == 0.0

