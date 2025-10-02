from __future__ import annotations


def test_manifest_shape_contains_ids_and_options():
    # Synthetic response matching Lean4/CodeWorld bridge shape
    response = {
        "results": [
            {"item_id": "item-1", "item": {"requirement_text": "0 + n = n"}},
            {"item_id": "item-2", "item": {"requirement_text": "m + n = n + m"}},
        ],
        "run_manifest": {
            "run_id": "abc123",
            "schema": "canonical+lean4@v1",
            "options": {"max_seconds": 180, "session_id": "s1", "track_id": "t1"},
            "item_ids": ["item-1", "item-2"],
        },
    }
    rm = response.get("run_manifest", {})
    assert isinstance(rm.get("run_id"), str) and rm["run_id"]
    assert isinstance(rm.get("options"), dict)
    assert rm["options"]["session_id"] == "s1"
    assert rm["options"]["track_id"] == "t1"
    assert response["results"][0]["item_id"] in rm.get("item_ids", [])

