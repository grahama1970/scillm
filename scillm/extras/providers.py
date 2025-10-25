#!/usr/bin/env python3
from __future__ import annotations

"""
Frictionless helpers for CodeWorld and Certainly within scillm.

These provide one-call paved paths with minimal required params and no hidden
switching. They simply shape the payloads expected by the providers and call
`scillm.completion`.
"""

from typing import Any, Dict, List, Optional
import os

from scillm import completion as _completion  # re-exported litellm surface
import json as _json
from scillm.extras.chutes import (
    ensure as _ensure_chute,
    infer as _infer_chute,
    close as _close_chute,
    aensure as _aensure_chute,
    ainfer as _ainfer_chute,
    aclose as _aclose_chute,
)


def codeworld_mcts(
    *,
    messages: List[Dict[str, Any]],
    items: Optional[List[Dict[str, Any]]] = None,
    provider_args: Optional[Dict[str, Any]] = None,
    options: Optional[Dict[str, Any]] = None,
    request_timeout: float = 60.0,
    api_base: Optional[str] = None,
) -> Dict[str, Any]:
    """Run CodeWorld with strategy=mcts via the provider.

    - No env gates required; provider is registered by default.
    - If you want MCTS autogeneration to use Chutes, set:
        OPENAI_BASE_URL=$CHUTES_API_BASE
        OPENAI_API_KEY=$CHUTES_API_KEY
        and pass provider_args={"strategy":"mcts","strategy_config":{"autogenerate":{...}}}
    """
    args = dict(provider_args or {})
    if "strategy" not in args:
        args["strategy"] = "mcts"
    if "strategy_config" not in args:
        args["strategy_config"] = {"name": "mcts"}
    body = {
        "messages": messages,
        "provider": {"name": "codeworld", "args": args},
    }
    if items is not None:
        body["items"] = items
    if options is not None:
        body["options"] = options
    return _completion(
        model="codeworld/mcts",
        custom_llm_provider="codeworld",
        messages=body["messages"],
        timeout=request_timeout,
        api_base=(api_base or os.getenv("CODEWORLD_BASE")),
        # The CodeWorld provider consumes items/provider/options via optional_params
        items=body.get("items"),
        provider=body["provider"],
        options=options,
        request_timeout=request_timeout,
    )


def certainly_prove(
    *,
    items: List[Dict[str, Any]],
    messages: Optional[List[Dict[str, Any]]] = None,
    request_timeout: float = 120.0,
    max_seconds: Optional[float] = None,
    flags: Optional[List[str]] = None,
    session_id: Optional[str] = None,
    track_id: Optional[str] = None,
    api_base: Optional[str] = None,
    require_proved: bool = False,
) -> Dict[str, Any]:
    """Call the Certainly (Lean4) bridge via provider 'certainly' with minimal friction.

    - Accepts items with either 'requirement_text' (canonical) or 'text' (mapped).
    - No env gates required; provider is registered by default. Set CERTAINLY_BRIDGE_BASE if not localhost:8787.
    """
    canon: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if "requirement_text" in it:
            canon.append(it)
        elif "text" in it:
            tmp = dict(it)
            tmp.setdefault("requirement_text", tmp.pop("text"))
            canon.append(tmp)
        else:
            canon.append(it)
    opt: Dict[str, Any] = {}
    # Default LLM-on flags to lower cognitive load when CERTAINLY_LLM_DEFAULT=1
    eff_flags = list(flags or [])
    try:
        _llm_default = (os.getenv("CERTAINLY_LLM_DEFAULT") or "").strip().lower() in {"1", "true", "yes", "on"}
        if not eff_flags and _llm_default:
            model_env = (os.getenv("CERTAINLY_MODEL") or os.getenv("OPENROUTER_DEFAULT_MODEL") or "").strip()
            # Use a conservative default strategy set; CLI validates/adjusts
            eff_flags = [
                "--model", model_env or "",
                "--strategies", "direct,structured,computational",
                "--max-refinements", "1",
                "--best-of",
            ]
    except Exception:
        pass
    if eff_flags:
        opt["flags"] = eff_flags
    if max_seconds is not None:
        opt["max_seconds"] = max_seconds
    options: Dict[str, Any] = {}
    if session_id is not None:
        options["session_id"] = session_id
    if track_id is not None:
        options["track_id"] = track_id
    if options:
        opt["options"] = options
    resp = _completion(
        model="certainly/bridge",
        custom_llm_provider="certainly",
        messages=messages or [{"role": "system", "content": "Certainly/Lean4"}],
        items=canon,
        timeout=request_timeout,
        request_timeout=request_timeout,
        api_base=(api_base or os.getenv("CERTAINLY_BRIDGE_BASE") or os.getenv("LEAN4_BRIDGE_BASE")),
        **opt,
    )
    if require_proved:
        payload = (resp.get("additional_kwargs", {}) or {}).get("certainly", {})
        s = payload.get("summary", {}) if isinstance(payload, dict) else {}
        proved = int((s or {}).get("proved") or 0)
        total = len(canon)
        if proved < total:
            raise RuntimeError(f"Lean4 verification failed: proved={proved} < total={total}")
    return resp


def certainly_prove_and_summarize_with_chutes(
    *,
    items: List[Dict[str, Any]],
    chutes_model: Optional[str] = None,
    bridge_base: Optional[str] = None,
    chutes_base: Optional[str] = None,
    chutes_key: Optional[str] = None,
    request_timeout: float = 60.0,
    require_proved: bool = False,
) -> Dict[str, Any]:
    """
    Run a Lean4/Certainly proof batch, then summarize results using a Chutes model
    via the OpenAI‑compatible paved path (bearer + JSON mode).

    - Proofs: provider 'certainly' (bridge_base or CERTAINLY_BRIDGE_BASE/LEAN4_BRIDGE_BASE)
    - Summary: provider 'openai_like' to CHUTES_API_BASE with Authorization: Bearer CHUTES_API_KEY
    """
    # 1) Run proofs
    proof = certainly_prove(items=items, api_base=bridge_base, request_timeout=request_timeout, require_proved=require_proved)
    # 2) Build a compact summary prompt from the proof payload
    summary_seed = {
        "summary": proof.get("choices", [{}])[0].get("message", {}).get("content", ""),
        "details": (proof.get("additional_kwargs", {}) or {}).get("certainly", {}),
    }
    prompt = (
        "Return strictly JSON with keys: summary, proved, failed, unproved.\n"
        + _json.dumps(summary_seed, ensure_ascii=False)[:4000]
    )
    # 3) Call Chutes JSON mode
    base = (chutes_base or os.getenv("CHUTES_API_BASE") or "").rstrip("/")
    key = (chutes_key or os.getenv("CHUTES_API_KEY") or "").strip()
    model = (chutes_model or os.getenv("CHUTES_TEXT_MODEL") or "").strip()
    if not (base and key and model):
        raise RuntimeError("Chutes config missing: require CHUTES_API_BASE, CHUTES_API_KEY, and CHUTES_TEXT_MODEL or chutes_model")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    summary = _completion(
        model=model,
        custom_llm_provider="openai_like",
        api_base=base,
        api_key=None,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        extra_headers=headers,
        timeout=request_timeout,
        request_timeout=request_timeout,
    )
    return {"proof": proof, "summary": summary}


def certainly_openrouter_chat(
    *,
    messages: List[Dict[str, Any]],
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    api_base: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    timeout: float = 60.0,
    referer: Optional[str] = None,
    title: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Certainly via OpenRouter (formal-methods LLM hosted on OpenRouter).

    Env (recommended):
      - OPENROUTER_API_KEY
      - CERTAINLY_MODEL (e.g., provider/model id from OpenRouter)

    Notes:
      - Uses provider 'openrouter' in scillm/litellm.
      - Optionally sets HTTP-Referer and X-Title headers per OpenRouter guidance.
    """
    m = (model or os.getenv("CERTAINLY_MODEL") or os.getenv("OPENROUTER_DEFAULT_MODEL") or "").strip()
    if not m:
        raise RuntimeError("CERTAINLY_MODEL not set; set CERTAINLY_MODEL in .env or pass model=…")
    key = (api_key or os.getenv("OPENROUTER_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set in env or api_key not provided")
    base = (api_base or os.getenv("OPENROUTER_API_BASE") or "https://openrouter.ai/api/v1").rstrip("/")
    headers: Dict[str, str] = {}
    if referer:
        headers["HTTP-Referer"] = referer
    if title:
        headers["X-Title"] = title
    resp = _completion(
        model=m,
        custom_llm_provider="openrouter",
        api_key=key,
        api_base=base,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        extra_headers=headers or None,
    )
    return resp


def chutes_completion(
    *,
    chute_name: str,
    model: str,
    messages: List[Dict[str, Any]],
    json_mode: bool = True,
    ttl_sec: Optional[float] = None,
    ephemeral: bool = False,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Frictionless Chutes call that feels like scillm.completion.

    - Launches (or reuses) the chute via ensure(); waits for readiness.
    - Runs one OpenAI‑compatible completion with JSON mode by default.
    - Optionally deletes the chute when done (ephemeral=True).
    - Honors env CHUTES_API_KEY; header style and model aliasing handled internally.
    """
    ch = _ensure_chute(chute_name, ttl_sec=ttl_sec)
    try:
        rf = {"type": "json_object"} if json_mode else None
        return _infer_chute(
            ch,
            model=model,
            messages=messages,
            response_format=rf,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            seed=seed,
        )
    finally:
        if ephemeral:
            try:
                _close_chute(chute_name)
            except Exception:
                pass


async def chutes_acompletion(
    *,
    chute_name: str,
    model: str,
    messages: List[Dict[str, Any]],
    json_mode: bool = True,
    ttl_sec: Optional[float] = None,
    ephemeral: bool = False,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Async version of chutes_completion using non-blocking wrappers."""
    ch = await _aensure_chute(chute_name, ttl_sec=ttl_sec)
    try:
        rf = {"type": "json_object"} if json_mode else None
        return await _ainfer_chute(
            ch,
            model=model,
            messages=messages,
            response_format=rf,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            seed=seed,
        )
    finally:
        if ephemeral:
            try:
                await _aclose_chute(chute_name)
            except Exception:
                pass
