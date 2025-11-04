from __future__ import annotations

import json
from typing import Any, Dict, Optional

import httpx


class UnitsClarification(Exception):
    def __init__(self, detail: Dict[str, Any]):
        super().__init__("clarification_needed")
        self.detail = detail


async def request_with_units(
    *,
    base_url: str,
    payload: Dict[str, Any],
    timeout: float = 30.0,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """POST /bridge/complete and surface unit clarifications.

    - If the bridge returns 422 with `clarification_needed`, raise UnitsClarification(detail)
      so the caller can display `human_prompt` to a human and retry with
      `engineering_confirmed: true`.
    - Returns JSON on 200.
    """
    owns = False
    ac = client
    try:
        if ac is None:
            ac = httpx.AsyncClient(base_url=base_url, timeout=timeout)
            owns = True
        r = await ac.post("/bridge/complete", json=payload)
        if r.status_code == 422:
            body = r.json()
            detail = body.get("detail") if isinstance(body, dict) and "detail" in body else body
            if isinstance(detail, dict) and detail.get("type") == "clarification_needed":
                raise UnitsClarification(detail)
        r.raise_for_status()
        return r.json()
    finally:
        if owns and ac is not None:
            await ac.aclose()

