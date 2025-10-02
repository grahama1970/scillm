from __future__ import annotations

from typing import Any, Dict, List
import os

import httpx


class Lean4Provider:
    """Minimal Lean4 provider client mirroring CodeWorldProvider.

    Posts to the Lean4 bridge `/bridge/complete` endpoint.
    """

    def __init__(self, base: str, token: str | None = None):
        self.base = base.rstrip("/")
        self.token = token

    def _headers(self) -> Dict[str, str]:
        h: Dict[str, str] = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def acomplete(
        self,
        *,
        messages: List[Dict[str, Any]],
        requirements: List[Dict[str, Any]],
        flags: List[str] | None = None,
        request_timeout: float = 300.0,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "messages": messages,
            "lean4_requirements": requirements,
        }
        if flags:
            payload["lean4_flags"] = list(flags)
        async with httpx.AsyncClient(timeout=float(request_timeout) + 30.0) as client:
            r = await client.post(self.base + "/bridge/complete", json=payload, headers=self._headers())
            if r.status_code in (200, 202):
                return r.json()
            try:
                return {"error": True, "status": r.status_code, "body": r.json()}
            except Exception:
                return {"error": True, "status": r.status_code, "body": r.text}

