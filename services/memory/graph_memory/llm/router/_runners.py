"""Tier execution handlers and helper functions.

Implements _run_heuristic, _run_small_gpt (HTTP/GGUF/HF backends),
_run_scillm, model loaders, JSON extraction, and prompt building.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Optional, Tuple

from loguru import logger

from ._models import RouteResult, Tier, TierConfig


# Re-export _log from client for internal use
def _client_log(event: str, **fields: Any) -> None:
    """Wrapper around client._log to avoid circular imports at module level."""
    from graph_memory.llm.client import _log
    _log(event, **fields)


# ---------------------------------------------------------------------------
# Tier handlers
# ---------------------------------------------------------------------------

def _run_heuristic(
    config: TierConfig, input_data: Dict[str, Any]
) -> RouteResult:
    """Execute a Tier 0 heuristic handler."""
    t0 = time.time()
    try:
        if config.handler is None:
            raise ValueError("Heuristic tier has no handler")

        output = config.handler(input_data)
        latency_ms = int((time.time() - t0) * 1000)

        confidence = float(output.get("confidence", 0.0))
        skip = output.get("skip", False)

        return RouteResult(
            payload=output.get("result", output),
            tier_used=Tier.HEURISTIC,
            tier_name=config.name,
            confidence=confidence,
            latency_ms=latency_ms,
            model=None,
            escalated=skip or confidence < config.min_confidence,
            escalation_reason=(
                "handler_skip" if skip
                else f"confidence_{confidence:.2f}_below_{config.min_confidence:.2f}"
                if confidence < config.min_confidence
                else None
            ),
        )
    except Exception as exc:
        latency_ms = int((time.time() - t0) * 1000)
        logger.warning("router.heuristic_error: {}", exc)
        _client_log("router.heuristic_error", error=str(exc))
        return RouteResult(
            payload={"error": str(exc)},
            tier_used=Tier.HEURISTIC,
            tier_name=config.name,
            confidence=0.0,
            latency_ms=latency_ms,
            escalated=True,
            escalation_reason=f"error: {exc}",
        )


def _run_small_gpt(
    config: TierConfig, input_data: Dict[str, Any], prompt: str
) -> RouteResult:
    """Execute a Tier 1.5 small GPT inference.

    Supports three backends:
    1. GGUF via llama-cpp-python
    2. HTTP service endpoint
    3. HuggingFace transformers (deferred)
    """
    t0 = time.time()
    model_name = config.model_path or config.model_endpoint or "unknown"

    try:
        # Backend 1: HTTP service endpoint
        if config.model_endpoint:
            return _run_small_gpt_http(config, prompt, t0)

        # Backend 2: GGUF via llama-cpp-python
        if config.model_path and (
            config.model_path.endswith(".gguf")
            or "/gguf/" in config.model_path
        ):
            return _run_small_gpt_gguf(config, prompt, t0)

        # Backend 3: HuggingFace transformers
        if config.model_path:
            return _run_small_gpt_hf(config, prompt, t0)

        raise ValueError("small_gpt tier needs model_path or model_endpoint")

    except Exception as exc:
        latency_ms = int((time.time() - t0) * 1000)
        logger.warning("router.small_gpt_error model={}: {}", model_name, exc)
        _client_log("router.small_gpt_error", model=model_name, error=str(exc))
        return RouteResult(
            payload={"error": str(exc)},
            tier_used=Tier.SMALL_GPT,
            tier_name=config.name,
            confidence=0.0,
            latency_ms=latency_ms,
            model=model_name,
            escalated=True,
            escalation_reason=f"error: {exc}",
        )


def _run_small_gpt_http(
    config: TierConfig, prompt: str, t0: float
) -> RouteResult:
    """Call a small GPT via HTTP endpoint (e.g., create-gpt inference service)."""
    from graph_memory.http_clients import get_httpx_client

    timeout_s = max(1.0, config.timeout_ms / 1000.0)
    payload = {"prompt": prompt}
    if config.system_prompt:
        payload["system"] = config.system_prompt

    resp = get_httpx_client().post(
        f"{config.model_endpoint}/v1/completions",
        json=payload,
        timeout=timeout_s,
    )
    resp.raise_for_status()
    data = resp.json()

    # Extract response -- support OpenAI-compatible and simple formats
    if "choices" in data:
        text = data["choices"][0].get("text", "") or data["choices"][0].get(
            "message", {}
        ).get("content", "")
    else:
        text = data.get("text", data.get("output", ""))

    # Parse JSON from response
    result = _extract_json(text)
    confidence = _extract_confidence(result, data)
    latency_ms = int((time.time() - t0) * 1000)

    return RouteResult(
        payload=result,
        tier_used=Tier.SMALL_GPT,
        tier_name=config.name,
        confidence=confidence,
        latency_ms=latency_ms,
        model=config.model_endpoint,
        escalated=confidence < config.min_confidence,
        escalation_reason=(
            f"confidence_{confidence:.2f}_below_{config.min_confidence:.2f}"
            if confidence < config.min_confidence
            else None
        ),
    )


def _run_small_gpt_gguf(
    config: TierConfig, prompt: str, t0: float
) -> RouteResult:
    """Run inference via llama-cpp-python on a GGUF model."""
    try:
        from llama_cpp import Llama  # type: ignore  # noqa: F401
    except ImportError as exc:
        raise ImportError("llama-cpp-python required for GGUF inference") from exc

    # Cache the model instance (module-level singleton per path)
    model = _get_gguf_model(config.model_path)

    messages = []
    if config.system_prompt:
        messages.append({"role": "system", "content": config.system_prompt})
    messages.append({"role": "user", "content": prompt})

    response = model.create_chat_completion(
        messages=messages,
        response_format={"type": "json_object"},
        max_tokens=config.max_tokens or 512,
        temperature=0.2,
    )

    text = response["choices"][0]["message"]["content"]
    result = _extract_json(text)
    confidence = _extract_confidence(result, response)
    latency_ms = int((time.time() - t0) * 1000)

    return RouteResult(
        payload=result,
        tier_used=Tier.SMALL_GPT,
        tier_name=config.name,
        confidence=confidence,
        latency_ms=latency_ms,
        model=config.model_path,
        escalated=confidence < config.min_confidence,
        escalation_reason=(
            f"confidence_{confidence:.2f}_below_{config.min_confidence:.2f}"
            if confidence < config.min_confidence
            else None
        ),
    )


def _run_small_gpt_hf(
    config: TierConfig, prompt: str, t0: float
) -> RouteResult:
    """Run inference via HuggingFace transformers."""
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer  # type: ignore  # noqa: F401
    except ImportError as exc:
        raise ImportError("transformers required for HF inference") from exc

    model, tokenizer = _get_hf_model(config.model_path)

    messages = []
    if config.system_prompt:
        messages.append({"role": "system", "content": config.system_prompt})
    messages.append({"role": "user", "content": prompt})

    text_input = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    inputs = tokenizer(text_input, return_tensors="pt").to(model.device)

    with torch.no_grad():
        outputs = model.generate(
            **inputs,
            max_new_tokens=config.max_tokens or 512,
            temperature=0.2,
            do_sample=True,
        )

    generated = tokenizer.decode(
        outputs[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True
    )
    result = _extract_json(generated)
    confidence = _extract_confidence(result, {})
    latency_ms = int((time.time() - t0) * 1000)

    return RouteResult(
        payload=result,
        tier_used=Tier.SMALL_GPT,
        tier_name=config.name,
        confidence=confidence,
        latency_ms=latency_ms,
        model=config.model_path,
        escalated=confidence < config.min_confidence,
        escalation_reason=(
            f"confidence_{confidence:.2f}_below_{config.min_confidence:.2f}"
            if confidence < config.min_confidence
            else None
        ),
    )


def _run_scillm(
    config: TierConfig, prompt: str
) -> RouteResult:
    """Execute a Tier 2 SciLLM API call (always-accept fallback)."""
    from graph_memory.llm.client import call_llm_json, resolve_model

    t0 = time.time()
    model_id = config.model
    if not model_id:
        model_id, _ = resolve_model()

    try:
        payload, used_model = call_llm_json(
            prompt,
            profile=config.profile,
            timeout_ms=config.timeout_ms,
            max_tokens=config.max_tokens,
            model=model_id,
        )

        confidence = _extract_confidence(payload, {})
        latency_ms = int((time.time() - t0) * 1000)

        return RouteResult(
            payload=payload,
            tier_used=Tier.SCILLM,
            tier_name=config.name,
            confidence=max(confidence, 0.5),  # SciLLM floor: at least 0.5
            latency_ms=latency_ms,
            model=used_model,
            escalated=False,
        )
    except Exception as exc:
        latency_ms = int((time.time() - t0) * 1000)
        logger.warning("router.scillm_error model={}: {}", model_id, exc)
        _client_log("router.scillm_error", model=model_id, error=str(exc))
        return RouteResult(
            payload={"error": str(exc)},
            tier_used=Tier.SCILLM,
            tier_name=config.name,
            confidence=0.0,
            latency_ms=latency_ms,
            model=model_id,
            escalated=False,
            escalation_reason=f"scillm_error: {exc}",
        )


# ---------------------------------------------------------------------------
# Model singletons (avoid reloading on every call)
# ---------------------------------------------------------------------------

_gguf_models: Dict[str, Any] = {}
_hf_models: Dict[str, Tuple[Any, Any]] = {}


def _get_gguf_model(model_path: str) -> Any:
    if model_path not in _gguf_models:
        from llama_cpp import Llama  # type: ignore

        _client_log("router.loading_gguf", path=model_path)
        _gguf_models[model_path] = Llama(
            model_path=model_path,
            n_ctx=2048,
            n_gpu_layers=-1,  # Use all GPU layers
            verbose=False,
        )
    return _gguf_models[model_path]


def _get_hf_model(model_path: str) -> Tuple[Any, Any]:
    if model_path not in _hf_models:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer  # type: ignore

        _client_log("router.loading_hf", path=model_path)
        tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
        model = AutoModelForCausalLM.from_pretrained(
            model_path,
            torch_dtype=torch.float16,
            device_map="auto",
            trust_remote_code=True,
        )
        _hf_models[model_path] = (model, tokenizer)
    return _hf_models[model_path]


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> Dict[str, Any]:
    """Extract JSON from LLM output text."""
    if not text:
        return {"error": "empty_response"}
    try:
        return json.loads(text)
    except Exception as exc:
        logger.error("Suppressed error in router JSON parse: {}", exc)
    # Try extracting from code block
    try:
        s = text.find("{")
        e = text.rfind("}")
        if s != -1 and e != -1 and e > s:
            return json.loads(text[s : e + 1])
    except Exception as exc:
        logger.error("Suppressed error in router JSON fallback parse: {}", exc)
    return {"error": "json_parse_failed", "raw": text[:500]}


def _extract_confidence(
    result: Dict[str, Any], raw_response: Any
) -> float:
    """Extract confidence from result payload or raw response.

    Sources (in priority order):
    1. Explicit "confidence" field in result
    2. Explicit "score" field in result
    3. Logprobs from raw response
    4. Default 0.5
    """
    # Explicit confidence field
    if "confidence" in result:
        try:
            return float(result["confidence"])
        except (ValueError, TypeError):
            pass

    # Score field (common in assessment outputs)
    if "score" in result:
        try:
            return float(result["score"])
        except (ValueError, TypeError):
            pass

    # Logprobs from raw response
    if isinstance(raw_response, dict):
        choices = raw_response.get("choices", [])
        if choices and "logprobs" in choices[0]:
            logprobs = choices[0]["logprobs"]
            if logprobs and "token_logprobs" in logprobs:
                import math
                probs = logprobs["token_logprobs"]
                valid = [p for p in probs if p is not None]
                if valid:
                    avg_logprob = sum(valid) / len(valid)
                    return min(1.0, max(0.0, math.exp(avg_logprob)))

    # Format validation as proxy
    if "error" not in result and result:
        return 0.5

    return 0.0


def _default_prompt_builder(input_data: Dict[str, Any]) -> str:
    """Default prompt builder -- serializes input as JSON."""
    return json.dumps(input_data, ensure_ascii=False, indent=2)
