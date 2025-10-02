from __future__ import annotations

import os
import importlib
import sys
from pathlib import Path

# Ensure repo root is importable for 'litellm' package
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def test_shape_payload_converts_items_and_backend(monkeypatch):
    monkeypatch.setenv("CERTAINLY_BACKEND", "lean4")
    # Import after env set so defaults are picked up
    mod = importlib.import_module("litellm.llms.lean4")
    _shape_payload = getattr(mod, "_shape_payload")
    messages = [{"role": "system", "content": "t"}]
    items = [{"requirement_text": "0 + n = n"}, {"requirement_text": "m + n = n + m"}]
    payload = _shape_payload(messages, {"items": items, "max_seconds": 123})
    assert payload["messages"] == messages
    assert "lean4_requirements" in payload and isinstance(payload["lean4_requirements"], list)
    assert payload["lean4_requirements"][0]["requirement_text"].startswith("0 + n")
    assert payload["max_seconds"] == 123.0
    # backend is carried alongside payload (placeholder)
    assert payload.get("backend") in ("lean4",)
