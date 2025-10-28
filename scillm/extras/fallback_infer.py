from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from scillm import Router, completion
from .attribution import extract_served_model

from .model_selector import auto_model_list_from_env


def _embed_meta(resp: Any, meta: Dict[str, Any]) -> None:
    """Attach meta to response without breaking common consumers.

    - Always set attribute `scillm_meta` on object responses.
    - Optionally embed a `scillm_meta` key into dict-shaped responses when
      SCILLM_EMBED_META=1 is set. Default is attribute-only to avoid surprises.
    """
    try:
        setattr(resp, "scillm_meta", meta)
    except Exception:
        pass
    if os.getenv("SCILLM_EMBED_META", "0") in {"1", "true", "yes", "y"}:
        try:
            if isinstance(resp, dict):
                resp.setdefault("scillm_meta", meta)
        except Exception:
            pass


def infer_with_fallback(
    *,
    messages: List[Dict[str, Any]],
    kind: str = "text",
    require_json: bool = False,
    require_tools: bool = False,
    response_format: Optional[Dict[str, Any]] = None,
    max_retries: int = 3,
    retry_after: float = 1.0,
    timeout: float = 45.0,
) -> Tuple[Any, Dict[str, Any]]:
    """
    Perform an inference with automatic dynamic fallbacks and return (response, meta).

    - Discovers candidate chutes from env (CHUTES_API_BASE_n / CHUTES_API_KEY_n).
    - Dynamically selects closest models per base and ranks by availability and utilization.
    - Uses Router to pick the first healthy target; on Router failure, retries sequentially.
    - Attaches a `scillm_meta` attribute to the response containing attribution details.
    """
    model_list = auto_model_list_from_env(
        kind=kind,
        require_tools=require_tools,
        require_json=require_json,
    )
    if not model_list:
        raise RuntimeError("No candidate chutes found in environment; set CHUTES_API_BASE_1/CHUTES_API_KEY_1 and model envs.")

    # Primary: Router-based attempt
    router = Router(model_list=model_list)
    preferred = model_list[0]["model_name"]
    attempts: List[Dict[str, Any]] = []

    try:
        resp = router.completion(
            model=preferred,
            messages=messages,
            response_format=response_format,
            max_retries=max_retries,
            retry_after=retry_after,
            timeout=timeout,
        )
        served_model = extract_served_model(resp)
        meta = {
            "routing": "router",
            "preferred": preferred,
            "served_model": served_model,
            "attempts": attempts,
        }
        _embed_meta(resp, meta)
        return resp, meta
    except Exception as e:
        attempts.append({"via": "router", "error": str(e)})

    # Fallback: try each entry sequentially with direct completion
    for entry in model_list:
        p = entry.get("litellm_params", {})
        try:
            resp = completion(
                model=p.get("model"),
                custom_llm_provider=p.get("custom_llm_provider"),
                api_base=p.get("api_base"),
                api_key=p.get("api_key"),
                extra_headers=p.get("extra_headers"),
                messages=messages,
                response_format=response_format,
                max_retries=max_retries,
                retry_after=retry_after,
                timeout=timeout,
            )
            served_model = extract_served_model(resp)
            meta = {
                "routing": "sequential",
                "preferred": preferred,
                "selected": entry.get("model_name"),
                "served_model": served_model,
                "attempts": attempts,
            }
            _embed_meta(resp, meta)
            return resp, meta
        except Exception as e:  # try next
            attempts.append({"via": entry.get("model_name"), "error": str(e)})

    raise RuntimeError(f"All candidates failed. Attempts: {attempts}")


__all__ = [
    "infer_with_fallback",
]
