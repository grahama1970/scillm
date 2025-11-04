from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, field_validator

from .. import units as U


class QuantityModel(BaseModel):
    value: float
    unit: str

    @field_validator("unit")
    def _unit_nonempty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("unit must be non-empty")
        return v

    @classmethod
    def from_any(cls, obj: Any, default_unit: str | None = None) -> "QuantityModel":
        q = U.parse_quantity(obj, default_unit)
        d = U.to_json_dict(q)
        return cls(**d)

    def to_pint(self):
        return U.Q(self.value, self.unit)

    def to_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "unit": self.unit}

