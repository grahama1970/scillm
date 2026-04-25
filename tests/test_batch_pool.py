from __future__ import annotations

import pytest

from scillm.proxy.app import (
    _DEFAULT_MODEL_POOLS,
    _item_id,
    _lane_for_index,
    _messages_for_batch_item,
    _model_pool,
    _weighted_lane_sequence,
)
from scillm.proxy.errors import ProxyError


def test_default_qra_pool_uses_chutes_and_opencode_go_lanes():
    pool = _model_pool("qra-deepseek-pool")

    assert pool is not None
    models = [lane["model"] for lane in pool["lanes"]]
    assert "deepseek-ai/DeepSeek-V3-0324-TEE" in models
    assert "opencode-go/deepseek-v4-flash" in models


def test_weighted_lane_sequence_expands_weights():
    lanes = _DEFAULT_MODEL_POOLS["qra-deepseek-pool"]["lanes"]
    sequence = _weighted_lane_sequence(lanes)

    assert [lane["name"] for lane in sequence] == [
        "chutes-deepseek",
        "chutes-deepseek",
        "chutes-deepseek",
        "opencode-go-deepseek-v4-flash",
        "opencode-go-deepseek-v4-flash",
    ]


def test_lane_for_index_uses_weighted_round_robin():
    lanes = _DEFAULT_MODEL_POOLS["qra-deepseek-pool"]["lanes"]

    assigned = [_lane_for_index(lanes, index)["name"] for index in range(7)]

    assert assigned == [
        "chutes-deepseek",
        "chutes-deepseek",
        "chutes-deepseek",
        "opencode-go-deepseek-v4-flash",
        "opencode-go-deepseek-v4-flash",
        "chutes-deepseek",
        "chutes-deepseek",
    ]


def test_batch_item_accepts_messages_or_prompt():
    assert _messages_for_batch_item({"messages": [{"role": "user", "content": "hi"}]}) == [
        {"role": "user", "content": "hi"}
    ]
    assert _messages_for_batch_item({"prompt": "hi"}) == [{"role": "user", "content": "hi"}]


def test_batch_item_requires_content():
    with pytest.raises(ProxyError):
        _messages_for_batch_item({"id": "empty"})


def test_item_id_prefers_explicit_fields():
    assert _item_id({"item_id": "a", "id": "b"}, 0) == "a"
    assert _item_id({"id": "b"}, 0) == "b"
    assert _item_id({}, 2) == "item-3"
