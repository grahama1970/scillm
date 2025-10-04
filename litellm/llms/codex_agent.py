from __future__ import annotations

"""
Experimental env‑gated provider: codex-agent

Intent
- Allow Router(model_list=[{"model_name":"codex-agent-1","litellm_params":{"model":"codex-agent/mini"}}])
  to work without client changes when explicitly enabled.

How it works
- Uses an OpenAI‑compatible chat endpoint exposed by a local service (e.g., mini‑agent shim)
  pointed to by CODEX_AGENT_API_BASE, or the `api_base` param passed by Router/LiteLLM.
- Disabled by default; enable with LITELLM_ENABLE_CODEX_AGENT=1.

Notes
- This is a minimal adapter to keep the surface stable in this fork. It does not shell out to
  any CLI; it expects an HTTP endpoint that accepts OpenAI Chat Completions and returns
  {choices: [{message: {content}}]}.
"""

import os
from typing import Any, Optional, Union, Callable

import httpx

from litellm.llms.custom_llm import CustomLLM, CustomLLMError
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, HTTPHandler
from litellm.utils import ModelResponse

from .codex_sidecar_manager import SidecarError, ensure_sidecar


class CodexAgentLLM(CustomLLM):
    def __init__(self) -> None:
        super().__init__()

    def _resolve_base(self, api_base: Optional[str]) -> str:
        base = api_base or os.getenv("CODEX_AGENT_API_BASE")
        if base:
            return base.rstrip("/")

        try:
            sidecar_base = ensure_sidecar()
        except SidecarError as exc:
            raise CustomLLMError(
                status_code=500,
                message=(
                    "codex-agent sidecar failed to start: "
                    + str(exc)
                )[:400],
            ) from exc
        except Exception as exc:  # pragma: no cover - safety net
            raise CustomLLMError(
                status_code=500,
                message=(
                    "codex-agent sidecar unexpected failure: "
                    + str(exc)
                )[:400],
            ) from exc
        return sidecar_base.rstrip("/")

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
        payload: dict[str, Any] = {"model": model, "messages": messages}
        extras = dict(optional_params or {})
        for key, value in extras.items():
            if key not in ("model", "messages"):
                payload[key] = value
        # Compose headers; honor provided headers but add Authorization if api_key is present
        _hdr = dict(headers or {})
        if api_key and not any(k.lower() == "authorization" for k in _hdr.keys()):
            _hdr["Authorization"] = f"Bearer {api_key}"
        request_timeout: Optional[Union[float, httpx.Timeout]] = timeout or 30.0
        max_retries = int(os.getenv("CODEX_AGENT_MAX_RETRIES", "2"))
        base_ms = int(os.getenv("CODEX_AGENT_RETRY_BASE_MS", "120"))
        max_backoff_ms = int(os.getenv("CODEX_AGENT_MAX_BACKOFF_MS", "1500"))
        det_seed = os.getenv("SCILLM_DETERMINISTIC_SEED") or os.getenv("CODEX_AGENT_DETERMINISTIC_SEED")
        rng = None
        if det_seed:
            import random as _r
            rng = _r.Random(int(det_seed))
        attempt = 0
        while True:
            try:
                if isinstance(client, HTTPHandler):
                    r = client.post(
                        f"{base}/v1/chat/completions",
                        json=payload,
                        headers=_hdr or None,
                        timeout=request_timeout,
                    )
                else:
                    with httpx.Client(timeout=request_timeout, headers=_hdr) as c:
                        r = c.post(f"{base}/v1/chat/completions", json=payload)
                # Retry on transient 5xx
                if 500 <= getattr(r, "status_code", 0) < 600 and attempt < max_retries:
                    attempt += 1
                    import time as _t
                    exp = min(base_ms * (2 ** (attempt - 1)), max_backoff_ms)
                    jitter = exp * ((0.05 + 0.10 * (rng.random() if rng else 0.5)))
                    sleep_ms = exp + jitter
                    if os.getenv("CODEX_AGENT_LOG_RETRIES", "0") == "1":
                        try:
                            print_verbose(f"codex-agent retry: attempt={attempt} status={getattr(r,'status_code',0)} sleep_ms={int(sleep_ms)}")
                        except Exception:
                            pass
                    _t.sleep(sleep_ms / 1000.0)
                    continue
                if r.status_code < 200 or r.status_code >= 300:
                    raise CustomLLMError(status_code=r.status_code, message=r.text[:400])
                data = r.json()
                break
            except CustomLLMError:
                raise
            except Exception as e:
                if attempt < max_retries:
                    attempt += 1
                    import time as _t
                    exp = min(base_ms * (2 ** (attempt - 1)), max_backoff_ms)
                    jitter = exp * ((0.05 + 0.10 * (rng.random() if rng else 0.5)))
                    sleep_ms = exp + jitter
                    if os.getenv("CODEX_AGENT_LOG_RETRIES", "0") == "1":
                        try:
                            print_verbose(f"codex-agent retry: exception attempt={attempt} sleep_ms={int(sleep_ms)} err={str(e)[:120]}")
                        except Exception:
                            pass
                    _t.sleep(sleep_ms / 1000.0)
                    continue
                raise CustomLLMError(status_code=500, message=str(e)[:400])

        content = ""
        try:
            content = (((data or {}).get("choices") or [{}])[0] or {}).get("message", {}).get("content") or ""
        except Exception:
            content = ""

        # Populate provided model_response with a proper Message object
        model_response.model = model
        try:
            # choices[0].message is a Message; set content directly to preserve typing
            model_response.choices[0].message.content = content  # type: ignore[attr-defined]
            model_response.choices[0].message.role = "assistant"  # type: ignore[attr-defined]
        except Exception:
            # Fallback: re-wrap safely via dict constructor
            model_response.choices[0].message = {"role": "assistant", "content": content}  # type: ignore[assignment]
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
        base = self._resolve_base(api_base)
        payload: dict[str, Any] = {"model": model, "messages": messages}
        extras = dict(optional_params or {})
        for key, value in extras.items():
            if key not in ("model", "messages"):
                payload[key] = value
        _hdr = dict(headers or {})
        if api_key and not any(k.lower() == "authorization" for k in _hdr.keys()):
            _hdr["Authorization"] = f"Bearer {api_key}"
        request_timeout: Optional[Union[float, httpx.Timeout]] = timeout or 30.0
        max_retries = int(os.getenv("CODEX_AGENT_MAX_RETRIES", "2"))
        base_ms = int(os.getenv("CODEX_AGENT_RETRY_BASE_MS", "120"))
        max_backoff_ms = int(os.getenv("CODEX_AGENT_MAX_BACKOFF_MS", "1500"))
        det_seed = os.getenv("SCILLM_DETERMINISTIC_SEED") or os.getenv("CODEX_AGENT_DETERMINISTIC_SEED")
        rng = None
        if det_seed:
            import random as _r
            rng = _r.Random(int(det_seed))
        attempt = 0
        while True:
            try:
                if isinstance(client, AsyncHTTPHandler):
                    r = await client.post(
                        f"{base}/v1/chat/completions",
                        json=payload,
                        headers=_hdr or None,
                        timeout=request_timeout,
                    )
                else:
                    async with httpx.AsyncClient(timeout=request_timeout, headers=_hdr) as c:
                        r = await c.post(f"{base}/v1/chat/completions", json=payload)
                # Retry on transient 5xx
                if 500 <= getattr(r, "status_code", 0) < 600 and attempt < max_retries:
                    attempt += 1
                    import asyncio as _a
                    exp = min(base_ms * (2 ** (attempt - 1)), max_backoff_ms)
                    jitter = exp * ((0.05 + 0.10 * (rng.random() if rng else 0.5)))
                    sleep_ms = exp + jitter
                    if os.getenv("CODEX_AGENT_LOG_RETRIES", "0") == "1":
                        try:
                            print_verbose(f"codex-agent retry: attempt={attempt} status={getattr(r,'status_code',0)} sleep_ms={int(sleep_ms)}")
                        except Exception:
                            pass
                    await _a.sleep(sleep_ms / 1000.0)
                    continue
                if r.status_code < 200 or r.status_code >= 300:
                    raise CustomLLMError(status_code=r.status_code, message=r.text[:400])
                data = r.json()
                break
            except CustomLLMError:
                raise
            except Exception as e:
                if attempt < max_retries:
                    attempt += 1
                    import asyncio as _a
                    exp = min(base_ms * (2 ** (attempt - 1)), max_backoff_ms)
                    jitter = exp * ((0.05 + 0.10 * (rng.random() if rng else 0.5)))
                    sleep_ms = exp + jitter
                    if os.getenv("CODEX_AGENT_LOG_RETRIES", "0") == "1":
                        try:
                            print_verbose(f"codex-agent retry: exception attempt={attempt} sleep_ms={int(sleep_ms)} err={str(e)[:120]}")
                        except Exception:
                            pass
                    await _a.sleep(sleep_ms / 1000.0)
                    continue
                raise CustomLLMError(status_code=500, message=str(e)[:400])

        content = ""
        try:
            content = (((data or {}).get("choices") or [{}])[0] or {}).get("message", {}).get("content") or ""
        except Exception:
            content = ""

        model_response.model = model
        try:
            model_response.choices[0].message.content = content  # type: ignore[attr-defined]
            model_response.choices[0].message.role = "assistant"  # type: ignore[attr-defined]
        except Exception:
            model_response.choices[0].message = {"role": "assistant", "content": content}  # type: ignore[assignment]
        return model_response

# --- Optional self-registration (env-gated) -----------------------------------
try:
    if os.getenv("LITELLM_ENABLE_CODEX_AGENT", "") == "1":
        # Try a central registry first
        try:
            from litellm.llms import PROVIDER_REGISTRY  # type: ignore
            PROVIDER_REGISTRY["codex-agent"] = CodexAgentLLM
            PROVIDER_REGISTRY["codex_cli_agent"] = CodexAgentLLM
        except Exception:
            # Fall back to a helper registration function if available
            try:
                from litellm.llms.custom_llm import register_custom_provider  # type: ignore
                register_custom_provider("codex-agent", CodexAgentLLM)
                register_custom_provider("codex_cli_agent", CodexAgentLLM)
            except Exception:
                pass
except Exception:
    pass
