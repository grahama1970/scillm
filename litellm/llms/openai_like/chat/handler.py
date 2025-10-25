"""
OpenAI-like chat completion handler

For handling OpenAI-like chat completions, like IBM WatsonX, etc.
"""

import json
import os
from typing import Any, Callable, Optional, Union, Tuple
import time

import httpx

import litellm
from litellm import LlmProviders
from litellm.llms.bedrock.chat.invoke_handler import MockResponseIterator
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, HTTPHandler
from litellm.llms.databricks.streaming_utils import ModelResponseIterator
from litellm.llms.openai.chat.gpt_transformation import OpenAIGPTConfig
from litellm.llms.openai.openai import OpenAIConfig
from litellm.types.utils import CustomStreamingDecoder, ModelResponse
from litellm.utils import CustomStreamWrapper, ProviderConfigManager

from ..common_utils import OpenAILikeBase, OpenAILikeError
from litellm.exceptions import AuthenticationError, NotFoundError, RateLimitError
from .transformation import OpenAILikeChatConfig
from litellm.secret_managers.main import get_secret_bool
def _bool_env(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return str(val).strip().lower() in {"1","true","yes","on"}


def _scillm_transport_name(api_base: str) -> str:
    # OpenAI-compatible non-OpenAI bases use HTTPX transport here
    return "httpx-oai-compatible"


def _scillm_detect_auth_style(h: dict) -> str:
    auth = h.get("Authorization")
    if isinstance(auth, str) and auth.startswith("Bearer"):
        return "bearer"
    if "x-api-key" in h:
        return "x-api-key"
    return "none"


def _scillm_apply_auth_policy(api_base: str, headers: dict) -> Tuple[dict, str]:
    """
    SCILLM paved-path auth for OpenAI-compatible gateways:
    - SCILLM_SAFE_MODE=1 (default): no mutation, only detect style.
    - When SCILLM_SAFE_MODE!=1 and SCILLM_ENABLE_AUTO_AUTH=1:
        Prefer x-api-key for non api.openai.com; convert Bearer->x-api-key.
    Never log secrets; only style names are surfaced for DEBUG.
    """
    # Defaults: SAFE_MODE=True (no mutation), ENABLE_AUTO_AUTH=False
    safe_mode = _bool_env("SCILLM_SAFE_MODE", True)
    enable_auto_auth = _bool_env("SCILLM_ENABLE_AUTO_AUTH", False)
    new_headers = dict(headers or {})
    is_openai = "api.openai.com" in (api_base or "")

    if safe_mode:
        return new_headers, _scillm_detect_auth_style(new_headers)

    if not is_openai:
        # Prefer x-api-key on non-OpenAI bases
        if "x-api-key" in new_headers:
            return new_headers, "x-api-key"
        auth = new_headers.get("Authorization")
        if isinstance(auth, str) and auth.startswith("Bearer") and enable_auto_auth:
            token = auth.split(" ", 1)[-1]
            new_headers.pop("Authorization", None)
            new_headers["x-api-key"] = token
            return new_headers, "x-api-key"

    # Default: no mutation, return detected style
    return new_headers, _scillm_detect_auth_style(new_headers)


# --- SciLLM: minimal preflight (opt-in, cached) ---
_SC_PREFLIGHT_CACHE: dict[str, float] = {}

def _scillm_preflight(api_base: str, headers: dict, model: str) -> None:
    """
    If SCILLM_PREFLIGHT=1, do a tiny JSON echo to verify Chat is reachable.
    Accept 200 or 429 (capacity). Fail on 401/403/404/5xx.
    Cache per base for 300s.
    """
    try:
        # Default ON for non-openai.com bases; can be disabled by SCILLM_PREFLIGHT=0
        base = (api_base or "").strip()
        default_on = ("api.openai.com" not in base)
        if not _bool_env("SCILLM_PREFLIGHT", default_on):
            return
        now = time.time()
        if _SC_PREFLIGHT_CACHE.get(base, 0) > now:
            return
        client = HTTPHandler(timeout=httpx.Timeout(timeout=20.0, connect=5.0))
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": 'Return only {"ok":true} as JSON.'}],
            "response_format": {"type": "json_object"},
            "max_tokens": 8,
            "temperature": 0,
        }
        resp = client.post(url=base, headers=headers, data=json.dumps(payload))
        code = resp.status_code
        if code in (200, 429):
            # TTL 300s
            _SC_PREFLIGHT_CACHE[base] = now + 300.0
            return
        # Map helpful message
        try:
            msg = resp.text
        except Exception:
            msg = str(resp)
        raise OpenAILikeError(status_code=code, message=f"preflight_failed: {msg}")
    except OpenAILikeError:
        raise
    except Exception as e:
        # Non-fatal unknown failure: let main call proceed
        return


def _scillm_guard_headers(headers: dict) -> None:
    """Fail fast on obviously bad Authorization/x-api-key values (quoted/nested)."""
    for k in ("Authorization", "x-api-key"):
        v = headers.get(k)
        if not isinstance(v, str) or not v:
            continue
        s = v.strip()
        if s.startswith("CHUTES_API_KEY=") or (
            (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'"))
        ):
            raise OpenAILikeError(status_code=400, message=f"invalid auth header for {k}: looks quoted or nested; fix your .env")


async def make_call(
    client: Optional[AsyncHTTPHandler],
    api_base: str,
    headers: dict,
    data: str,
    model: str,
    messages: list,
    logging_obj,
    streaming_decoder: Optional[CustomStreamingDecoder] = None,
    fake_stream: bool = False,
):
    if client is None:
        client = litellm.module_level_aclient

    response = await client.post(
        api_base, headers=headers, data=data, stream=not fake_stream
    )

    if streaming_decoder is not None:
        completion_stream: Any = streaming_decoder.aiter_bytes(
            response.aiter_bytes(chunk_size=1024)
        )
    elif fake_stream:
        model_response = ModelResponse(**response.json())
        completion_stream = MockResponseIterator(model_response=model_response)
    else:
        # Pass the response object so iterator can close it when finished
        completion_stream = ModelResponseIterator(
            streaming_response=response.aiter_lines(), sync_stream=False, response_obj=response
        )
    # LOGGING
    logging_obj.post_call(
        input=messages,
        api_key="",
        original_response=completion_stream,  # Pass the completion stream for logging
        additional_args={"complete_input_dict": data},
    )

    return completion_stream


def make_sync_call(
    client: Optional[HTTPHandler],
    api_base: str,
    headers: dict,
    data: str,
    model: str,
    messages: list,
    logging_obj,
    streaming_decoder: Optional[CustomStreamingDecoder] = None,
    fake_stream: bool = False,
    timeout: Optional[Union[float, httpx.Timeout]] = None,
):
    if client is None:
        client = litellm.module_level_client  # Create a new client if none provided

    response = client.post(
        api_base, headers=headers, data=data, stream=not fake_stream, timeout=timeout
    )

    if response.status_code != 200:
        code = response.status_code
        try:
            msg = response.text
        except Exception:
            msg = str(response)
        if code in (401, 403):
            style = _scillm_detect_auth_style(headers)
            hint = f"AuthError(header_style={style}); check .env and header style for non-openai base"
            raise AuthenticationError(
                message=hint if not msg else msg,
                llm_provider="openai_like",
                model=model,
                response=response,
            )
        if code == 404:
            raise NotFoundError(
                message=f"model not found: {model}",
                model=model,
                llm_provider="openai_like",
                response=response,
            )
        if code == 429:
            raise RateLimitError(
                message="capacity or rate limit",
                llm_provider="openai_like",
                model=model,
                response=response,
            )
        raise OpenAILikeError(status_code=code, message=msg)

    if streaming_decoder is not None:
        completion_stream = streaming_decoder.iter_bytes(
            response.iter_bytes(chunk_size=1024)
        )
    elif fake_stream:
        model_response = ModelResponse(**response.json())
        completion_stream = MockResponseIterator(model_response=model_response)
    else:
        completion_stream = ModelResponseIterator(
            streaming_response=response.iter_lines(), sync_stream=True
        )

    # LOGGING
    logging_obj.post_call(
        input=messages,
        api_key="",
        original_response="first stream response received",
        additional_args={"complete_input_dict": data},
    )

    return completion_stream


class OpenAILikeChatHandler(OpenAILikeBase):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def acompletion_stream_function(
        self,
        model: str,
        messages: list,
        custom_llm_provider: str,
        api_base: str,
        custom_prompt_dict: dict,
        model_response: ModelResponse,
        print_verbose: Callable,
        encoding,
        api_key,
        logging_obj,
        stream,
        data: dict,
        optional_params=None,
        litellm_params=None,
        logger_fn=None,
        headers={},
        client: Optional[AsyncHTTPHandler] = None,
        streaming_decoder: Optional[CustomStreamingDecoder] = None,
        fake_stream: bool = False,
    ) -> CustomStreamWrapper:
        data["stream"] = True
        completion_stream = await make_call(
            client=client,
            api_base=api_base,
            headers=headers,
            data=json.dumps(data),
            model=model,
            messages=messages,
            logging_obj=logging_obj,
            streaming_decoder=streaming_decoder,
        )
        streamwrapper = CustomStreamWrapper(
            completion_stream=completion_stream,
            model=model,
            custom_llm_provider=custom_llm_provider,
            logging_obj=logging_obj,
        )
        # Attach client so downstream can close it when stream ends
        try:
            setattr(streamwrapper, "_sc_client", client)
        except Exception:
            pass

        return streamwrapper

    async def acompletion_function(
        self,
        model: str,
        messages: list,
        api_base: str,
        custom_prompt_dict: dict,
        model_response: ModelResponse,
        custom_llm_provider: str,
        print_verbose: Callable,
        client: Optional[AsyncHTTPHandler],
        encoding,
        api_key,
        logging_obj,
        stream,
        data: dict,
        base_model: Optional[str],
        optional_params: dict,
        litellm_params=None,
        logger_fn=None,
        headers={},
        timeout: Optional[Union[float, httpx.Timeout]] = None,
        json_mode: bool = False,
    ) -> ModelResponse:
        if timeout is None:
            timeout = httpx.Timeout(timeout=600.0, connect=5.0)

        if client is None:
            client = litellm.module_level_aclient

        try:
            response = await client.post(
                api_base, headers=headers, data=json.dumps(data), timeout=timeout
            )
            if response.status_code != 200:
                code = response.status_code
                text = response.text
                if code in (401, 403):
                    style = _scillm_detect_auth_style(headers)
                    hint = f"AuthError(header_style={style}); check .env and header style for non-openai base"
                    raise AuthenticationError(
                        message=hint if text is None or text == "" else text,
                        llm_provider="openai_like",
                        model=model,
                        response=response,
                    )
                if code == 404:
                    raise NotFoundError(
                        message=f"model not found: {model}",
                        model=model,
                        llm_provider="openai_like",
                        response=response,
                    )
                if code == 429:
                    raise RateLimitError(
                        message="capacity or rate limit",
                        llm_provider="openai_like",
                        model=model,
                        response=response,
                    )
                raise OpenAILikeError(status_code=code, message=text)
        except httpx.TimeoutException:
            raise OpenAILikeError(status_code=408, message="Timeout error occurred.")
        except Exception as e:
            # Preserve mapped LiteLLM exceptions; only wrap unknowns
            if isinstance(e, (AuthenticationError, NotFoundError, RateLimitError)):
                raise
            raise OpenAILikeError(status_code=500, message=str(e))

        return OpenAILikeChatConfig._transform_response(
            model=model,
            response=response,
            model_response=model_response,
            stream=stream,
            logging_obj=logging_obj,
            optional_params=optional_params,
            api_key=api_key,
            data=data,
            messages=messages,
            print_verbose=print_verbose,
            encoding=encoding,
            json_mode=json_mode,
            custom_llm_provider=custom_llm_provider,
            base_model=base_model,
        )

    def completion(
        self,
        *,
        model: str,
        messages: list,
        api_base: str,
        custom_llm_provider: str,
        custom_prompt_dict: dict,
        model_response: ModelResponse,
        print_verbose: Callable,
        encoding,
        api_key: Optional[str],
        logging_obj,
        optional_params: dict,
        acompletion=None,
        litellm_params: dict = {},
        logger_fn=None,
        headers: Optional[dict] = None,
        timeout: Optional[Union[float, httpx.Timeout]] = None,
        client: Optional[Union[HTTPHandler, AsyncHTTPHandler]] = None,
        custom_endpoint: Optional[bool] = None,
        streaming_decoder: Optional[
            CustomStreamingDecoder
        ] = None,  # if openai-compatible api needs custom stream decoder - e.g. sagemaker
        fake_stream: bool = False,
    ):
        custom_endpoint = custom_endpoint or optional_params.pop(
            "custom_endpoint", None
        )
        base_model: Optional[str] = optional_params.pop("base_model", None)
        api_base, headers = self._validate_environment(
            api_base=api_base,
            api_key=api_key,
            endpoint_type="chat_completions",
            custom_endpoint=custom_endpoint,
            headers=headers,
        )
        # SCILLM: apply paved-path auth policy for HTTPX OpenAI-compatible transport
        # Do not log secrets; only capture style names.
        headers, __sc_auth_style = _scillm_apply_auth_policy(api_base=api_base, headers=headers)
        __sc_transport = _scillm_transport_name(api_base)
        # Ensure required content headers are present when caller supplied headers
        if headers is None:
            headers = {}
        if "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"
        # Optional but helpful for some gateways
        headers.setdefault("Accept", "application/json")
        # Guard clearly bad headers (quoted/nested)
        _scillm_guard_headers(headers)
        # Optional preflight (cached)
        _scillm_preflight(api_base=api_base, headers=headers, model=model)

        stream: bool = optional_params.pop("stream", None) or False
        extra_body = optional_params.pop("extra_body", {})
        json_mode = optional_params.pop("json_mode", None)
        optional_params.pop("max_retries", None)
        if not fake_stream:
            optional_params["stream"] = stream

        if messages is not None and custom_llm_provider is not None:
            provider_config = ProviderConfigManager.get_provider_chat_config(
                model=model, provider=LlmProviders(custom_llm_provider)
            )
            if isinstance(provider_config, OpenAIGPTConfig) or isinstance(
                provider_config, OpenAIConfig
            ):
                messages = provider_config._transform_messages(
                    messages=messages, model=model
                )

        data = {
            "model": model,
            "messages": messages,
            **optional_params,
            **extra_body,
        }

        ## LOGGING
        # Do not log raw headers. Optionally include names-only meta if SCILLM_DEBUG_META=1.
        _debug_meta: dict = {}
        try:
            if get_secret_bool("SCILLM_DEBUG_META", default_value=False):
                _auth_style = "none"
                try:
                    _hdrs = headers or {}
                    if "x-api-key" in _hdrs:
                        _auth_style = "x-api-key"
                    elif "Authorization" in _hdrs:
                        _auth_style = "authorization-bearer"
                except Exception:
                    pass
                _debug_meta = {
                    "scillm_transport": "openai_like",
                    "scillm_auth_style": _auth_style,
                }
        except Exception:
            _debug_meta = {}

        logging_obj.pre_call(
            input=messages,
            api_key=api_key,
            additional_args={
                "complete_input_dict": data,
                "api_base": api_base,
                **_debug_meta,
            },
        )
        if acompletion is True:
            if client is None or not isinstance(client, AsyncHTTPHandler):
                client = None
            if (
                stream is True
            ):  # if function call - fake the streaming (need complete blocks for output parsing in openai format)
                data["stream"] = stream
                return self.acompletion_stream_function(
                    model=model,
                    messages=messages,
                    data=data,
                    api_base=api_base,
                    custom_prompt_dict=custom_prompt_dict,
                    model_response=model_response,
                    print_verbose=print_verbose,
                    encoding=encoding,
                    api_key=api_key,
                    logging_obj=logging_obj,
                    optional_params=optional_params,
                    stream=stream,
                    litellm_params=litellm_params,
                    logger_fn=logger_fn,
                    headers=headers,
                    client=client,
                    custom_llm_provider=custom_llm_provider,
                    streaming_decoder=streaming_decoder,
                    fake_stream=fake_stream,
                )
            else:
                return self.acompletion_function(
                    model=model,
                    messages=messages,
                    data=data,
                    api_base=api_base,
                    custom_prompt_dict=custom_prompt_dict,
                    custom_llm_provider=custom_llm_provider,
                    model_response=model_response,
                    print_verbose=print_verbose,
                    encoding=encoding,
                    api_key=api_key,
                    logging_obj=logging_obj,
                    optional_params=optional_params,
                    stream=stream,
                    litellm_params=litellm_params,
                    logger_fn=logger_fn,
                    headers=headers,
                    timeout=timeout,
                    base_model=base_model,
                    client=client,
                    json_mode=json_mode,
                )
        else:
            ## COMPLETION CALL
            if stream is True:
                completion_stream = make_sync_call(
                    client=(
                        client
                        if client is not None and isinstance(client, HTTPHandler)
                        else None
                    ),
                    api_base=api_base,
                    headers=headers,
                    data=json.dumps(data),
                    model=model,
                    messages=messages,
                    logging_obj=logging_obj,
                    streaming_decoder=streaming_decoder,
                    fake_stream=fake_stream,
                    timeout=timeout,
                )
                # completion_stream.__iter__()
                return CustomStreamWrapper(
                    completion_stream=completion_stream,
                    model=model,
                    custom_llm_provider=custom_llm_provider,
                    logging_obj=logging_obj,
                )
            else:
                if client is None or not isinstance(client, HTTPHandler):
                    client = HTTPHandler(timeout=timeout)  # type: ignore
                try:
                    response = client.post(
                        url=api_base, headers=headers, data=json.dumps(data)
                    )
                    if response.status_code != 200:
                        code = response.status_code
                        text = response.text
                        if code in (401, 403):
                            # Map to AuthenticationError with header style hint
                            style = _scillm_detect_auth_style(headers)
                            hint = f"AuthError(header_style={style}); check .env and header style for non-openai base"
                            raise AuthenticationError(
                                message=hint if text is None or text == "" else text,
                                llm_provider="openai_like",
                                model=model,
                                response=response,
                            )
                        if code == 404:
                            raise NotFoundError(
                                message=f"model not found: {model}",
                                model=model,
                                llm_provider="openai_like",
                                response=response,
                            )
                        if code == 429:
                            raise RateLimitError(
                                message="capacity or rate limit",
                                llm_provider="openai_like",
                                model=model,
                                response=response,
                            )
                        # Fallback to generic
                        raise OpenAILikeError(status_code=code, message=text)
                except httpx.TimeoutException:
                    raise OpenAILikeError(
                        status_code=408, message="Timeout error occurred."
                    )
                except Exception as e:
                    if isinstance(e, (AuthenticationError, NotFoundError, RateLimitError)):
                        raise
                    raise OpenAILikeError(status_code=500, message=str(e))
        return OpenAILikeChatConfig._transform_response(
            model=model,
            response=response,
            model_response=model_response,
            stream=stream,
            logging_obj=logging_obj,
            optional_params=optional_params,
            api_key=api_key,
            data=data,
            messages=messages,
            print_verbose=print_verbose,
            encoding=encoding,
            json_mode=json_mode,
            custom_llm_provider=custom_llm_provider,
            base_model=base_model,
        )
