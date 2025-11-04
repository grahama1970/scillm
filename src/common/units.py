from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Any, Dict, Tuple, Union

from pint import UnitRegistry


_ureg: UnitRegistry | None = None


def _load_registry() -> UnitRegistry:
    global _ureg
    if _ureg is not None:
        return _ureg
    # Load custom definitions packaged alongside this module
    ureg = UnitRegistry()
    try:
        with resources.files(__package__).joinpath("units_defense.txt").open("r", encoding="utf-8") as f:
            ureg.load_definitions(f)
    except Exception:
        # Fallback without custom defs if missing; still useful in dev
        pass
    ureg.default_system = "mks"
    _ureg = ureg
    return ureg


def ureg() -> UnitRegistry:
    return _load_registry()


Q = lambda *args, **kwargs: _load_registry().Quantity(*args, **kwargs)  # noqa: E731


def parse_quantity(obj: Union[str, Dict[str, Any], Tuple[Any, str], float, int], default_unit: str | None = None):
    """Parse various inputs into a Pint Quantity.

    Accepts:
    - "3.2 m/s"
    - {"value": 3.2, "unit": "m/s"}
    - (3.2, "m/s")
    - bare number with default_unit provided
    """
    u = _load_registry()
    if isinstance(obj, (int, float)):
        if not default_unit:
            raise ValueError("default_unit required for bare numbers")
        return u.Quantity(obj, default_unit)
    if isinstance(obj, str):
        return u(obj)
    if isinstance(obj, tuple) and len(obj) == 2:
        val, unit = obj
        return u.Quantity(val, str(unit))
    if isinstance(obj, dict):
        val = obj.get("value")
        unit = obj.get("unit")
        if val is None or not unit:
            raise ValueError("quantity dict requires 'value' and 'unit'")
        return u.Quantity(val, str(unit))
    raise TypeError(f"Unsupported quantity input: {type(obj)}")


def to_json_dict(q) -> Dict[str, Any]:
    q = q.to_base_units()
    return {"value": float(q.magnitude), "unit": f"{q.units:~P}"}


@dataclass
class QuantityJSON:
    value: float
    unit: str

    @classmethod
    def from_any(cls, obj: Any, default_unit: str | None = None) -> "QuantityJSON":
        q = parse_quantity(obj, default_unit)
        d = to_json_dict(q)
        return cls(**d)

    def to_pint(self):  # Quantity
        return Q(self.value, self.unit)

    def json(self) -> str:
        return json.dumps({"value": self.value, "unit": self.unit})

