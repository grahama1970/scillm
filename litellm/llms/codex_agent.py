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
import uuid
from typing import Any, Optional, Union, Callable

import httpx

from litellm.llms.custom_llm import CustomLLM, CustomLLMError
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, HTTPHandler
from litellm.utils import ModelResponse

# Lazy import of sidecar manager to avoid hard dependency at import time.
SidecarError = None  # type: ignore
ensure_sidecar = None  # type: ignore
restart_sidecar = None  # type: ignore


class CodexAgentLLM(CustomLLM):
    def __init__(self) -> None:
        super().__init__()

    def _resolve_base(self, api_base: Optional[str]) -> str:
        base = api_base or os.getenv("CODEX_AGENT_API_BASE")
        if base:
            b = base.strip()
            # Guard: users sometimes set a base ending with '/v1'. Normalize to root.
            if b.endswith("/v1"):
                b = b[:-3]
            return b.rstrip("/")

        try:
            global ensure_sidecar, SidecarError
            if ensure_sidecar is None:
                from .codex_sidecar_manager import ensure_sidecar as _ens, SidecarError as _SE  # type: ignore
                ensure_sidecar = _ens; SidecarError = _SE
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
        # Ephemeral per-call for local judge (gpt-5) calls when no explicit base is set
        # Acceptable 3–8s overhead for a fresh process
        try:
            _mid_check = str(model)
            _is_judge = "gpt-5" in _mid_check
            _has_explicit_base = bool(api_base or os.getenv("CODEX_AGENT_API_BASE"))
            if _is_judge and not _has_explicit_base:
                # Restart embedded sidecar and use its base
                global restart_sidecar
                if restart_sidecar is None:
                    from .codex_sidecar_manager import restart_sidecar as _rs  # type: ignore
                    restart_sidecar = _rs
                base = restart_sidecar()
            else:
                base = self._resolve_base(api_base)
        except Exception:
            base = self._resolve_base(api_base)
        # Normalize common provider prefixes so the sidecar/gateway sees a plain model id
        try:
            _mid = str(model)
            if _mid.startswith("codex-agent/"):
                _mid = _mid.split("/", 1)[1]
            elif _mid.startswith("codex/"):
                _mid = _mid.split("/", 1)[1]
        except Exception:
            _mid = model
        # Fresh session + context guard (low cognitive load: defaults via env, no caller changes)
        raw_msgs = list(messages or [])
        try:
            # Keep system messages + last N non-system messages (heuristic to avoid context rot)
            max_hist = int(os.getenv("SCILLM_CODEX_MAX_HISTORY", "8"))
            if max_hist > 0 and len(raw_msgs) > max_hist:
                system_msgs = [m for m in raw_msgs if isinstance(m, dict) and m.get("role") == "system"]
                non_system = [m for m in raw_msgs if not (isinstance(m, dict) and m.get("role") == "system")]
                raw_msgs = system_msgs + non_system[-max_hist:]
        except Exception:
            pass

        payload: dict[str, Any] = {"model": _mid, "messages": raw_msgs}
        extras = dict(optional_params or {})
        for key, value in extras.items():
            if key not in ("model", "messages"):
                payload[key] = value
        # Normalize reasoning parameter shapes for OpenAI-compatible backends
        try:
            reffort = (payload.get("reasoning_effort") or extras.get("reasoning_effort"))
            if isinstance(reffort, str):
                # Ensure both forms are present for maximum compatibility
                payload["reasoning_effort"] = reffort
                if not isinstance(payload.get("reasoning"), dict):
                    payload["reasoning"] = {"effort": reffort}
                else:
                    payload["reasoning"].setdefault("effort", reffort)
        except Exception:
            pass
        # Compose headers; honor provided headers but add Authorization if api_key is present
        _hdr = dict(headers or {})
        # Force local sidecar handling (the sidecar honors this header to bypass any forwarding config)
        _hdr.setdefault("X-Codex-Force-Local", "1")
        if api_key and not any(k.lower() == "authorization" for k in _hdr.keys()):
            _hdr["Authorization"] = f"Bearer {api_key}"
        # Sanitize hop-by-hop and duplicate Authorization headers defensively
        _sanitized: dict[str, Any] = {}
        _forbidden = {
            "connection",
            "upgrade",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailers",
            "transfer-encoding",
            "keep-alive",
        }
        for k, v in list(_hdr.items()):
            lk = str(k).lower()
            if lk in _forbidden:
                continue
            if lk == "authorization" and any(str(x).lower() == "authorization" for x in _sanitized.keys()):
                continue
            _sanitized[k] = v
        _hdr = _sanitized
        # Fresh session header by default (stateless per-call behavior)
        try:
            if os.getenv("SCILLM_CODEX_FRESH", "1") in {"1", "true", "yes", "on"}:
                _hdr.setdefault("X-Codex-Session", uuid.uuid4().hex)
        except Exception:
            pass

        # Reserve output space if caller didn't specify
        try:
            if "max_tokens" not in payload and os.getenv("SCILLM_CODEX_MAX_TOKENS"):
                payload["max_tokens"] = int(os.getenv("SCILLM_CODEX_MAX_TOKENS", "256"))
        except Exception:
            pass
        request_timeout: Optional[Union[float, httpx.Timeout]] = timeout or 30.0
        max_retries = int(os.getenv("CODEX_AGENT_MAX_RETRIES", "2"))
        base_ms = int(os.getenv("CODEX_AGENT_RETRY_BASE_MS", "120"))
        max_backoff_ms = int(os.getenv("CODEX_AGENT_MAX_BACKOFF_MS", "1500"))
        det_seed = os.getenv("SCILLM_DETERMINISTIC_SEED") or os.getenv("CODEX_AGENT_DETERMINISTIC_SEED")
        rng = None
        if det_seed:
            import random as _r
            rng = _r.Random(int(det_seed))
        metrics_enabled = os.getenv("CODEX_AGENT_ENABLE_METRICS", "0") == "1"
        retry_stats = {
            "attempts": 0,
            "failures": 0,
            "total_sleep_ms": 0,
            "final_status": None,
            "first_failure_status": None,
            "retry_sequence": [],
            "statuses": [],
        }
        attempt = 0
        while True:
            try:
                if client is not None and hasattr(client, "post"):
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
                    if metrics_enabled:
                        retry_stats["attempts"] = attempt
                        retry_stats["failures"] += 1
                        retry_stats["total_sleep_ms"] += int(sleep_ms)
                        try:
                            retry_stats["retry_sequence"].append(int(sleep_ms))
                        except Exception:
                            pass
                        if retry_stats.get("first_failure_status") is None:
                            retry_stats["first_failure_status"] = getattr(r, "status_code", None)
                        try:
                            retry_stats["statuses"].append(getattr(r, "status_code", None))
                        except Exception:
                            pass
                        try:
                            retry_stats["statuses"].append(getattr(r, "status_code", None))
                        except Exception:
                            pass
                    if os.getenv("CODEX_AGENT_LOG_RETRIES", "0") == "1":
                        try:
                            print_verbose(f"[codex-agent][retry] {{\"attempt\":{attempt},\"status\":{getattr(r,'status_code',0)},\"sleep_ms\":{int(sleep_ms)} }}")
                        except Exception:
                            pass
                    _t.sleep(sleep_ms / 1000.0)
                    continue
                if r.status_code < 200 or r.status_code >= 300:
                    raise CustomLLMError(status_code=r.status_code, message=r.text[:400])
                data = r.json()
                if metrics_enabled:
                    try:
                        retry_stats["final_status"] = r.status_code
                        retry_stats["statuses"].append(r.status_code)
                    except Exception:
                        pass
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
                    if metrics_enabled:
                        retry_stats["attempts"] = attempt
                        retry_stats["failures"] += 1
                        retry_stats["total_sleep_ms"] += int(sleep_ms)
                        try:
                            retry_stats["retry_sequence"].append(int(sleep_ms))
                        except Exception:
                            pass
                        if retry_stats.get("first_failure_status") is None:
                            retry_stats["first_failure_status"] = getattr(e, "status_code", 500)
                        try:
                            retry_stats["statuses"].append(getattr(e, "status_code", 500))
                        except Exception:
                            pass
                    if os.getenv("CODEX_AGENT_LOG_RETRIES", "0") == "1":
                        try:
                            print_verbose(f"[codex-agent][retry] {{\"attempt\":{attempt},\"exception\":true,\"sleep_ms\":{int(sleep_ms)},\"error\":\"{str(e)[:80]}\"}}")
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
        if metrics_enabled:
            try:
                model_response.additional_kwargs = getattr(model_response, "additional_kwargs", {}) or {}
                model_response.additional_kwargs.setdefault("codex_agent", {})["retry_stats"] = retry_stats
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
        base = self._resolve_base(api_base)
        try:
            _mid = str(model)
            if _mid.startswith("codex-agent/"):
                _mid = _mid.split("/", 1)[1]
            elif _mid.startswith("codex/"):
                _mid = _mid.split("/", 1)[1]
        except Exception:
            _mid = model
        payload: dict[str, Any] = {"model": _mid, "messages": messages}
        extras = dict(optional_params or {})
        for key, value in extras.items():
            if key not in ("model", "messages"):
                payload[key] = value
        # Normalize reasoning parameter shapes for OpenAI-compatible backends (async)
        try:
            reffort = (payload.get("reasoning_effort") or extras.get("reasoning_effort"))
            if isinstance(reffort, str):
                payload["reasoning_effort"] = reffort
                if not isinstance(payload.get("reasoning"), dict):
                    payload["reasoning"] = {"effort": reffort}
                else:
                    payload["reasoning"].setdefault("effort", reffort)
        except Exception:
            pass
        _hdr = dict(headers or {})
        if api_key and not any(k.lower() == "authorization" for k in _hdr.keys()):
            _hdr["Authorization"] = f"Bearer {api_key}"
        _sanitized: dict[str, Any] = {}
        _forbidden = {
            "connection",
            "upgrade",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailers",
            "transfer-encoding",
            "keep-alive",
        }
        for k, v in list(_hdr.items()):
            lk = str(k).lower()
            if lk in _forbidden:
                continue
            if lk == "authorization" and any(str(x).lower() == "authorization" for x in _sanitized.keys()):
                continue
            _sanitized[k] = v
        _hdr = _sanitized
        # Force local sidecar handling for async path as well
        _hdr.setdefault("X-Codex-Force-Local", "1")
        request_timeout: Optional[Union[float, httpx.Timeout]] = timeout or 30.0
        max_retries = int(os.getenv("CODEX_AGENT_MAX_RETRIES", "2"))
        base_ms = int(os.getenv("CODEX_AGENT_RETRY_BASE_MS", "120"))
        max_backoff_ms = int(os.getenv("CODEX_AGENT_MAX_BACKOFF_MS", "1500"))
        det_seed = os.getenv("SCILLM_DETERMINISTIC_SEED") or os.getenv("CODEX_AGENT_DETERMINISTIC_SEED")
        rng = None
        if det_seed:
            import random as _r
            rng = _r.Random(int(det_seed))
        metrics_enabled = os.getenv("CODEX_AGENT_ENABLE_METRICS", "0") == "1"
        retry_stats = {
            "attempts": 0,
            "failures": 0,
            "total_sleep_ms": 0,
            "final_status": None,
            "first_failure_status": None,
            "retry_sequence": [],
            "statuses": [],
        }
        attempt = 0
        while True:
            try:
                if client is not None and hasattr(client, "post"):
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
                    if metrics_enabled:
                        try:
                            retry_stats["statuses"].append(getattr(r, "status_code", None))
                        except Exception:
                            pass
                    if os.getenv("CODEX_AGENT_LOG_RETRIES", "0") == "1":
                        try:
                            print_verbose(f"[codex-agent][retry] {{\"attempt\":{attempt},\"status\":{getattr(r,'status_code',0)},\"sleep_ms\":{int(sleep_ms)} }}")
                        except Exception:
                            pass
                    if metrics_enabled:
                        retry_stats["attempts"] = attempt
                        retry_stats["failures"] += 1
                        retry_stats["total_sleep_ms"] += int(sleep_ms)
                        try:
                            retry_stats["retry_sequence"].append(int(sleep_ms))
                        except Exception:
                            pass
                        if retry_stats.get("first_failure_status") is None:
                            retry_stats["first_failure_status"] = getattr(r, "status_code", None)
                    await _a.sleep(sleep_ms / 1000.0)
                    continue
                if r.status_code < 200 or r.status_code >= 300:
                    raise CustomLLMError(status_code=r.status_code, message=r.text[:400])
                data = r.json()
                if metrics_enabled:
                    try:
                        retry_stats["final_status"] = r.status_code
                        retry_stats["statuses"].append(r.status_code)
                    except Exception:
                        pass
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
                            print_verbose(f"[codex-agent][retry] {{\"attempt\":{attempt},\"exception\":true,\"sleep_ms\":{int(sleep_ms)},\"error\":\"{str(e)[:80]}\"}}")
                        except Exception:
                            pass
                    if metrics_enabled:
                        retry_stats["attempts"] = attempt
                        retry_stats["failures"] += 1
                        retry_stats["total_sleep_ms"] += int(sleep_ms)
                        try:
                            retry_stats["retry_sequence"].append(int(sleep_ms))
                        except Exception:
                            pass
                        if retry_stats.get("first_failure_status") is None:
                            retry_stats["first_failure_status"] = getattr(e, "status_code", 500)
                        try:
                            retry_stats["statuses"].append(getattr(e, "status_code", 500))
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
        if metrics_enabled:
            try:
                model_response.additional_kwargs = getattr(model_response, "additional_kwargs", {}) or {}
                model_response.additional_kwargs.setdefault("codex_agent", {})["retry_stats"] = retry_stats
            except Exception:
                pass
        return model_response

# --- Optional self-registration (env-gated) -----------------------------------
try:
    if os.getenv("LITELLM_ENABLE_CODEX_AGENT", "") == "1":
        # Try a central registry first
        try:
            from litellm.llms import PROVIDER_REGISTRY  # type: ignore
            PROVIDER_REGISTRY["codex-agent"] = CodexAgentLLM
            PROVIDER_REGISTRY["codex_cli_agent"] = CodexAgentLLM
            # Friendly aliases used by teams/scripts
            PROVIDER_REGISTRY["code-agent"] = CodexAgentLLM
            PROVIDER_REGISTRY["code_agent"] = CodexAgentLLM
        except Exception:
            # Fall back to a helper registration function if available
            try:
                from litellm.llms.custom_llm import register_custom_provider  # type: ignore
                register_custom_provider("codex-agent", CodexAgentLLM)
                register_custom_provider("codex_cli_agent", CodexAgentLLM)
                register_custom_provider("code-agent", CodexAgentLLM)
                register_custom_provider("code_agent", CodexAgentLLM)
            except Exception:
                pass
except Exception:
    pass
