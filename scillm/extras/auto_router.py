from __future__ import annotations

from typing import Optional

from scillm import Router

from .model_selector import auto_model_list_from_env


def auto_router_from_env(
    *,
    kind: str = "text",
    require_json: bool = False,
    require_tools: bool = False,
    max_bases: int = 5,
    max_candidates_per_base: int = 2,
) -> Router:
    """
    Create a Router using dynamically discovered and ranked chutes from env.

    One‑liner: router = auto_router_from_env(kind="text", require_json=True)
    """
    model_list = auto_model_list_from_env(
        kind=kind,
        require_json=require_json,
        require_tools=require_tools,
        max_bases=max_bases,
        max_candidates_per_base=max_candidates_per_base,
    )
    if not model_list:
        raise RuntimeError("No candidate chutes found; set CHUTES_API_BASE_1/CHUTES_API_KEY_1 and model envs.")
    return Router(model_list=model_list)


__all__ = [
    "auto_router_from_env",
]

