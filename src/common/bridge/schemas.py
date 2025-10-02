from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ProviderArgs(BaseModel):
    name: str = Field(..., description="Provider name, e.g., 'lean4' or 'codeworld'")
    args: Dict[str, Any] = Field(default_factory=dict, description="Provider-specific arguments")


class Options(BaseModel):
    max_seconds: Optional[float] = Field(
        None, description="Optional wall-clock limit for the end-to-end run (seconds)"
    )


class CanonicalBridgeRequest(BaseModel):
    messages: List[Dict[str, Any]]
    items: Optional[List[Dict[str, Any]]] = None
    provider: Optional[ProviderArgs] = None
    options: Optional[Options] = None

