from typing import Any, Dict, List
from loguru import logger

def validate_llm_score_response(payload: Dict[str, Any]) -> List[str]:
    """
    Validate shape for llm-score results:
    {
      "items": [{ "edge_id": str, "score": float in [0,1], "rationale"?: str, "model": str }],
      "model": str,
      "ts": str
    }
    Returns [] when valid; else a list of error strings.
    """
    errors: List[str] = []
    if not isinstance(payload, dict):
        return ["payload must be an object"]
    items = payload.get("items")
    if not isinstance(items, list):
        errors.append("items must be a list")
    else:
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                errors.append(f"items[{i}] must be an object")
                continue
            if not isinstance(it.get("edge_id"), str) or not it.get("edge_id"):
                errors.append(f"items[{i}].edge_id must be a non-empty string")
            score = it.get("score")
            try:
                f = float(score)
                if not (0.0 <= f <= 1.0):
                    raise ValueError
            except Exception as exc:
                logger.error("Suppressed error in schemas: {}", exc)
                errors.append(f"items[{i}].score must be a number in [0,1]")
            if "rationale" in it and not (it["rationale"] is None or isinstance(it["rationale"], str)):
                errors.append(f"items[{i}].rationale must be a string if present")
            if not isinstance(it.get("model"), str) or not it.get("model"):
                errors.append(f"items[{i}].model must be a non-empty string")
    if not isinstance(payload.get("model"), str) or not payload.get("model"):
        errors.append("model must be a non-empty string")
    if not isinstance(payload.get("ts"), str) or not payload.get("ts"):
        errors.append("ts must be an ISO timestamp string")
    return errors

