from __future__ import annotations

import time
import json
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

try:
    from common.bridge.schemas import (
        ProviderArgs as CanonProviderArgs,
        Options as CanonOptions,
        CanonicalBridgeRequest,
    )
except Exception:  # pragma: no cover
    CanonProviderArgs = None  # type: ignore
    CanonOptions = None  # type: ignore
    CanonicalBridgeRequest = BaseModel  # type: ignore

app = FastAPI(
    title="Coq Bridge",
    description="Skeleton Coq bridge endpoint using the canonical schema.",
    version="0.1.0",
)


class CoqBridgeRequest(CanonicalBridgeRequest):
    # Back-compat aliases (if any in future)
    coq_goals: Optional[List[Dict[str, Any]]] = None


@app.post("/bridge/complete")
async def bridge_complete(req: CoqBridgeRequest):
    items = req.items or req.coq_goals or []
    if not items:
        raise HTTPException(status_code=400, detail="items/coq_goals must contain at least one item")

    # Placeholder: echo the items with status ok — integrate a real Coq CLI/daemon later
    results = []
    for i, it in enumerate(items):
        results.append(
            {
                "index": i,
                "status": "ok",
                "goal": it.get("goal") or it.get("coq_goal_text") or it,
                "stdout": "",
                "stderr": "",
            }
        )

    response = {
        "summary": {
            "items": len(results),
            "succeeded": len(results),
            "failed": 0,
        },
        "statistics": {},
        "results": results,
        "duration_ms": 0,
        "stdout": "",
        "stderr": "",
        "run_manifest": {
            "ts": int(time.time()),
            "schema": "canonical+coq@v1",
        },
    }
    return JSONResponse(response)

