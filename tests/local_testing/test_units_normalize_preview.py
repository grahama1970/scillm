from __future__ import annotations

import os
from fastapi.testclient import TestClient

from lean4_prover.bridge.server import app


def test_units_normalize_preview_ok_and_questions():
    os.environ["LEAN4_UNITS_POLICY"] = "ask_always"
    client = TestClient(app)
    # Mixed: one parseable (pressure), one missing units (airspeed as bare number)
    payload = {
        "engineering": {
            "pressure": {"value": 14.7, "unit": "psi"},
            "airspeed": 250,
        },
        "units_policy": "ask_always",
    }
    r = client.post("/bridge/units/normalize", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False  # ask_always should request clarification
    assert body.get("type") == "clarification_needed"
    qs = body.get("questions") or []
    fields = {q.get("field") for q in qs if isinstance(q, dict)}
    assert "airspeed" in fields
    # SI preview should include pressure conversion
    si_prev = body.get("canonical_si_preview") or {}
    if "pressure" in si_prev:
        assert si_prev["pressure"]["unit"] in ("Pa", "kPa")

