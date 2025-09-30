from __future__ import annotations

"""
Custom provider: codeworld

Bridges LiteLLM calls to the CodeWorld Bridge API.
- POST /bridge/complete for bounded sync runs (flips to async if budget > threshold)
- On HTTP 202, provider polls /bridge/result/{job_id} until completion or budget expiry

Usage via Router model_list:
    Router(model_list=[{
        "model_name": "codeworld",
        "litellm_params": {
            "model": "codeworld",
            "custom_llm_provider": "codeworld",
            # Optional (or env):
            # "api_base": "http://127.0.0.1:8000",
            # "api_key": os.getenv("CODEWORLD_TOKEN"),
        }
    }])

Env fallbacks:
- CODEWORLD_BASE  (default http://127.0.0.1:8000)
- CODEWORLD_TOKEN (Bearer token if server enforces auth)
"""
import os
import time
import asyncio
from typing import Any, Dict, Optional, Union, Callable

import httpx

from litellm.llms.custom_llm import CustomLLM, CustomLLMError, register_custom_provider
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, HTTPHandler
from litellm.utils import ModelResponse

DEFAULT_BASE = os.getenv("CODEWORLD_BASE", "http://127.0.0.1:8000").rstrip("/")


class CodeWorldLLM(CustomLLM):
    def __init__(self) -> None:
        super().__init__()

    def _resolve_base(self, api_base: Optional[str]) -> str:
        return (api_base or DEFAULT_BASE).rstrip("/")

    @staticmethod
    def _headers(api_key: Optional[str]) -> Dict[str, str]:
        h: Dict[str, str] = {"Content-Type": "application/json"}
        token = api_key or os.getenv("CODEWORLD_TOKEN")
        if token and not any(k.lower() == "authorization" for k in h):
            h["Authorization"] = f"Bearer {token}"
        return h

    def _build_payload(self, _model: str, _messages: list, optional_params: dict) -> Dict[str, Any]:
        p: Dict[str, Any] = {
            "messages": _messages,
            "codeworld_metrics": optional_params.get(
                "codeworld_metrics", ["correctness", "robustness", "speed", "brevity"]
            ),
            "codeworld_iterations": int(optional_params.get("codeworld_iterations", 3)),
            "codeworld_allowed_languages": optional_params.get("codeworld_allowed_languages", []),
            "request_timeout": float(optional_params.get("request_timeout", 60.0)),
        }
        if optional_params.get("temperature") is not None:
            p["temperature"] = float(optional_params["temperature"])
        if optional_params.get("seed") is not None:
            p["seed"] = int(optional_params["seed"])
        if optional_params.get("return_artifacts") is not None:
            p["return_artifacts"] = bool(optional_params["return_artifacts"])
        return p

    def _map_response(self, model_response: ModelResponse, data: Dict[str, Any], model: str) -> ModelResponse:
        summary = data.get("summary") or ""
        additional = data.get("additional") or {}
        model_response.model = model
        try:
            model_response.choices[0].message.content = summary  # type: ignore[attr-defined]
            model_response.choices[0].message.role = "assistant"  # type: ignore[attr-defined]
        except Exception:
            model_response.choices[0].message = {"role": "assistant", "content": summary}  # type: ignore[assignment]
        # Expose additional fields via additional_kwargs["codeworld"]
        try:
            if hasattr(model_response, "additional_kwargs") and isinstance(model_response.additional_kwargs, dict):  # type: ignore[attr-defined]
                model_response.additional_kwargs.setdefault("codeworld", additional)  # type: ignore[attr-defined]
            else:
                setattr(model_response, "additional_kwargs", {"codeworld": additional})
        except Exception:
            pass
        return model_response

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
        base = self._resolve_base(api_base)
        payload = self._build_payload(model, messages, optional_params or {})
        hdr = {**headers, **self._headers(api_key)} if headers else self._headers(api_key)
        budget = float(payload.get("request_timeout", 60.0))
        try:
            if isinstance(client, HTTPHandler):
                r = client.post(
                    f"{base}/bridge/complete", json=payload, headers=hdr, timeout=budget + 30.0
                )
                status = getattr(r, "status_code", 0)
                data = r.json() if hasattr(r, "json") else {}
            else:
                with httpx.Client(timeout=budget + 30.0, headers=hdr) as c:
                    resp = c.post(f"{base}/bridge/complete", json=payload)
                    status = resp.status_code
                    data = resp.json() if status in (200, 202) else {}
        except Exception as e:
            raise CustomLLMError(status_code=500, message=str(e)[:400])

        if status == 200:
            return self._map_response(model_response, data, model)
        if status == 202:
            # Poll with exponential backoff up to 10s
            result_url = data.get("result_url") or ""
            t_end = time.time() + budget + 30.0
            backoff = 0.5
            while time.time() < t_end:
                try:
                    with httpx.Client(timeout=10.0, headers=hdr) as c:
                        rr = c.get(base + result_url) if result_url else None
                        if rr is not None and rr.status_code == 200:
                            return self._map_response(model_response, rr.json(), model)
                except Exception:
                    pass
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 10.0)
            raise CustomLLMError(status_code=504, message="CodeWorld job did not complete within budget")
        # Error path
        try:
            msg = data.get("error") or str(data)[:200]
        except Exception:
            msg = f"CodeWorld bridge error (status {status})"
        raise CustomLLMError(status_code=status or 500, message=msg)

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
        base = self._resolve_base(api_base)
        payload = self._build_payload(model, messages, optional_params or {})
        hdr = {**headers, **self._headers(api_key)} if headers else self._headers(api_key)
        budget = float(payload.get("request_timeout", 60.0))
        try:
            if isinstance(client, AsyncHTTPHandler):
                r = await client.post(
                    f"{base}/bridge/complete", json=payload, headers=hdr, timeout=budget + 30.0
                )
                status = getattr(r, "status_code", 0)
                data = r.json() if hasattr(r, "json") else {}
            else:
                async with httpx.AsyncClient(timeout=budget + 30.0, headers=hdr) as c:
                    resp = await c.post(f"{base}/bridge/complete", json=payload)
                    status = resp.status_code
                    data = resp.json() if status in (200, 202) else {}
        except Exception as e:
            raise CustomLLMError(status_code=500, message=str(e)[:400])

        if status == 200:
            return self._map_response(model_response, data, model)
        if status == 202:
            result_url = data.get("result_url") or ""
            t_end = time.time() + budget + 30.0
            async with httpx.AsyncClient(timeout=10.0, headers=hdr) as c:
                backoff = 0.5
                while time.time() < t_end:
                    try:
                        rr = await c.get(base + result_url) if result_url else None
                        if rr is not None and rr.status_code == 200:
                            return self._map_response(model_response, rr.json(), model)
                    except Exception:
                        pass
                    await asyncio.sleep(backoff)  # type: ignore
                    backoff = min(backoff * 2.0, 10.0)
            raise CustomLLMError(status_code=504, message="CodeWorld job did not complete within budget")
        try:
            msg = data.get("error") or str(data)[:200]
        except Exception:
            msg = f"CodeWorld bridge error (status {status})"
        raise CustomLLMError(status_code=status or 500, message=msg)


# Register provider so Router can resolve custom_llm_provider="codeworld"
try:
    register_custom_provider("codeworld", CodeWorldLLM)
except Exception:
    pass
