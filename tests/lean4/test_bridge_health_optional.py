import os
import pytest
import httpx


@pytest.mark.optional
def test_lean4_bridge_health_optional():
    base = os.getenv("LEAN4_BRIDGE_BASE")
    if not base:
        pytest.skip("LEAN4_BRIDGE_BASE not set; skipping live bridge health check")
    r = httpx.get(f"{base.rstrip('/')}/healthz", timeout=5.0)
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, dict)
    assert data.get("ok") is True
