from __future__ import annotations

import math

from common import units as U


def test_speed_knots_to_ms():
    v = U.parse_quantity({"value": 250, "unit": "kn"})
    ms = v.to("m/s").magnitude
    assert 125 <= ms <= 130  # ~128.6 m/s


def test_pressure_ksi_to_mpa_roundtrip():
    q = U.parse_quantity("45 ksi")
    mpa = q.to("MPa")
    back = mpa.to("ksi")
    assert math.isclose(back.magnitude, 45.0, rel_tol=1e-6)


def test_slug_definition():
    m = U.Q(1, "slug")
    n = m.to("kg").magnitude
    assert 14.0 < n < 15.0  # ~14.5939 kg


def test_json_roundtrip():
    src = {"value": 101.325, "unit": "kPa"}
    q = U.parse_quantity(src)
    dj = U.to_json_dict(q)
    assert dj["unit"] in ("Pa", "kPa", "m^(-1) * kg * s^(-2)")
