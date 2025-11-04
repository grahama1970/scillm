from __future__ import annotations

import os
from fastapi.testclient import TestClient

from lean4_prover.bridge.server import app


def test_units_ask_always_returns_422_for_parseable_inputs():
    # Ensure collaborative policy
    os.environ["LEAN4_UNITS_POLICY"] = "ask_always"
    client = TestClient(app)
    payload = {
        "messages": [{"role": "user", "content": "prove"}],
        "items": [{"requirement_text": "x must equal x"}],
        "units_policy": "ask_always",
        "engineering": {"airspeed": {"value": 250, "unit": "kn"}},
    }
    r = client.post("/bridge/complete", json=payload)
    assert r.status_code == 422, r.text
    body = r.json()
    # FastAPI wraps detail under {"detail": ...}
    detail = body.get("detail") if "detail" in body else body
    assert detail.get("type") == "clarification_needed"
    # human_prompt should mention the field and provide recommended units
    hp = detail.get("human_prompt", "")
    assert "airspeed" in hp
    assert "m/s" in hp or "kn" in hp
    # questions must enumerate the field
    qs = detail.get("questions") or []
    fields = {q.get("field") for q in qs if isinstance(q, dict)}
    assert "airspeed" in fields

