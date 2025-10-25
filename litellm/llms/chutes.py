from __future__ import annotations

"""
Custom provider: chutes

Frictionless dynamic chute lifecycle integrated into scillm/litellm surface.
- Parses model strings like: "chutes:<chute_name>/<model_id>"
- On first use (guarded by SCILLM_ENABLE_CHUTES_AUTOSTART=1), builds/deploys the
  chute via scillm.extras.chutes.ensure(), waits for readiness, then forwards a
  JSON-mode OpenAI-compatible request to the chute endpoint.

Safety:
- Disabled unless SCILLM_ENABLE_CHUTES_AUTOSTART=1 (to avoid unexpected costs).
- Honors CHUTES_TTL_SEC for reuse and chutes_ephemeral for deletion.
"""

import os
from typing import Any, Dict, Optional

from litellm.llms.custom_llm import CustomLLM, register_custom_provider
from litellm.utils import ModelResponse

from scillm.extras.chutes import ensure as _ensure_chute, infer as _infer_chute, close as _close_chute


def _parse_model(model: str) -> tuple[str, str]:
    # Expect chutes:<name>/<model_id>
    if model.startswith("chutes:") and "/" in model:
        rest = model.split(":", 1)[1]
        chute_name, mdl = rest.split("/", 1)
        return chute_name, mdl
    # Fallback: model is the target model; require chute_name in optional params
    return "", model


class ChutesLLM(CustomLLM):
    def __init__(self) -> None:
        super().__init__()

    def completion(
        self,
        model: str,
        messages: list,
        api_base: str,
        custom_prompt_dict: Dict[str, Any],
        model_response: ModelResponse,
        print_verbose: Any,
        encoding: Any,
        api_key: Optional[str],
        logging_obj: Any,
        optional_params: Dict[str, Any],
        acompletion=None,
        litellm_params=None,
        logger_fn=None,
        headers: Dict[str, str] = {},
        timeout: Optional[float] = None,
        client: Any = None,
    ) -> ModelResponse:
        # Guard to avoid surprise deploy costs
        if (os.getenv("SCILLM_ENABLE_CHUTES_AUTOSTART", "").lower() not in {"1", "true", "yes"}):
            raise RuntimeError("Chutes provider disabled; set SCILLM_ENABLE_CHUTES_AUTOSTART=1 to enable")

        chute_name, mdl = _parse_model(model)
        if not chute_name:
            chute_name = str(optional_params.get("chute_name") or os.getenv("CHUTES_CHUTE_NAME") or "").strip()
        if not chute_name:
            raise ValueError("chute_name not provided (use model='chutes:<name>/<model_id>' or optional chute_name=...) ")

        ttl_sec = optional_params.get("chutes_ttl_sec")
        if ttl_sec is None:
            env_ttl = os.getenv("CHUTES_TTL_SEC")
            ttl_sec = float(env_ttl) if env_ttl else None
        ephemeral = bool(optional_params.get("chutes_ephemeral", False))

        # Cost knobs (optional)
        max_tokens = optional_params.get("max_tokens")
        temperature = optional_params.get("temperature")
        top_p = optional_params.get("top_p")
        seed = optional_params.get("seed")

        ch = _ensure_chute(chute_name, ttl_sec=ttl_sec)
        out = _infer_chute(
            ch,
            model=mdl,
            messages=messages,
            response_format=optional_params.get("response_format"),
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            seed=seed,
        )
        try:
            if ephemeral:
                _close_chute(chute_name)
        except Exception:
            pass

        mr = model_response or ModelResponse()
        mr.model = model
        mr.choices = out.get("choices", [])
        extra = {"base_url": ch.base_url, "name": ch.name}
        if getattr(ch, "warmup_seconds", None) is not None:
            extra["warmup_seconds"] = float(ch.warmup_seconds)  # type: ignore
        mr.additional_kwargs = {"chutes": extra}
        return mr

    async def acompletion(
        self,
        model: str,
        messages: list,
        api_base: str,
        custom_prompt_dict: Dict[str, Any],
        model_response: ModelResponse,
        print_verbose: Any,
        encoding: Any,
        api_key: Optional[str],
        logging_obj: Any,
        optional_params: Dict[str, Any],
        acompletion=None,
        litellm_params=None,
        logger_fn=None,
        headers: Dict[str, str] = {},
        timeout: Optional[float] = None,
        client: Any = None,
    ) -> ModelResponse:
        # Mirror completion() but using async ensure/infer helpers
        optional_params = optional_params or {}
        if (os.getenv("SCILLM_ENABLE_CHUTES_AUTOSTART", "").lower() not in {"1", "true", "yes"}):
            raise RuntimeError("Chutes provider disabled; set SCILLM_ENABLE_CHUTES_AUTOSTART=1 to enable")

        chute_name, mdl = _parse_model(model)
        if not chute_name:
            chute_name = str(optional_params.get("chute_name") or os.getenv("CHUTES_CHUTE_NAME") or "").strip()
        if not chute_name:
            raise ValueError("chute_name not provided (use model='chutes:<name>/<model_id>' or optional chute_name=...) ")

        ttl_sec = optional_params.get("chutes_ttl_sec")
        if ttl_sec is None:
            env_ttl = os.getenv("CHUTES_TTL_SEC")
            ttl_sec = float(env_ttl) if env_ttl else None
        ephemeral = bool(optional_params.get("chutes_ephemeral", False))

        # Cost knobs (optional)
        max_tokens = optional_params.get("max_tokens")
        temperature = optional_params.get("temperature")
        top_p = optional_params.get("top_p")
        seed = optional_params.get("seed")

        # Async ensure/infer
        from scillm.extras.chutes import aensure as _aensure_chute, ainfer as _ainfer_chute, aclose as _aclose_chute
        ch = await _aensure_chute(chute_name, ttl_sec=ttl_sec)
        out = await _ainfer_chute(
            ch,
            model=mdl,
            messages=messages,
            response_format=optional_params.get("response_format"),
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            seed=seed,
        )
        try:
            if ephemeral:
                await _aclose_chute(chute_name)
        except Exception:
            pass

        mr = model_response or ModelResponse()
        mr.model = model
        mr.choices = out.get("choices", [])
        extra = {"base_url": ch.base_url, "name": ch.name}
        if getattr(ch, "warmup_seconds", None) is not None:
            extra["warmup_seconds"] = float(ch.warmup_seconds)  # type: ignore
        mr.additional_kwargs = {"chutes": extra}
        return mr


# Register provider
register_custom_provider("chutes", ChutesLLM())
