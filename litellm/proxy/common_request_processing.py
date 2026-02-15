import asyncio
import time
import os
import json
import logging
import traceback
from datetime import datetime
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncGenerator,
    Callable,
    Literal,
    Optional,
    Tuple,
    Union,
)

try:
    import fastuuid as uuid  # type: ignore
except Exception:
    import uuid as uuid
import httpx
import orjson
from fastapi import HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse

import litellm
from litellm._logging import verbose_proxy_logger
from litellm.constants import (
    DD_TRACER_STREAMING_CHUNK_YIELD_RESOURCE,
    STREAM_SSE_DATA_PREFIX,
)
from litellm.litellm_core_utils.dd_tracing import tracer
from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj
from litellm.litellm_core_utils.safe_json_dumps import safe_dumps
from litellm.proxy._types import ProxyException, UserAPIKeyAuth
from chutes.middleware.budget_guard import preflight_usage, normalize_ratelimit_to_budget, budget_snapshot, payg_snapshot, budget_register_attempt, budget_metadata
from chutes.middleware.metrics import inc_call, inc_429, set_budget
from scillm.telemetry.metrics import record_call as sc_record_call, record_latency as sc_record_latency, set_budget as sc_set_budget, set_spend_today as sc_set_spend_today, set_payg_active as sc_set_payg_active, record_payg_decision as sc_record_payg_decision
from litellm.exceptions import RateLimitError
from litellm.proxy.auth.auth_utils import check_response_size_is_safe
from litellm.proxy.common_utils.callback_utils import (
    get_logging_caching_headers,
    get_remaining_tokens_and_requests_from_request_data,
)
from litellm.proxy.route_llm_request import route_request
from litellm.proxy.utils import ProxyLogging
from litellm.router import Router

if TYPE_CHECKING:
    from litellm.proxy.proxy_server import ProxyConfig as _ProxyConfig

    ProxyConfig = _ProxyConfig
else:
    ProxyConfig = Any
from litellm.proxy.litellm_pre_call_utils import add_litellm_data_to_request


async def _parse_event_data_for_error(event_line: Union[str, bytes]) -> Optional[int]:
    """Parses an event line and returns an error code if present, else None."""
    event_line = (
        event_line.decode("utf-8") if isinstance(event_line, bytes) else event_line
    )
    if event_line.startswith("data: "):
        json_str = event_line[len("data: ") :].strip()
        if not json_str or json_str == "[DONE]":  # handle empty data or [DONE] message
            return None
        try:
            data = orjson.loads(json_str)
            if (
                isinstance(data, dict)
                and "error" in data
                and isinstance(data["error"], dict)
            ):
                error_code_raw = data["error"].get("code")
                error_code: Optional[int] = None

                if isinstance(error_code_raw, int):
                    error_code = error_code_raw
                elif isinstance(error_code_raw, str):
                    try:
                        error_code = int(error_code_raw)
                    except ValueError:
                        verbose_proxy_logger.warning(
                            f"Error code is a string but not a valid integer: {error_code_raw}"
                        )
                        # Not a valid integer string, treat as if no valid code was found for this check
                        pass

                # Ensure error_code is a valid HTTP status code
                if error_code is not None and 100 <= error_code <= 599:
                    return error_code
                elif (
                    error_code_raw is not None
                ):  # Log if original code was present but not valid
                    verbose_proxy_logger.warning(
                        f"Error has invalid or non-convertible code: {error_code_raw}"
                    )
        except (orjson.JSONDecodeError, json.JSONDecodeError):
            # not a known error chunk
            pass
    return None


async def create_streaming_response(
    generator: AsyncGenerator[str, None],
    media_type: str,
    headers: dict,
    default_status_code: int = status.HTTP_200_OK,
) -> StreamingResponse:
    """
    Creates a StreamingResponse by inspecting the first chunk for an error code.
    The entire original generator content is streamed, but the HTTP status code
    of the response is set based on the first chunk if it's a recognized error.
    """
    first_chunk_value: Optional[str] = None
    final_status_code = default_status_code

    try:
        # Handle coroutine that returns a generator
        if asyncio.iscoroutine(generator):
            generator = await generator

        # Now get the first chunk from the actual generator
        first_chunk_value = await generator.__anext__()

        if first_chunk_value is not None:
            try:
                error_code_from_chunk = await _parse_event_data_for_error(
                    first_chunk_value
                )
                if error_code_from_chunk is not None:
                    final_status_code = error_code_from_chunk
                    verbose_proxy_logger.debug(
                        f"Error detected in first stream chunk. Status code set to: {final_status_code}"
                    )
            except Exception as e:
                verbose_proxy_logger.debug(f"Error parsing first chunk value: {e}")

    except StopAsyncIteration:
        # Generator was empty. Default status
        async def empty_gen() -> AsyncGenerator[str, None]:
            if False:
                yield  # type: ignore

        return StreamingResponse(
            empty_gen(),
            media_type=media_type,
            headers=headers,
            status_code=default_status_code,
        )
    except Exception as e:
        # Unexpected error consuming first chunk.
        verbose_proxy_logger.exception(
            f"Error consuming first chunk from generator: {e}"
        )

        # Fallback to a generic error stream
        async def error_gen_message() -> AsyncGenerator[str, None]:
            yield f"data: {json.dumps({'error': {'message': 'Error processing stream start', 'code': status.HTTP_500_INTERNAL_SERVER_ERROR}})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            error_gen_message(),
            media_type=media_type,
            headers=headers,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    async def combined_generator() -> AsyncGenerator[str, None]:
        if first_chunk_value is not None:
            with tracer.trace(DD_TRACER_STREAMING_CHUNK_YIELD_RESOURCE):
                yield first_chunk_value
        async for chunk in generator:
            with tracer.trace(DD_TRACER_STREAMING_CHUNK_YIELD_RESOURCE):
                yield chunk

    return StreamingResponse(
        combined_generator(),
        media_type=media_type,
        headers=headers,
        status_code=final_status_code,
    )


class ProxyBaseLLMRequestProcessing:
    def __init__(self, data: dict):
        self.data = data

    @staticmethod
    def get_custom_headers(
        *,
        user_api_key_dict: UserAPIKeyAuth,
        call_id: Optional[str] = None,
        model_id: Optional[str] = None,
        cache_key: Optional[str] = None,
        api_base: Optional[str] = None,
        version: Optional[str] = None,
        model_region: Optional[str] = None,
        response_cost: Optional[Union[float, str]] = None,
        hidden_params: Optional[dict] = None,
        fastest_response_batch_completion: Optional[bool] = None,
        request_data: Optional[dict] = {},
        timeout: Optional[Union[float, int, httpx.Timeout]] = None,
        **kwargs,
    ) -> dict:
        exclude_values = {"", None, "None"}
        hidden_params = hidden_params or {}
        headers = {
            "x-litellm-call-id": call_id,
            "x-litellm-model-id": model_id,
            "x-litellm-cache-key": cache_key,
            "x-litellm-model-api-base": (
                api_base.split("?")[0] if api_base else None
            ),  # don't include query params, risk of leaking sensitive info
            "x-litellm-version": version,
            "x-litellm-model-region": model_region,
            "x-litellm-response-cost": str(response_cost),
            "x-litellm-key-tpm-limit": str(user_api_key_dict.tpm_limit),
            "x-litellm-key-rpm-limit": str(user_api_key_dict.rpm_limit),
            "x-litellm-key-max-budget": str(user_api_key_dict.max_budget),
            "x-litellm-key-spend": str(user_api_key_dict.spend),
            "x-litellm-response-duration-ms": str(
                hidden_params.get("_response_ms", None)
            ),
            "x-litellm-overhead-duration-ms": str(
                hidden_params.get("litellm_overhead_time_ms", None)
            ),
            "x-litellm-fastest_response_batch_completion": (
                str(fastest_response_batch_completion)
                if fastest_response_batch_completion is not None
                else None
            ),
            "x-litellm-timeout": str(timeout) if timeout is not None else None,
            **{k: str(v) for k, v in kwargs.items()},
        }
        if request_data:
            remaining_tokens_header = (
                get_remaining_tokens_and_requests_from_request_data(request_data)
            )
            headers.update(remaining_tokens_header)

            logging_caching_headers = get_logging_caching_headers(request_data)
            if logging_caching_headers:
                headers.update(logging_caching_headers)

        # Budget headers (if tracker/preflight available, else sensible defaults)
        try:
            snap = budget_snapshot()
            if snap:
                if snap.get("reset_at"):
                    headers["x-budget-reset-at"] = str(snap["reset_at"])
                headers["x-ratelimit-remaining-requests"] = str(snap.get("remaining", ""))
                if snap.get("limit") is not None:
                    headers["x-ratelimit-limit"] = str(snap.get("limit"))
            else:
                # Provide sensible defaults when budget tracker isn't configured
                from chutes.middleware.budget_guard import _get_daily_limit, _next_reset_at_iso_utc
                default_limit = _get_daily_limit()
                headers["x-ratelimit-limit"] = str(default_limit)
                headers["x-ratelimit-remaining-requests"] = str(default_limit)
                headers["x-budget-reset-at"] = _next_reset_at_iso_utc()
        except Exception:
            pass

        try:
            return {
                key: str(value)
                for key, value in headers.items()
                if value not in exclude_values
            }
        except Exception as e:
            verbose_proxy_logger.error(f"Error setting custom headers: {e}")
            return {}

    async def common_processing_pre_call_logic(
        self,
        request: Request,
        general_settings: dict,
        user_api_key_dict: UserAPIKeyAuth,
        proxy_logging_obj: ProxyLogging,
        proxy_config: ProxyConfig,
        route_type: Literal[
            "acompletion",
            "aresponses",
            "_arealtime",
            "aget_responses",
            "adelete_responses",
            "acancel_responses",
            "acreate_batch",
            "aretrieve_batch",
            "afile_content",
            "atext_completion",
            "acreate_fine_tuning_job",
            "acancel_fine_tuning_job",
            "alist_fine_tuning_jobs",
            "aretrieve_fine_tuning_job",
            "alist_input_items",
            "aimage_edit",
            "agenerate_content",
            "agenerate_content_stream",
            "allm_passthrough_route",
            "avector_store_search",
            "avector_store_create",
        ],
        version: Optional[str] = None,
        user_model: Optional[str] = None,
        user_temperature: Optional[float] = None,
        user_request_timeout: Optional[float] = None,
        user_max_tokens: Optional[int] = None,
        user_api_base: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Tuple[dict, LiteLLMLoggingObj]:
        start_time = datetime.now()  # start before calling guardrail hooks
        self.data["_sc_started_at"] = time.monotonic()
        self.data = await add_litellm_data_to_request(
            data=self.data,
            request=request,
            general_settings=general_settings,
            user_api_key_dict=user_api_key_dict,
            version=version,
            proxy_config=proxy_config,
        )

        # Register a local budget attempt early (for consistent remaining/used math)
        try:
            budget_register_attempt()
        except Exception:
            pass

        self.data["model"] = (
            general_settings.get("completion_model", None)  # server default
            or user_model  # model name passed via cli args
            or model  # for azure deployments
            or self.data.get("model", None)  # default passed in http request
        )

        # override with user settings, these are params passed via cli
        if user_temperature:
            self.data["temperature"] = user_temperature
        if user_request_timeout:
            self.data["request_timeout"] = user_request_timeout
        if user_max_tokens:
            self.data["max_tokens"] = user_max_tokens
        if user_api_base:
            self.data["api_base"] = user_api_base

        ### MODEL ALIAS MAPPING ###
        # check if model name in model alias map
        # get the actual model name
        if (
            isinstance(self.data["model"], str)
            and self.data["model"] in litellm.model_alias_map
        ):
            self.data["model"] = litellm.model_alias_map[self.data["model"]]

        # Optional auto vendor selection: 'text-auto' → choose chutes or moonshot
        try:
            if isinstance(self.data.get("model"), str) and self.data.get("model") == "text-auto":
                from chutes.middleware.budget_guard import payg_snapshot  # type: ignore
                from chutes.middleware.pricing import get_model_pricing  # type: ignore

                # Defaults
                selected = "chutes-text"
                pref = (os.getenv("SCILLM_PAYG_VENDOR", "auto").strip().lower())
                allow_payg = (os.getenv("SCILLM_ALLOW_PAYG", "0").strip().lower() in {"1", "true", "yes"})
                max_mult = 1.3
                try:
                    max_mult = float(os.getenv("SCILLM_MAX_COST_MULTIPLIER", "1.3") or 1.3)
                except Exception:
                    max_mult = 1.3

                if pref in {"chutes", "moonshot"}:
                    selected = "moonshot-text" if pref == "moonshot" else "chutes-text"
                else:
                    # auto: if PAYG active and allowed, consider moonshot when price within multiplier
                    ps = payg_snapshot() or {}
                    payg_active = bool(ps.get("payg_active"))
                    if payg_active and allow_payg:
                        chutes_id = os.getenv("CHUTES_TEXT_MODEL", "")
                        moonshot_id = os.getenv("MOONSHOT_MODEL", "")
                        ch_p = get_model_pricing(chutes_id)
                        ms_p = get_model_pricing(moonshot_id)
                        def _sum(p):
                            try:
                                a, b = p
                                return float((a or 0)) + float((b or 0))
                            except Exception:
                                return None
                        ch_cost = _sum(ch_p)
                        ms_cost = _sum(ms_p)
                        if ch_cost and ms_cost:
                            try:
                                ratio = ms_cost / ch_cost if ch_cost > 0 else None
                            except Exception:
                                ratio = None
                            if ratio is not None and ratio <= max_mult:
                                selected = "moonshot-text"
                        else:
                            # If we can't resolve both prices, stay on chutes
                            selected = "chutes-text"
                    else:
                        selected = "chutes-text"
                self.data["model"] = selected
        except Exception:
            # On any error, keep original model
            pass

        self.data["litellm_call_id"] = request.headers.get(
            "x-litellm-call-id", str(uuid.uuid4())
        )
        # Optional preflight: short-circuit if Chutes daily budget exhausted or PAYG confirmation required
        try:
            pf = preflight_usage()
            if pf and pf.get("ok") and int(pf.get("remaining", 0)) <= 0:
                # Budget exhausted path (429)
                cooldown = int(os.getenv("SCILLM_COOLDOWN_429_S", "120") or "120")
                raise ProxyException(
                    message="Provider daily budget exhausted",
                    type="budget_exhausted",
                    param=None,
                    code=429,
                    headers={"Retry-After": str(cooldown)},
                )
            # PAYG policy: if daily limit is reached and policy requires confirmation, block with 402
            ps = payg_snapshot()
            if ps and ps.get("ok") and bool(ps.get("payg_active")):
                cost_today = ps.get("cost_usd_today")
                reset_at = ps.get("reset_at")
                limit_val = ps.get("limit")
                try:
                    sc_set_payg_active("text", True)
                    if isinstance(cost_today, (int, float)):
                        sc_set_spend_today("text", float(cost_today))
                except Exception:
                    pass
                # Strict 'do_not_charge' guard (per-request or env) → always 429 budget_exhausted
                strict_stop = False
                try:
                    strict_stop = bool(self.data.get("do_not_charge", False))
                except Exception:
                    strict_stop = False
                if not strict_stop:
                    strict_stop = (os.getenv("SCILLM_DO_NOT_CHARGE", "0").strip().lower() in {"1", "true", "yes"})
                if strict_stop:
                    sc_record_payg_decision("strict_stop")
                    raise ProxyException(
                        message="Budget exhausted and do_not_charge is set; overage blocked.",
                        type="budget_exhausted",
                        param=None,
                        code=429,
                        headers={
                            "x-payg-active": "1",
                            "x-ratelimit-remaining-requests": "0",
                            "x-ratelimit-limit": (str(limit_val) if limit_val is not None else ""),
                            "x-budget-reset-at": str(reset_at),
                            "Retry-After": os.getenv("SCILLM_COOLDOWN_429_S", "120"),
                        },
                    )
                allow_env = (os.getenv("SCILLM_ALLOW_PAYG", "0").strip().lower() in {"1", "true", "yes"})
                hard_stop = os.getenv("CHUTES_PAYG_HARD_STOP_USD")
                soft_cap = os.getenv("CHUTES_PAYG_MAX_DAILY_USD")
                try:
                    hard_stop_val = float(hard_stop) if hard_stop else None
                except Exception:
                    hard_stop_val = None
                try:
                    soft_cap_val = float(soft_cap) if soft_cap else None
                except Exception:
                    soft_cap_val = None
                # per-request override
                confirm_flag = False
                try:
                    confirm_flag = bool(self.data.get("confirm_payg", False))
                except Exception:
                    confirm_flag = False
                # Hard stop wins
                if isinstance(cost_today, (int, float)) and hard_stop_val is not None and float(cost_today) >= hard_stop_val:
                    sc_record_payg_decision("hard_stop")
                    raise ProxyException(
                        message=f"PAYG hard cap reached (spend={cost_today} USD, cap={hard_stop_val}).",
                        type="payg_hard_stop",
                        param=None,
                        code=402,
                        headers={
                            "x-payg-active": "1",
                            "x-spend-usd-today": str(cost_today),
                            "x-hard-cap-usd": str(hard_stop_val),
                            "x-budget-reset-at": str(reset_at),
                        },
                    )
                # Require confirmation unless globally allowed
                if not (allow_env or confirm_flag):
                    sc_record_payg_decision("blocked")
                    raise ProxyException(
                        message=(
                            "PAYG is active; set SCILLM_ALLOW_PAYG=1 or pass confirm_payg=true to continue now."
                        ),
                        type="payg_confirmation_required",
                        param=None,
                        code=402,
                        headers={
                            "x-payg-active": "1",
                            "x-spend-usd-today": (str(cost_today) if cost_today is not None else ""),
                            "x-soft-cap-usd": (str(soft_cap_val) if soft_cap_val is not None else ""),
                            "x-budget-reset-at": str(reset_at),
                        },
                    )
                # Allowed; soft-cap exceeded is advisory only
                if isinstance(cost_today, (int, float)) and soft_cap_val is not None and float(cost_today) >= soft_cap_val:
                    sc_record_payg_decision("allowed_softcap_exceeded")
        except Exception:
            # Fail open: if preflight not configured or errors, proceed normally
            pass

        ### CALL HOOKS ### - modify/reject incoming data before calling the model
        
        ## LOGGING OBJECT ## - initialize logging object for logging success/failure events for call
        ## IMPORTANT Note: - initialize this before running pre-call checks. Ensures we log rejected requests to langfuse.
        logging_obj, self.data = litellm.utils.function_setup(
            original_function=route_type,
            rules_obj=litellm.utils.Rules(),
            start_time=start_time,
            **self.data,
        )

        self.data["litellm_logging_obj"] = logging_obj

        self.data = await proxy_logging_obj.pre_call_hook(  # type: ignore
            user_api_key_dict=user_api_key_dict, data=self.data, call_type=route_type  # type: ignore
        )

        if "messages" in self.data and self.data["messages"]:
            logging_obj.update_messages(self.data["messages"])

        return self.data, logging_obj

    async def base_process_llm_request(
        self,
        request: Request,
        fastapi_response: Response,
        user_api_key_dict: UserAPIKeyAuth,
        route_type: Literal[
            "acompletion",
            "aresponses",
            "_arealtime",
            "aget_responses",
            "adelete_responses",
            "acancel_responses",
            "atext_completion",
            "aimage_edit",
            "alist_input_items",
            "agenerate_content",
            "agenerate_content_stream",
            "allm_passthrough_route",
            "avector_store_search",
            "avector_store_create",
        ],
        proxy_logging_obj: ProxyLogging,
        general_settings: dict,
        proxy_config: ProxyConfig,
        select_data_generator: Callable,
        llm_router: Optional[Router] = None,
        model: Optional[str] = None,
        user_model: Optional[str] = None,
        user_temperature: Optional[float] = None,
        user_request_timeout: Optional[float] = None,
        user_max_tokens: Optional[int] = None,
        user_api_base: Optional[str] = None,
        version: Optional[str] = None,
        is_streaming_request: Optional[bool] = False,
    ) -> Any:
        """
        Common request processing logic for both chat completions and responses API endpoints
        """
        if verbose_proxy_logger.isEnabledFor(logging.DEBUG):
            verbose_proxy_logger.debug(
                "Request received by LiteLLM:\n{}".format(
                    json.dumps(self.data, indent=4, default=str)
                ),
            )

        self.data, logging_obj = await self.common_processing_pre_call_logic(
            request=request,
            general_settings=general_settings,
            proxy_logging_obj=proxy_logging_obj,
            user_api_key_dict=user_api_key_dict,
            version=version,
            proxy_config=proxy_config,
            user_model=user_model,
            user_temperature=user_temperature,
            user_request_timeout=user_request_timeout,
            user_max_tokens=user_max_tokens,
            user_api_base=user_api_base,
            model=model,
            route_type=route_type,
        )

        tasks = []
        tasks.append(
            proxy_logging_obj.during_call_hook(
                data=self.data,
                user_api_key_dict=user_api_key_dict,
                call_type=ProxyBaseLLMRequestProcessing._get_pre_call_type(
                    route_type=route_type  # type: ignore
                ),
            )
        )

        ### ROUTE THE REQUEST ###
        # Do not change this - it should be a constant time fetch - ALWAYS
        llm_call = await route_request(
            data=self.data,
            route_type=route_type,
            llm_router=llm_router,
            user_model=user_model,
        )
        tasks.append(llm_call)

        # wait for call to end
        llm_responses = asyncio.gather(
            *tasks
        )  # run the moderation check in parallel to the actual llm api call

        responses = await llm_responses

        response = responses[1]

        hidden_params = getattr(response, "_hidden_params", {}) or {}
        model_id = hidden_params.get("model_id", None) or ""
        cache_key = hidden_params.get("cache_key", None) or ""
        api_base = hidden_params.get("api_base", None) or ""
        response_cost = hidden_params.get("response_cost", None) or ""
        fastest_response_batch_completion = hidden_params.get(
            "fastest_response_batch_completion", None
        )
        additional_headers: dict = hidden_params.get("additional_headers", {}) or {}
        # Pricing headers + metadata (best-effort): per-model pricing and estimated cost
        try:
            from chutes.middleware.pricing import get_model_pricing, estimate_cost_usd  # type: ignore
            from scillm.telemetry.metrics import record_cost_usd as sc_record_cost_usd  # type: ignore

            # Prefer explicit model id; else fall back to request model
            priced_model = model_id or (self.data.get("model") if isinstance(self.data.get("model"), str) else "")
            pp, cp = get_model_pricing(priced_model)
            if pp is not None:
                additional_headers.setdefault("x-price-prompt-usd-perk", str(pp))
            if cp is not None:
                additional_headers.setdefault("x-price-completion-usd-perk", str(cp))
            # Estimate cost if token usage info is present on the response
            prompt_tok = None
            completion_tok = None
            try:
                usage = getattr(response, "usage", None) or {}
                prompt_tok = usage.get("prompt_tokens") if isinstance(usage, dict) else None
                completion_tok = usage.get("completion_tokens") if isinstance(usage, dict) else None
            except Exception:
                pass
            est = estimate_cost_usd(priced_model, prompt_tok, completion_tok)
            if est is not None:
                additional_headers.setdefault("x-estimated-cost-usd", str(est))
                # Prometheus: accumulate per-request cost (feature inferred from route)
                try:
                    # Derive vendor + served model id
                    vendor = ""
                    try:
                        ab = (api_base or "").lower()
                        if "chutes.ai" in ab:
                            vendor = "chutes"
                        elif "moonshot.ai" in ab:
                            vendor = "moonshot"
                        elif "ollama" in ab:
                            vendor = "ollama"
                        else:
                            vendor = "other"
                    except Exception:
                        vendor = "other"
                    served_model = model_id or str(self.data.get("model") or "")
                    sc_record_cost_usd(_route_to_feature(route_type), float(est), vendor=vendor, model=served_model)
                except Exception:
                    pass
            # Attach into response.additional_kwargs.scillm.pricing for body metadata
            try:
                ak = getattr(response, "additional_kwargs", {}) or {}
                sc = ak.get("scillm") or {}
                pricing_meta = sc.get("pricing") or {}
                if pp is not None:
                    pricing_meta.setdefault("prompt_per_1k", float(pp))
                if cp is not None:
                    pricing_meta.setdefault("completion_per_1k", float(cp))
                if est is not None:
                    pricing_meta.setdefault("estimated_cost_usd", float(est))
                if pricing_meta:
                    sc["pricing"] = pricing_meta
                    ak["scillm"] = sc
                    response.additional_kwargs = ak
            except Exception:
                pass
        except Exception:
            pass

        # Post Call Processing
        if llm_router is not None:
            self.data["deployment"] = llm_router.get_deployment(model_id=model_id)
        asyncio.create_task(
            proxy_logging_obj.update_request_status(
                litellm_call_id=self.data.get("litellm_call_id", ""), status="success"
            )
        )
        if self._is_streaming_request(
            data=self.data, is_streaming_request=is_streaming_request
        ) or self._is_streaming_response(
            response
        ):  # use generate_responses to stream responses
            custom_headers = ProxyBaseLLMRequestProcessing.get_custom_headers(
                user_api_key_dict=user_api_key_dict,
                call_id=logging_obj.litellm_call_id,
                model_id=model_id,
                cache_key=cache_key,
                api_base=api_base,
                version=version,
                response_cost=response_cost,
                model_region=getattr(user_api_key_dict, "allowed_model_region", ""),
                fastest_response_batch_completion=fastest_response_batch_completion,
                request_data=self.data,
                hidden_params=hidden_params,
                **additional_headers,
            )
            # Metrics + budget headers for streaming path
            try:
                snap = budget_snapshot()
                if snap:
                    # Set gauges
                    reset_at = snap.get("reset_at")
                    reset_epoch = None
                    if isinstance(reset_at, str):
                        s = reset_at[:-1] + "+00:00" if reset_at.endswith("Z") else reset_at
                        try:
                            from datetime import datetime, timezone

                            reset_epoch = datetime.fromisoformat(s).timestamp()
                        except Exception:
                            reset_epoch = None
                    set_budget(limit=snap.get("limit"), remaining=snap.get("remaining"), reset_epoch=reset_epoch)
            except Exception:
                pass
            inc_call("ok")
            # Record metrics for streaming path prior to return
            try:
                started = float(self.data.get("_sc_started_at", 0.0) or 0.0)
                if started > 0:
                    sc_record_latency(_route_to_feature(route_type), time.monotonic() - started)
                sc_record_call(_route_to_feature(route_type), "ok")
            except Exception:
                pass

            if route_type == "allm_passthrough_route":
                # Check if response is an async generator
                if self._is_streaming_response(response):
                    if asyncio.iscoroutine(response):
                        generator = await response
                    else:
                        generator = response

                    # For passthrough routes, stream directly without error parsing
                    # since we're dealing with raw binary data (e.g., AWS event streams)
                    return StreamingResponse(
                        content=generator,
                        status_code=status.HTTP_200_OK,
                        headers=custom_headers,
                    )
                else:
                    # Traditional HTTP response with aiter_bytes
                    return StreamingResponse(
                        content=response.aiter_bytes(),
                        status_code=response.status_code,
                        headers=custom_headers,
                    )
            else:
                selected_data_generator = select_data_generator(
                    response=response,
                    user_api_key_dict=user_api_key_dict,
                    request_data=self.data,
                )
                return await create_streaming_response(
                    generator=selected_data_generator,
                    media_type="text/event-stream",
                    headers=custom_headers,
                )

        ### CALL HOOKS ### - modify outgoing data
        response = await proxy_logging_obj.post_call_success_hook(
            data=self.data, user_api_key_dict=user_api_key_dict, response=response
        )

        hidden_params = (
            getattr(response, "_hidden_params", {}) or {}
        )  # get any updated response headers
        additional_headers = hidden_params.get("additional_headers", {}) or {}

        # Success metrics and headers
        try:
            snap = budget_snapshot()
            if snap:
                reset_at = snap.get("reset_at")
                if reset_at:
                    additional_headers["x-budget-reset-at"] = reset_at
                additional_headers["x-ratelimit-remaining-requests"] = str(snap.get("remaining", ""))
                # Set gauges
                reset_epoch = None
                if isinstance(reset_at, str):
                    s = reset_at[:-1] + "+00:00" if reset_at.endswith("Z") else reset_at
                    try:
                        from datetime import datetime

                        reset_epoch = datetime.fromisoformat(s).timestamp()
                    except Exception:
                        reset_epoch = None
                set_budget(limit=snap.get("limit"), remaining=snap.get("remaining"), reset_epoch=reset_epoch)
                sc_set_budget(_route_to_feature(route_type), limit=snap.get("limit"), remaining=snap.get("remaining"), reset_epoch=reset_epoch)
        except Exception:
            pass
        # Record success
        inc_call("ok")
        try:
            started = float(self.data.get("_sc_started_at", 0.0) or 0.0)
            if started > 0:
                sc_record_latency(_route_to_feature(route_type), time.monotonic() - started)
            sc_record_call(_route_to_feature(route_type), "ok")
        except Exception:
            pass

        # Inject standardized budget metadata into response body (non-streaming)
        try:
            if os.getenv("SCILLM_BUDGET_METADATA", "1").strip().lower() not in {"0", "false", "no"}:
                ak = getattr(response, "additional_kwargs", {}) or {}
                sc = ak.get("scillm") or {}
                sc["budget"] = budget_metadata()
                ak["scillm"] = sc
                response.additional_kwargs = ak
        except Exception:
            pass

        fastapi_response.headers.update(
            ProxyBaseLLMRequestProcessing.get_custom_headers(
                user_api_key_dict=user_api_key_dict,
                call_id=logging_obj.litellm_call_id,
                model_id=model_id,
                cache_key=cache_key,
                api_base=api_base,
                version=version,
                response_cost=response_cost,
                model_region=getattr(user_api_key_dict, "allowed_model_region", ""),
                fastest_response_batch_completion=fastest_response_batch_completion,
                request_data=self.data,
                hidden_params=hidden_params,
                **additional_headers,
            )
        )
        await check_response_size_is_safe(response=response)

        return response

    async def base_passthrough_process_llm_request(
        self,
        request: Request,
        fastapi_response: Response,
        user_api_key_dict: UserAPIKeyAuth,
        proxy_logging_obj: ProxyLogging,
        general_settings: dict,
        proxy_config: ProxyConfig,
        select_data_generator: Callable,
        llm_router: Optional[Router] = None,
        model: Optional[str] = None,
        user_model: Optional[str] = None,
        user_temperature: Optional[float] = None,
        user_request_timeout: Optional[float] = None,
        user_max_tokens: Optional[int] = None,
        user_api_base: Optional[str] = None,
        version: Optional[str] = None,
    ):
        from litellm.proxy.pass_through_endpoints.pass_through_endpoints import (
            HttpPassThroughEndpointHelpers,
        )

        result = await self.base_process_llm_request(
            request=request,
            fastapi_response=fastapi_response,
            user_api_key_dict=user_api_key_dict,
            route_type="allm_passthrough_route",
            proxy_logging_obj=proxy_logging_obj,
            llm_router=llm_router,
            general_settings=general_settings,
            proxy_config=proxy_config,
            select_data_generator=select_data_generator,
            model=model,
            user_model=user_model,
            user_temperature=user_temperature,
            user_request_timeout=user_request_timeout,
            user_max_tokens=user_max_tokens,
            user_api_base=user_api_base,
            version=version,
        )

        # Check if result is actually a streaming response by inspecting its type
        if isinstance(result, StreamingResponse):
            return result

        content = await result.aread()
        return Response(
            content=content,
            status_code=result.status_code,
            headers=HttpPassThroughEndpointHelpers.get_response_headers(
                headers=result.headers,
                custom_headers=None,
            ),
        )

    def _is_streaming_response(self, response: Any) -> bool:
        """
        Check if the response object is actually a streaming response by inspecting its type.

        This uses standard Python inspection to detect streaming/async iterator objects
        rather than relying on specific wrapper classes.
        """
        import inspect
        from collections.abc import AsyncGenerator, AsyncIterator

        # Check if it's an async generator (most reliable)
        if inspect.isasyncgen(response):
            return True

        # Check if it implements the async iterator protocol
        if isinstance(response, (AsyncIterator, AsyncGenerator)):
            return True

        return False

    def _is_streaming_request(
        self, data: dict, is_streaming_request: Optional[bool] = False
    ) -> bool:
        """
        Check if the request is a streaming request.

        1. is_streaming_request is a dynamic param passed in
        2. if "stream" in data and data["stream"] is True
        """
        if is_streaming_request is True:
            return True
        if "stream" in data and data["stream"] is True:
            return True
        return False

    @staticmethod
    def _get_pre_call_type(
        route_type: Literal["acompletion", "aresponses"],
    ) -> Literal["completion", "responses"]:
        if route_type == "acompletion":
            return "completion"
        elif route_type == "aresponses":
            return "responses"

    async def _handle_llm_api_exception(
        self,
        e: Exception,
        user_api_key_dict: UserAPIKeyAuth,
        proxy_logging_obj: ProxyLogging,
        version: Optional[str] = None,
    ):
        """Raises ProxyException (OpenAI API compatible) if an exception is raised"""
        verbose_proxy_logger.exception(
            f"litellm.proxy.proxy_server._handle_llm_api_exception(): Exception occured - {str(e)}"
        )
        await proxy_logging_obj.post_call_failure_hook(
            user_api_key_dict=user_api_key_dict,
            original_exception=e,
            request_data=self.data,
        )
        litellm_debug_info = getattr(e, "litellm_debug_info", "")
        verbose_proxy_logger.debug(
            "\033[1;31mAn error occurred: %s %s\n\n Debug this by setting `--debug`, e.g. `litellm --model gpt-3.5-turbo --debug`",
            e,
            litellm_debug_info,
        )

        timeout = getattr(
            e, "timeout", None
        )  # returns the timeout set by the wrapper. Used for testing if model-specific timeout are set correctly
        _litellm_logging_obj: Optional[LiteLLMLoggingObj] = self.data.get(
            "litellm_logging_obj", None
        )
        custom_headers = ProxyBaseLLMRequestProcessing.get_custom_headers(
            user_api_key_dict=user_api_key_dict,
            call_id=(
                _litellm_logging_obj.litellm_call_id if _litellm_logging_obj else None
            ),
            version=version,
            response_cost=0,
            model_region=getattr(user_api_key_dict, "allowed_model_region", ""),
            request_data=self.data,
            timeout=timeout,
        )
        headers = getattr(e, "headers", {}) or {}
        headers.update(custom_headers)

        # Normalize rate limit into budget_exhausted where applicable
        if isinstance(e, RateLimitError) or getattr(e, "status_code", None) == 429:
            try:
                cooldown = int(os.getenv("SCILLM_COOLDOWN_429_S", "120") or "120")
            except Exception:
                cooldown = 120
            norm = normalize_ratelimit_to_budget(exc=e, default_cooldown_s=cooldown)
            headers.update(norm.get("headers", {}))
            # Metrics for 429
            kind = "budget_exhausted" if norm.get("type") == "budget_exhausted" else "throttling"
            inc_429(kind)
            inc_call("429_budget" if kind == "budget_exhausted" else "429_other")
            try:
                # Fallback feature tag to 'text' on error paths
                sc_record_call("text", "429")
                started = float(self.data.get("_sc_started_at", 0.0) or 0.0)
                if started > 0:
                    sc_record_latency("text", time.monotonic() - started)
            except Exception:
                pass
            # Add a brief hint into the message for humans (non-breaking)
            try:
                snap = budget_snapshot() or {}
                rem = snap.get("remaining")
                rst = snap.get("reset_at")
                if rem is not None or rst:
                    hint = f" (remaining={rem}, reset_at={rst})"
                    norm_msg = str(norm.get("message", "Rate limit")) + hint
                else:
                    norm_msg = str(norm.get("message", "Rate limit"))
            except Exception:
                norm_msg = str(norm.get("message", "Rate limit"))
            raise ProxyException(
                message=norm_msg,
                type=str(norm.get("type", getattr(e, "type", "throttling_error"))),
                param=getattr(e, "param", None),
                code=int(norm.get("code", getattr(e, "status_code", 429)) or 429),
                headers=headers,
            )

        if isinstance(e, HTTPException):
            raise ProxyException(
                message=getattr(e, "detail", str(e)),
                type=getattr(e, "type", "None"),
                param=getattr(e, "param", "None"),
                code=getattr(e, "status_code", status.HTTP_400_BAD_REQUEST),
                headers=headers,
            )
        error_msg = f"{str(e)}"
        try:
            sc_record_call("text", "error")
            started = float(self.data.get("_sc_started_at", 0.0) or 0.0)
            if started > 0:
                sc_record_latency("text", time.monotonic() - started)
        except Exception:
            pass
        raise ProxyException(
            message=getattr(e, "message", error_msg),
            type=getattr(e, "type", "None"),
            param=getattr(e, "param", "None"),
            openai_code=getattr(e, "code", None),
            code=getattr(e, "status_code", 500),
            headers=headers,
        )


def _route_to_feature(route_type: str) -> str:
    mapping = {
        "acompletion": "text",
        "atext_completion": "text",
        "aembedding": "embeddings",
        "aimage_edit": "images",
        "agenerate_content": "text",
        "agenerate_content_stream": "text",
        "allm_passthrough_route": "gateway",
    }
    return mapping.get(route_type, "gateway")

    #########################################################
    # Proxy Level Streaming Data Generator
    #########################################################

    @staticmethod
    def return_sse_chunk(chunk: Any) -> str:
        """
        Helper function to format streaming chunks for Anthropic API format

        Args:
            chunk: A string or dictionary to be returned in SSE format

        Returns:
            str: A properly formatted SSE chunk string
        """
        if isinstance(chunk, dict):
            # Use safe_dumps for proper JSON serialization with circular reference detection
            chunk_str = safe_dumps(chunk)
            return f"{STREAM_SSE_DATA_PREFIX}{chunk_str}\n\n"
        else:
            return chunk

    @staticmethod
    async def async_sse_data_generator(
        response,
        user_api_key_dict: UserAPIKeyAuth,
        request_data: dict,
        proxy_logging_obj: ProxyLogging,
    ):
        """
        Anthropic /messages and Google /generateContent streaming data generator require SSE events
        """
        from litellm.types.utils import ModelResponse, ModelResponseStream

        verbose_proxy_logger.debug("inside generator")
        try:
            str_so_far = ""
            async for chunk in proxy_logging_obj.async_post_call_streaming_iterator_hook(
                user_api_key_dict=user_api_key_dict,
                response=response,
                request_data=request_data,
            ):
                verbose_proxy_logger.debug(
                    "async_data_generator: received streaming chunk - {}".format(chunk)
                )
                ### CALL HOOKS ### - modify outgoing data
                chunk = await proxy_logging_obj.async_post_call_streaming_hook(
                    user_api_key_dict=user_api_key_dict,
                    response=chunk,
                    data=request_data,
                    str_so_far=str_so_far,
                )

                if isinstance(chunk, (ModelResponse, ModelResponseStream)):
                    response_str = litellm.get_response_string(response_obj=chunk)
                    str_so_far += response_str

                # Format chunk using helper function
                yield ProxyBaseLLMRequestProcessing.return_sse_chunk(chunk)
        except Exception as e:
            verbose_proxy_logger.exception(
                "litellm.proxy.proxy_server.async_data_generator(): Exception occured - {}".format(
                    str(e)
                )
            )
            await proxy_logging_obj.post_call_failure_hook(
                user_api_key_dict=user_api_key_dict,
                original_exception=e,
                request_data=request_data,
            )
            verbose_proxy_logger.debug(
                f"\033[1;31mAn error occurred: {e}\n\n Debug this by setting `--debug`, e.g. `litellm --model gpt-3.5-turbo --debug`"
            )

            if isinstance(e, HTTPException):
                raise e
            else:
                error_traceback = traceback.format_exc()
                error_msg = f"{str(e)}\n\n{error_traceback}"

            proxy_exception = ProxyException(
                message=getattr(e, "message", error_msg),
                type=getattr(e, "type", "None"),
                param=getattr(e, "param", "None"),
                code=getattr(e, "status_code", 500),
            )
            error_returned = json.dumps({"error": proxy_exception.to_dict()})
            yield f"{STREAM_SSE_DATA_PREFIX}{error_returned}\n\n"
