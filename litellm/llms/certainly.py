from __future__ import annotations

import os
from typing import Optional, Union, Callable, Dict, Any

import httpx

from litellm.llms.custom_llm import CustomLLM
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, HTTPHandler
from litellm.utils import ModelResponse


class CertainlyLLM(CustomLLM):
    """Minimal adapter for the umbrella 'certainly' provider.

    For alpha, delegates to Lean4 while normalizing backend selection and
    ensuring additional_kwargs attaches under 'certainly' (and optionally 'lean4').
    """

    def _ensure_backend(self, optional_params: dict | None) -> dict:
        opt = dict(optional_params or {})
        backend = opt.get("backend") or os.getenv("CERTAINLY_BACKEND") or "lean4"
        try:
            opt["backend"] = str(backend).lower()
        except Exception:
            opt["backend"] = "lean4"
        return opt

    def _normalize_additional(self, model_response: ModelResponse) -> None:
        try:
            model_response.additional_kwargs = getattr(model_response, "additional_kwargs", {}) or {}
            attach_both = os.getenv("LITELLM_CERTAINLY_ATTACH_BOTH", "1") == "1"
            if "certainly" not in model_response.additional_kwargs and "lean4" in model_response.additional_kwargs:
                model_response.additional_kwargs["certainly"] = model_response.additional_kwargs["lean4"]
            if not attach_both and "lean4" in model_response.additional_kwargs:
                try:
                    del model_response.additional_kwargs["lean4"]
                except Exception:
                    pass
        except Exception:
            pass

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
        from .lean4 import Lean4LLM

        delegate = Lean4LLM()
        opt = self._ensure_backend(optional_params)
        resp = delegate.completion(
            model=model,
            messages=messages,
            api_base=api_base,
            custom_prompt_dict=custom_prompt_dict,
            model_response=model_response,
            print_verbose=print_verbose,
            encoding=encoding,
            api_key=api_key,
            logging_obj=logging_obj,
            optional_params=opt,
            acompletion=acompletion,
            litellm_params=litellm_params,
            logger_fn=logger_fn,
            headers=headers,
            timeout=timeout,
            client=client,
        )
        self._normalize_additional(resp)
        return resp

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
        from .lean4 import Lean4LLM

        delegate = Lean4LLM()
        opt = self._ensure_backend(optional_params)
        resp = await delegate.acompletion(
            model=model,
            messages=messages,
            api_base=api_base,
            custom_prompt_dict=custom_prompt_dict,
            model_response=model_response,
            print_verbose=print_verbose,
            encoding=encoding,
            api_key=api_key,
            logging_obj=logging_obj,
            optional_params=opt,
            acompletion=acompletion,
            litellm_params=litellm_params,
            logger_fn=logger_fn,
            headers=headers,
            timeout=timeout,
            client=client,
        )
        self._normalize_additional(resp)
        return resp

