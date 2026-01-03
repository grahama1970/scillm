from __future__ import annotations

import os
from fastapi.testclient import TestClient

from lean4_prover.bridge.server import app


def test_requires_engineering_confirmation_under_ask_always():
    os.environ["LEAN4_UNITS_POLICY"] = "ask_always"
    os.environ["LEAN4_BRIDGE_ECHO"] = "1"  # allow echo path to succeed when confirmation present
    client = TestClient(app)

    base_payload = {
        "messages": [{"role": "user", "content": "prove"}],
        "items": [{"requirement_text": "x must equal x"}],
        "engineering": {"airspeed": {"value": 250, "unit": "kn"}},
    }

    # Without engineering_confirmed -> 422
    r1 = client.post("/bridge/complete", json={**base_payload, "units_policy": "ask_always"})
    assert r1.status_code == 422, r1.text

    # With engineering_confirmed -> 200 (echo path)
    r2 = client.post(
        "//bridge/complete".replace("//", "/"),
        json={**base_payload, "units_policy": "ask_always", "engineering_confirmed": True},
    )
    assert r2.status_code == 200, r2.text

