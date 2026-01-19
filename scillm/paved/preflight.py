"""Paved-path helpers for SciLLM preflight & discovery."""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

try:  # litellm extras provide canonical model discovery
    from litellm.extras.preflight import (  # type: ignore
        preflight_models,
        discover_auth_style,
    )
except Exception as exc:  # pragma: no cover - import-time guard
    raise RuntimeError(
        "litellm.extras.preflight is required for scillm.paved preflight helpers"
    ) from exc


def _normalize_base(api_base: str) -> str:
    base = (api_base or "").strip()
    if base.endswith("/v1"):
        base = base[:-3]
    return base.rstrip("/")


def list_models_openai_like(
    api_base: str,
    api_key: str,
    *,
    custom_llm_provider: str = "openai_like",
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """Return available models for an OpenAI-like base.

    Args:
        api_base: Base URL (with or without /v1 suffix)
        api_key: API key used for Bearer/x-api-key auth
        custom_llm_provider: Placeholder for parity with Router APIs
        timeout: HTTP timeout for discovery call

    Returns a structured payload with models + metadata.
    """

    started = time.time()
    base = _normalize_base(api_base)
    models = sorted(preflight_models(api_base=base, api_key=api_key, timeout_s=timeout))
    return {
        "ok": bool(models),
        "provider": custom_llm_provider,
        "api_base": base,
        "models": models,
        "elapsed_s": round(time.time() - started, 3),
    }


def sanity_preflight(
    *,
    api_base: str,
    api_key: str,
    model: str,
    parallel: int = 3,
    wall_time_s: int = 30,
    attempts: int = 1,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """Check model availability + auth style before batch runs.

    This is a thin synchronous wrapper around the discovery helpers. It fetches
    /v1/models, validates the requested model, and reports the detected auth
    style. Callers can optionally wrap this in asyncio.to_thread as documented.
    """

    base = _normalize_base(api_base)
    started = time.time()
    attempt_log: List[Dict[str, Any]] = []
    ok = False
    last_error: Optional[str] = None

    for attempt in range(max(attempts, 1)):
        try:
            models = preflight_models(api_base=base, api_key=api_key, timeout_s=timeout, soft=False)
            ok = model in models
            attempt_log.append({
                "attempt": attempt + 1,
                "model_listed": ok,
                "total_models": len(models),
            })
            if ok:
                break
            last_error = f"model_not_found:{model}"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            attempt_log.append({"attempt": attempt + 1, "error": last_error})

    auth_style = discover_auth_style(api_base=base, api_key=api_key, prefer=None, timeout_s=timeout)

    payload = {
        "ok": ok,
        "api_base": base,
        "model": model,
        "parallel": parallel,
        "wall_time_s": wall_time_s,
        "attempts": attempt_log,
        "auth_style": auth_style,
        "elapsed_s": round(time.time() - started, 3),
    }
    if not ok:
        payload["error"] = last_error or "unknown_error"
    return payload


__all__ = ["sanity_preflight", "list_models_openai_like"]
