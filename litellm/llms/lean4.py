from __future__ import annotations

"""
Provider: lean4 / certainly

Provides LiteLLM integration for Lean4 theorem proving via the Certainly package.

Two modes:
1. DIRECT MODE (preferred): If certainly package is installed, uses direct Python
   imports for better performance, cleaner errors, and no HTTP overhead.
2. HTTP MODE (fallback): If certainly not installed, falls back to HTTP bridge
   at CERTAINLY_BRIDGE_BASE for backward compatibility.

Usage:
    from litellm import completion
    resp = completion(
        model="certainly/local",
        custom_llm_provider="lean4",
        items=[{"requirement_text": "Prove that n + 0 = n"}],
    )
    result = resp.additional_kwargs["certainly"]
"""

import asyncio
import json
import os
from typing import Any, Optional, Union, Callable, Dict, List

import httpx

from litellm.llms.custom_llm import CustomLLM, CustomLLMError
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, HTTPHandler
from litellm.utils import ModelResponse


# ---------------------------------------------------------------------------
# Direct import detection (lazy)
# ---------------------------------------------------------------------------

_direct_mode_available: Optional[bool] = None


def _check_direct_mode() -> bool:
    """Check if direct certainly import is available."""
    global _direct_mode_available
    if _direct_mode_available is not None:
        return _direct_mode_available
    try:
        from scillm.integrations.certainly import is_available
        _direct_mode_available = is_available()
    except ImportError:
        _direct_mode_available = False
    return _direct_mode_available


def _use_direct_mode() -> bool:
    """Determine whether to use direct mode or HTTP fallback."""
    # Explicit override: SCILLM_CERTAINLY_HTTP_ONLY=1 forces HTTP mode
    if os.getenv("SCILLM_CERTAINLY_HTTP_ONLY", "").lower() in ("1", "true"):
        return False
    return _check_direct_mode()


# ---------------------------------------------------------------------------
# HTTP fallback helpers (kept for backward compatibility)
# ---------------------------------------------------------------------------


def _resolve_base(api_base: Optional[str]) -> str:
    """Resolve bridge base URL from params or environment."""
    base = (
        api_base
        or os.getenv("CERTAINLY_BRIDGE_BASE")
        or os.getenv("LEAN4_BRIDGE_BASE")
        or "http://127.0.0.1:8787"
    )
    return base.rstrip("/")


def _shape_http_payload(messages: list, optional_params: dict | None) -> Dict[str, Any]:
    """Shape payload for HTTP bridge endpoint."""
    opt = dict(optional_params or {})
    backend = (opt.pop("backend", None) or os.getenv("CERTAINLY_BACKEND") or "lean4").lower()

    requirements = opt.pop("lean4_requirements", None) or opt.pop("items", None)
    if not isinstance(requirements, list) or not requirements:
        raise CustomLLMError(
            status_code=400,
            message="lean4 provider requires 'lean4_requirements' or 'items' list"
        )

    flags = opt.pop("lean4_flags", None) or opt.pop("flags", None)
    if not flags and os.getenv("CERTAINLY_LLM_DEFAULT", "").lower() in ("1", "true", "yes", "on"):
        model_env = os.getenv("CERTAINLY_MODEL") or os.getenv("OPENROUTER_DEFAULT_MODEL") or ""
        flags = [
            "--model", model_env,
            "--strategies", "direct,structured,computational",
            "--max-refinements", "1",
            "--best-of",
        ]

    payload: Dict[str, Any] = {
        "messages": messages,
        "lean4_requirements": requirements,
        "backend": backend,
    }
    if flags:
        payload["lean4_flags"] = flags
    if "max_seconds" in opt:
        payload["max_seconds"] = float(opt.pop("max_seconds"))

    # Forward session/track metadata
    options = opt.pop("options", {}) if isinstance(opt.get("options"), dict) else {}
    if opt.get("session_id"):
        options["session_id"] = opt.pop("session_id")
    if opt.get("track_id"):
        options["track_id"] = opt.pop("track_id")
    if options:
        payload["options"] = options

    return payload


def _summarize(resp_json: Dict[str, Any], label: str = "Certainly") -> str:
    """Create summary string from bridge response."""
    try:
        s = resp_json.get("summary", {}) or {}
        return f"{label}: items={s.get('items')}, proved={s.get('proved')}, failed={s.get('failed')}"
    except Exception:
        return f"{label}: completed (see additional_kwargs.certainly)"


def _render_content(resp_json: Dict[str, Any], optional_params: dict | None) -> str:
    """Render response content, optionally as JSON."""
    rf = (optional_params or {}).get("response_format", {})
    if isinstance(rf, dict) and rf.get("type") in ("json_object", "json_schema"):
        try:
            return json.dumps({
                "summary": resp_json.get("summary"),
                "statistics": resp_json.get("statistics"),
                "results": resp_json.get("results") or resp_json.get("proof_results"),
                "duration_ms": resp_json.get("duration_ms"),
            }, ensure_ascii=False)
        except Exception:
            pass
    return _summarize(resp_json)


def _attach_results(model_response: ModelResponse, data: Dict[str, Any]) -> None:
    """Attach certainly results to model response."""
    model_response.additional_kwargs = getattr(model_response, "additional_kwargs", {}) or {}
    model_response.additional_kwargs["certainly"] = data
    if os.getenv("LITELLM_CERTAINLY_ATTACH_BOTH", "1") == "1":
        model_response.additional_kwargs["lean4"] = data


# ---------------------------------------------------------------------------
# Direct mode implementation
# ---------------------------------------------------------------------------


async def _run_direct_prove(
    requirements: List[Dict[str, Any]],
    optional_params: dict,
) -> Dict[str, Any]:
    """Run proof via direct certainly import."""
    from scillm.integrations.certainly import prove_requirement, check_lean_container

    # For batch, we process first item (single proof mode for now)
    # TODO: Support batch via certainly's batch API when available
    if not requirements:
        return {"ok": False, "error": "No requirements provided"}

    req = requirements[0]
    requirement_text = req.get("requirement_text") or req.get("requirement") or ""
    tactics = req.get("tactics") or req.get("strategies") or []
    if isinstance(tactics, str):
        tactics = [t.strip() for t in tactics.split(",") if t.strip()]

    # Check container before attempting
    container = os.getenv("LEAN4_CONTAINER", "lean_runner")
    if not check_lean_container(container):
        return {
            "ok": False,
            "error": f"Lean container '{container}' not running. Start with: make lean-runner-up",
        }

    compile_timeout = optional_params.get("compile_timeout_s") or optional_params.get("max_seconds")

    result = await prove_requirement(
        requirement=requirement_text,
        tactics=tactics,
        container=container,
        compile_timeout_s=int(compile_timeout) if compile_timeout else None,
        num_candidates=optional_params.get("num_candidates", 3),
    )

    # Normalize to bridge-compatible format
    if result.get("ok"):
        best = result.get("best", {})
        return {
            "summary": {"items": 1, "proved": 1, "failed": 0},
            "results": [{"ok": True, "requirement_text": requirement_text, "lean_code": best.get("lean4")}],
            "proof_results": [best],
            "statistics": {"compile_ms": best.get("compile_ms", 0)},
            "duration_ms": best.get("compile_ms", 0),
            "_direct_mode": True,
            "_raw": result,
        }
    else:
        return {
            "summary": {"items": 1, "proved": 0, "failed": 1},
            "results": [{"ok": False, "requirement_text": requirement_text, "error": result.get("error")}],
            "diagnosis": result.get("diagnosis"),
            "attempts": result.get("attempts", []),
            "_direct_mode": True,
            "_raw": result,
        }


# ---------------------------------------------------------------------------
# Provider class
# ---------------------------------------------------------------------------


class Lean4LLM(CustomLLM):
    """LiteLLM provider for Lean4/Certainly theorem proving."""

    def completion(
        self,
        model: str,
        messages: list,
        api_base: Optional[str],
        custom_prompt_dict: dict,
        model_response: ModelResponse,
        print_verbose: Callable,
        encoding,
        api_key,
        logging_obj,
        optional_params: dict,
        acompletion=None,
        litellm_params=None,
        logger_fn=None,
        headers: dict = {},
        timeout: Optional[Union[float, httpx.Timeout]] = None,
        client: Optional[HTTPHandler] = None,
    ) -> ModelResponse:
        # Try direct mode first
        if _use_direct_mode():
            requirements = optional_params.get("items") or optional_params.get("lean4_requirements") or []
            if requirements:
                try:
                    data = asyncio.get_event_loop().run_until_complete(
                        _run_direct_prove(requirements, optional_params)
                    )
                    model_response.model = model
                    model_response.choices[0].message.content = _render_content(data, optional_params)
                    model_response.choices[0].message.role = "assistant"
                    _attach_results(model_response, data)
                    return model_response
                except Exception as e:
                    # Fall through to HTTP mode on direct mode failure
                    if os.getenv("SCILLM_CERTAINLY_DIRECT_STRICT", "").lower() in ("1", "true"):
                        raise CustomLLMError(status_code=500, message=f"Direct mode failed: {e}")

        # HTTP fallback mode
        base = _resolve_base(api_base)
        payload = _shape_http_payload(messages, optional_params)
        req_timeout = timeout or 60.0

        try:
            if isinstance(client, HTTPHandler):
                r = client.post(f"{base}/bridge/complete", json=payload, headers=headers or None, timeout=req_timeout)
                if not (200 <= getattr(r, "status_code", 200) < 300):
                    raise CustomLLMError(status_code=getattr(r, "status_code", 500), message=getattr(r, "text", "")[:400])
            else:
                with httpx.Client(timeout=req_timeout, headers=headers) as c:
                    r = c.post(f"{base}/bridge/complete", json=payload)
                    r.raise_for_status()
            data = r.json()
        except CustomLLMError:
            raise
        except httpx.HTTPStatusError as e:
            raise CustomLLMError(status_code=e.response.status_code, message=str(e)[:400])
        except Exception as e:
            raise CustomLLMError(status_code=500, message=f"HTTP bridge error: {e}"[:400])

        model_response.model = model
        model_response.choices[0].message.content = _render_content(data, optional_params)
        model_response.choices[0].message.role = "assistant"
        _attach_results(model_response, data)
        return model_response

    async def acompletion(
        self,
        model: str,
        messages: list,
        api_base: Optional[str],
        custom_prompt_dict: dict,
        model_response: ModelResponse,
        print_verbose: Callable,
        encoding,
        api_key,
        logging_obj,
        optional_params: dict,
        acompletion=None,
        litellm_params=None,
        logger_fn=None,
        headers: dict = {},
        timeout: Optional[Union[float, httpx.Timeout]] = None,
        client: Optional[AsyncHTTPHandler] = None,
    ) -> ModelResponse:
        # Try direct mode first
        if _use_direct_mode():
            requirements = optional_params.get("items") or optional_params.get("lean4_requirements") or []
            if requirements:
                try:
                    data = await _run_direct_prove(requirements, optional_params)
                    model_response.model = model
                    model_response.choices[0].message.content = _render_content(data, optional_params)
                    model_response.choices[0].message.role = "assistant"
                    _attach_results(model_response, data)
                    return model_response
                except Exception as e:
                    if os.getenv("SCILLM_CERTAINLY_DIRECT_STRICT", "").lower() in ("1", "true"):
                        raise CustomLLMError(status_code=500, message=f"Direct mode failed: {e}")

        # HTTP fallback mode
        base = _resolve_base(api_base)
        payload = _shape_http_payload(messages, optional_params)
        req_timeout = timeout or 60.0

        try:
            if isinstance(client, AsyncHTTPHandler):
                r = await client.post(f"{base}/bridge/complete", json=payload, headers=headers or None, timeout=req_timeout)
                if not (200 <= getattr(r, "status_code", 200) < 300):
                    raise CustomLLMError(status_code=getattr(r, "status_code", 500), message=getattr(r, "text", "")[:400])
            else:
                async with httpx.AsyncClient(timeout=req_timeout, headers=headers) as c:
                    r = await c.post(f"{base}/bridge/complete", json=payload)
                    r.raise_for_status()
            data = r.json()
        except CustomLLMError:
            raise
        except httpx.HTTPStatusError as e:
            raise CustomLLMError(status_code=e.response.status_code, message=str(e)[:400])
        except Exception as e:
            raise CustomLLMError(status_code=500, message=f"HTTP bridge error: {e}"[:400])

        model_response.model = model
        model_response.choices[0].message.content = _render_content(data, optional_params)
        model_response.choices[0].message.role = "assistant"
        _attach_results(model_response, data)
        return model_response


# ---------------------------------------------------------------------------
# Provider registration
# ---------------------------------------------------------------------------

try:
    if os.getenv("LITELLM_ENABLE_LEAN4", "") == "1" or os.getenv("SCILLM_ENABLE_LEAN4", "") == "1":
        try:
            from litellm.llms import PROVIDER_REGISTRY
            PROVIDER_REGISTRY["lean4"] = Lean4LLM
        except Exception:
            try:
                from litellm.llms.custom_llm import register_custom_provider
                register_custom_provider("lean4", Lean4LLM)
            except Exception:
                pass
except Exception:
    pass
