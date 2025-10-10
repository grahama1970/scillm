from __future__ import annotations

"""
Experimental env-gated provider: lean4

Intent
- Let Router users call Lean4 via a familiar LiteLLM provider surface.
- Posts to the Lean4 bridge `/bridge/complete` and returns a ModelResponse with
  a concise textual summary; attaches full results in `additional_kwargs['lean4']`.

Enable with: LITELLM_ENABLE_LEAN4=1
"""

import os
from typing import Any, Optional, Union, Callable, Dict, List

import httpx

from litellm.llms.custom_llm import CustomLLM, CustomLLMError
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, HTTPHandler
from litellm.utils import ModelResponse


def _resolve_base(api_base: Optional[str]) -> str:
    base = (
        api_base
        or os.getenv("CERTAINLY_BRIDGE_BASE")
        or os.getenv("LEAN4_BRIDGE_BASE")
        or "http://127.0.0.1:8787"
    )
    return base.rstrip("/")


def _shape_payload(messages: list, optional_params: dict | None) -> Dict[str, Any]:
    opt = dict(optional_params or {})
    # Optional multi-prover hint (placeholder): backend = "lean4" | "coq"
    backend = (opt.pop("backend", None) or os.getenv("CERTAINLY_BACKEND") or "lean4").lower()
    # Accept either 'lean4_requirements' or generic 'items'
    requirements = opt.pop("lean4_requirements", None) or opt.pop("items", None)
    if not isinstance(requirements, list) or not requirements:
        raise CustomLLMError(status_code=400, message="lean4 provider requires 'lean4_requirements' or 'items' list")
    flags = opt.pop("lean4_flags", None) or opt.pop("flags", None)
    payload: Dict[str, Any] = {"messages": messages, "lean4_requirements": requirements, "backend": backend}
    if isinstance(flags, list) and flags:
        payload["lean4_flags"] = flags
    # Pass through max_seconds if provided
    if "max_seconds" in opt:
        try:
            payload["max_seconds"] = float(opt.pop("max_seconds"))
        except Exception:
            pass
    # Forward options.session_id/track_id for parity with CodeWorld manifests
    options = {}
    try:
        if isinstance(opt.get("options"), dict):
            options.update(opt.pop("options"))
    except Exception:
        pass
    session_id = opt.pop("session_id", None)
    track_id = opt.pop("track_id", None)
    if session_id is not None:
        options["session_id"] = session_id
    if track_id is not None:
        options["track_id"] = track_id
    if options:
        payload["options"] = options
    return payload


def _summarize(resp_json: Dict[str, Any], label: str = "Lean4") -> str:
    try:
        s = resp_json.get("summary", {}) or {}
        items = s.get("items")
        proved = s.get("proved")
        failed = s.get("failed")
        unproved = s.get("unproved")
        return f"{label}: items={items}, proved={proved}, failed={failed}, unproved={unproved}"
    except Exception:
        return f"{label}: completed (see additional_kwargs.certainly)"


class Lean4LLM(CustomLLM):
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
        base = _resolve_base(api_base)
        payload = _shape_payload(messages, optional_params)
        req_timeout: Optional[Union[float, httpx.Timeout]] = timeout or 60.0
        try:
            if isinstance(client, HTTPHandler):
                r = client.post(f"{base}/bridge/complete", json=payload, headers=headers or None, timeout=req_timeout)
                if getattr(r, "status_code", 200) < 200 or getattr(r, "status_code", 200) >= 300:
                    # some injected handlers may not raise; normalize here
                    body = getattr(r, "text", "")
                    raise CustomLLMError(status_code=getattr(r, "status_code", 500), message=str(body)[:400])
            else:
                with httpx.Client(timeout=req_timeout, headers=headers) as c:
                    r = c.post(f"{base}/bridge/complete", json=payload)
                    if r.status_code < 200 or r.status_code >= 300:
                        raise CustomLLMError(status_code=r.status_code, message=r.text[:400])
            data = r.json()
        except CustomLLMError:
            raise
        except Exception as e:  # pragma: no cover
            raise CustomLLMError(status_code=500, message=str(e)[:400])

        backend_label = payload.get("backend") or "lean4"
        label = "Certainly" if backend_label == "lean4" else f"Certainly/{backend_label}"
        text = _summarize(data if isinstance(data, dict) else {}, label=label)
        model_response.model = model
        try:
            model_response.choices[0].message.content = text  # type: ignore[attr-defined]
            model_response.choices[0].message.role = "assistant"  # type: ignore[attr-defined]
        except Exception:
            model_response.choices[0].message = {"role": "assistant", "content": text}  # type: ignore[assignment]
        # Attach full payload
        try:
            model_response.additional_kwargs = getattr(model_response, "additional_kwargs", {}) or {}
            attach_both = os.getenv("LITELLM_CERTAINLY_ATTACH_BOTH", "1") == "1"
            # Always attach under 'certainly'
            model_response.additional_kwargs["certainly"] = data
            if attach_both:
                model_response.additional_kwargs["lean4"] = data
        except Exception:
            pass
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
        base = _resolve_base(api_base)
        payload = _shape_payload(messages, optional_params)
        req_timeout: Optional[Union[float, httpx.Timeout]] = timeout or 60.0
        try:
            if isinstance(client, AsyncHTTPHandler):
                r = await client.post(f"{base}/bridge/complete", json=payload, headers=headers or None, timeout=req_timeout)
                if getattr(r, "status_code", 200) < 200 or getattr(r, "status_code", 200) >= 300:
                    body = getattr(r, "text", "")
                    raise CustomLLMError(status_code=getattr(r, "status_code", 500), message=str(body)[:400])
            else:
                async with httpx.AsyncClient(timeout=req_timeout, headers=headers) as c:
                    r = await c.post(f"{base}/bridge/complete", json=payload)
                    if r.status_code < 200 or r.status_code >= 300:
                        raise CustomLLMError(status_code=r.status_code, message=r.text[:400])
            data = r.json()
        except CustomLLMError:
            raise
        except Exception as e:  # pragma: no cover
            raise CustomLLMError(status_code=500, message=str(e)[:400])

        backend_label = payload.get("backend") or "lean4"
        label = "Certainly" if backend_label == "lean4" else f"Certainly/{backend_label}"
        text = _summarize(data if isinstance(data, dict) else {}, label=label)
        model_response.model = model
        try:
            model_response.choices[0].message.content = text  # type: ignore[attr-defined]
            model_response.choices[0].message.role = "assistant"  # type: ignore[attr-defined]
        except Exception:
            model_response.choices[0].message = {"role": "assistant", "content": text}  # type: ignore[assignment]
        try:
            model_response.additional_kwargs = getattr(model_response, "additional_kwargs", {}) or {}
            attach_both = os.getenv("LITELLM_CERTAINLY_ATTACH_BOTH", "1") == "1"
            model_response.additional_kwargs["certainly"] = data
            if attach_both:
                model_response.additional_kwargs["lean4"] = data
        except Exception:
            pass
        return model_response


# --- Optional self-registration (env-gated) -----------------------------------
try:
    if os.getenv("LITELLM_ENABLE_LEAN4", "") == "1":
        try:
            from litellm.llms import PROVIDER_REGISTRY  # type: ignore
            PROVIDER_REGISTRY["lean4"] = Lean4LLM
        except Exception:
            try:
                from litellm.llms.custom_llm import register_custom_provider  # type: ignore
                register_custom_provider("lean4", Lean4LLM)
            except Exception:
                pass
except Exception:
    pass
