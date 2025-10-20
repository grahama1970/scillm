"""
Multi-agent convenience helpers for SciLLM.

Goals
-----
- Make it trivial to:
  1) Spawn N codex-agent calls in parallel and pick a best answer via a judge model (text Q&A).
  2) Apply CodeWorld MCTS to code tasks (variants provided or auto-generated) in one call.

Usage (text):
  from scillm.extras.multi_agents import answer_text_multi
  out = answer_text_multi(
      messages=[{"role":"user","content":"Explain quicksort in 3 bullets."}],
      model_ids=["<MODEL_ID_A>", "<MODEL_ID_B>"],
      judge_model="openai/gpt-4o-mini",
      codex_api_base=os.getenv("CODEX_AGENT_API_BASE")
  )
  print(out["best_index"], out["answers"][out["best_index"]][:60])

Usage (code + MCTS):
  from scillm.extras.multi_agents import answer_code_mcts
  items=[{"task":"fib","context":{"code_variants":{
           "a":"def solve(ctx): return 1",
           "b":"def solve(ctx): return 2"}}}]
  resp = answer_code_mcts(items, codeworld_base="http://127.0.0.1:8888", rollouts=24, depth=6)
  print(resp["results"][0]["mcts"]["best_variant"])  # 'a' or 'b'

Notes
-----
- These are thin wrappers over existing Router fan-out utilities and the CodeWorld provider.
- Keep responsibilities minimal; callers can customize prompts or judges as needed.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

import litellm
from litellm import Router, completion
from litellm.extras.codex_bootstrap import ensure_codex_agent

_SCILLM_DEBUG = str(os.getenv("SCILLM_DEBUG", "")).lower() in {"1", "true", "yes"}


def _dbg(msg: str) -> None:
    if _SCILLM_DEBUG:
        try:
            print(f"[multi_agents][debug] {msg}")
        except Exception:
            pass
from litellm.router_utils.parallel_acompletion import (
    RouterParallelRequest,
    gather_parallel_acompletions,
)


# ----------------------------- Text fan-out + judge -----------------------------

def _provider_kwargs_for_codex(api_base: Optional[str], api_key: Optional[str]) -> Dict[str, Any]:
    kw: Dict[str, Any] = {"custom_llm_provider": "codex-agent"}
    if api_base:
        kw["api_base"] = api_base
    if api_key:
        kw["api_key"] = api_key
    return kw


# ----------------------------- Judge model resolution -----------------------------

def _fetch_models(base: str, timeout: float = 5.0) -> List[Dict[str, Any]]:
    try:
        import urllib.request as rq, json as _json
        url = base.rstrip("/") + "/v1/models"
        with rq.urlopen(rq.Request(url=url), timeout=timeout) as resp:
            payload = _json.loads(resp.read().decode("utf-8", "ignore"))
            data = payload.get("data") if isinstance(payload, dict) else None
            return data if isinstance(data, list) else []
    except Exception as e:
        _dbg(f"models fetch error base={base} err={type(e).__name__}:{str(e)[:120]}")
        return []


def _is_chat_capable(m: Dict[str, Any]) -> bool:
    try:
        caps = m.get("capabilities")
        if isinstance(caps, dict) and caps.get("chat") is True:
            return True
        mid = str(m.get("id", "")).lower()
        return not any(x in mid for x in ("embed", "rerank", "tts", "image"))
    except Exception:
        return False


def _resolve_judge_model(judge_model: Optional[str]) -> Tuple[str, str]:
    """Return (resolved_model_id, source) where source is 'env'|'auto'|'user'."""
    env_id = os.getenv("CODEX_JUDGE_MODEL")
    if env_id:
        return env_id, "env"
    if judge_model and judge_model != "auto":
        return judge_model, "user"
    base = os.getenv("CODEX_AGENT_API_BASE") or os.getenv("CODEX_BASE") or "http://127.0.0.1:8089"
    models = _fetch_models(base)
    preferred = ["gpt-5", "gpt-4o", "claude", "deepseek", "qwen", "mixtral", "llama"]
    best: Optional[str] = None
    for p in preferred:
        for m in models:
            mid = str(m.get("id", ""))
            if p in mid and _is_chat_capable(m):
                best = mid
                break
        if best:
            break
    if not best:
        for m in models:
            if _is_chat_capable(m):
                best = str(m.get("id"))
                break
    if not best:
        best = "gpt-5"
    _dbg(f"judge auto-select id={best}")
    return best, "auto"


async def _aspawn_codex_agents(
    messages: List[Dict[str, Any]],
    model_ids: Sequence[str],
    codex_api_base: Optional[str] = None,
    codex_api_key: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    concurrency: int = 8,
) -> List[Dict[str, Any]]:
    """Run multiple codex-agent models in parallel and return OpenAI-shaped responses.

    Returns a list aligned with model_ids, each an OpenAI-style dict or minimal
    aggregated dict with choices[0].message.content.
    """
    router = Router()
    _dbg(f"spawn codex models={list(model_ids)} concurrency={concurrency}")
    reqs: List[RouterParallelRequest] = []
    extra = _provider_kwargs_for_codex(codex_api_base, codex_api_key)

    for mid in model_ids:
        reqs.append(
            RouterParallelRequest(
                model=mid,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                kwargs=extra,
            )
        )

    results = await gather_parallel_acompletions(router, reqs, concurrency=concurrency)

    out: List[Dict[str, Any]] = []
    for r in results:
        resp = r.response
        # Normalize common shapes to OpenAI-like dict
        if resp is None:
            out.append({"choices": [{"message": {"content": ""}}], "error": str(r.exception) if r.exception else ""})
        elif hasattr(resp, "choices"):
            try:
                # ModelResponse-like
                content = resp.choices[0].message["content"]  # type: ignore[index]
            except Exception:
                try:
                    content = resp.choices[0].message.content
                except Exception:
                    content = None
            out.append({"choices": [{"message": {"content": content}}]})
        elif isinstance(resp, dict):
            out.append(resp)
        else:
            out.append({"choices": [{"message": {"content": str(resp)}}]})
    # Reorder to original input order by index
    out_ordered = [None] * len(out)
    for r, d in zip(results, out):
        out_ordered[r.index] = d
    # Debug preview
    try:
        previews = []
        for d in out_ordered:
            c = None
            try:
                c = d["choices"][0]["message"]["content"]
            except Exception:
                pass
            previews.append((c or "")[:60])
        _dbg(f"spawn previews={previews}")
    except Exception:
        pass
    return out_ordered  # type: ignore[return-value]


def spawn_codex_agents(
    messages: List[Dict[str, Any]],
    model_ids: Sequence[str],
    codex_api_base: Optional[str] = None,
    codex_api_key: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    concurrency: int = 8,
) -> List[Dict[str, Any]]:
    """Sync wrapper around _aspawn_codex_agents for simple callers."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():  # In async environment, require explicit await by caller
        raise RuntimeError("spawn_codex_agents() called from an active event loop; use _aspawn_codex_agents().")
    return asyncio.run(
        _aspawn_codex_agents(
            messages,
            model_ids,
            codex_api_base,
            codex_api_key,
            temperature,
            max_tokens,
            concurrency,
        )
    )


def _judge_messages(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    sys = (
        "You are a strict judge. Given a user question and N model answers, "
        "choose the single best answer focusing on correctness, clarity, and grounding. "
        "Return JSON only: {best_index:int, rationale_short:string}."
    )
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def answer_text_multi(
    messages: List[Dict[str, Any]],
    model_ids: Sequence[str],
    judge_model: str,
    *,
    codex_api_base: Optional[str] = None,
    codex_api_key: Optional[str] = None,
    judge_via_codex: bool = True,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = 512,
    timeout: float = 60.0,
) -> Dict[str, Any]:
    """Run N codex-agent models and pick the best answer using a judge model.

    Returns dict with keys: answers (List[str]), best_index (int), judge_raw (dict).
    """
    # 1) Fan-out
    responses = spawn_codex_agents(
        messages=messages,
        model_ids=model_ids,
        codex_api_base=codex_api_base,
        codex_api_key=codex_api_key,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    answers: List[str] = []
    for resp in responses:
        content = None
        try:
            content = resp["choices"][0]["message"]["content"]
        except Exception:
            pass
        answers.append(content if isinstance(content, str) else "")

    # 2) Judge
    judge_payload = {"messages": messages, "answers": answers}
    judge_kwargs: Dict[str, Any] = {"request_timeout": timeout, "temperature": 0, "max_tokens": 128}
    judge_content = None
    if judge_via_codex:
        # Try direct sidecar call first for consistency with scillm codex flows
        base = codex_api_base or os.getenv("CODEX_AGENT_API_BASE") or "http://127.0.0.1:8089"
        # Auto-select judge model if requested or via env override
        jm, jsrc = _resolve_judge_model(judge_model)
        if jsrc != "user":
            judge_model = jm
            _dbg(f"text judge using model={judge_model} src={jsrc}")
        try:
            import json as _json, urllib.request as _rq
            # Normalize common aliases 'codex/<id>' and 'codex-agent/<id>' to '<id>' for chat endpoints
            mid = str(judge_model)
            if mid.startswith("codex-agent/"):
                mid = mid.split("/", 1)[1]
            elif mid.startswith("codex/"):
                mid = mid.split("/", 1)[1]
            req = _rq.Request(
                url=base.rstrip("/") + "/v1/chat/completions",
                data=_json.dumps({
                    "model": mid,
                    "messages": _judge_messages(judge_payload),
                    "temperature": 0,
                    "max_tokens": 128,
                    "response_format": {"type": "json_object"},
                    "reasoning_effort": "low",
                    "reasoning": {"effort": "low"},
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _rq.urlopen(req, timeout=float(timeout)) as resp:
                obj = _json.loads(resp.read().decode("utf-8", "ignore"))
                judge_content = (obj.get("choices", [{}])[0].get("message", {}) or {}).get("content")
            _dbg("text judge via sidecar ok")
        except Exception as e:
            _dbg(f"text judge sidecar_error err={type(e).__name__}:{str(e)[:120]} -> fallback to litellm")
    if judge_content is None:
        if judge_via_codex:
            judge_kwargs.update(_provider_kwargs_for_codex(codex_api_base, codex_api_key))
        judge_resp = litellm.completion(
            model=judge_model,
            messages=_judge_messages(judge_payload),
            response_format={"type": "json_object"},
            allowed_openai_params=["reasoning_effort","reasoning","reasoning_tokens"],
            reasoning_effort="low" if judge_via_codex else None,
            **judge_kwargs,
        )
        try:
            judge_content = judge_resp.choices[0].message["content"]  # type: ignore[index]
        except Exception:
            judge_content = getattr(judge_resp.choices[0].message, "content", None)
    try:
        parsed = json.loads(judge_content) if isinstance(judge_content, str) else {}
    except Exception as e:
        _dbg(f"judge parse_error err={type(e).__name__}:{str(e)[:120]} raw={str(judge_content)[:160]}")
        parsed = {"best_index": 0, "rationale_short": "parse_error"}
    _dbg(f"judge model={judge_model} parsed={parsed}")

    best_idx = int(parsed.get("best_index", 0) or 0)
    best_idx = max(0, min(best_idx, len(answers) - 1))

    return {
        "answers": answers,
        "best_index": best_idx,
        "judge_raw": parsed,
    }


# ----------------------------- Code (MCTS via CodeWorld) ------------------------

def answer_code_mcts(
    items: List[Dict[str, Any]],
    codeworld_base: Optional[str],
    *,
    rollouts: Optional[int] = None,
    depth: Optional[int] = None,
    uct_c: Optional[float] = None,
    autogenerate_n: Optional[int] = None,
    temperature: float = 0.0,
) -> Dict[str, Any]:
    """Apply MCTS to code tasks via the CodeWorld provider.

    If autogenerate_n is set, requests the bridge to generate that many code
    variants before running MCTS ("codeworld/mcts:auto"). Otherwise expects
    variants under items[*].context.code_variants.
    """
    model = "codeworld/mcts:auto" if autogenerate_n and autogenerate_n > 0 else "codeworld/mcts"
    kwargs: Dict[str, Any] = {"custom_llm_provider": "codeworld"}
    if codeworld_base:
        kwargs["api_base"] = codeworld_base

    if model == "codeworld/mcts:auto":
        # Pass autogen knobs using documented parameter names
        if autogenerate_n is not None:
            kwargs["n_variants"] = int(autogenerate_n)
        kwargs["temperature"] = temperature

    if rollouts is not None:
        kwargs["rollouts"] = int(rollouts)
    if depth is not None:
        kwargs["depth"] = int(depth)
    if uct_c is not None:
        kwargs["uct_c"] = float(uct_c)

    return completion(model=model, items=items, **kwargs)


def answer_code_mcts_autogen(
    items: List[Dict[str, Any]],
    *,
    n_variants: int = 6,
    rollouts: Optional[int] = None,
    depth: Optional[int] = None,
    uct_c: Optional[float] = None,
    temperature: float = 0.0,
    codeworld_base: Optional[str] = None,
    autostart_codex: bool = True,
    request_timeout: float = 120.0,
) -> Dict[str, Any]:
    """Ensure codex-agent is available, then autogenerate N variants and run MCTS."""
    if autostart_codex:
        ensure_codex_agent()
    kwargs: Dict[str, Any] = {"custom_llm_provider": "codeworld"}
    if codeworld_base:
        kwargs["api_base"] = codeworld_base
    kwargs["n_variants"] = int(n_variants)
    kwargs["temperature"] = float(temperature)
    if rollouts is not None:
        kwargs["rollouts"] = int(rollouts)
    if depth is not None:
        kwargs["depth"] = int(depth)
    if uct_c is not None:
        kwargs["uct_c"] = float(uct_c)
    kwargs["request_timeout"] = float(request_timeout)
    return completion(model="mcts:auto", items=items, **kwargs)


def _codex_judge_messages(task: str, variants: Dict[str, str], metrics: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    sys = (
        "You are a senior systems engineer and code reviewer. Given a task, a set of code variants, and optional metrics, "
        "decide which variant is best for a real‑time gaming plugin. Weigh numerical stability, predictable latency, maintainability, and SIMD/ISA use. "
        "Return STRICT JSON only: {best_id: string, rationale_short: string}."
    )
    payload = {
        "task": task,
        "variants": [{"id": k, "code": v, "loc": len((v or '').splitlines())} for k, v in variants.items()],
        "metrics": metrics or {},
    }
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def codex_judge_codeworld_result(
    codeworld_payload: Dict[str, Any],
    *,
    judge_model: str = "gpt-5",
    timeout: float = 90.0,
    codex_api_base: Optional[str] = None,
) -> Dict[str, Any]:
    """Judge CodeWorld result with a codex-agent high-reasoning call.

    Expects `codeworld_payload` as returned under resp.additional_kwargs['codeworld'].
    Returns {best_id, rationale_short} parsed from the judge output.
    """
    ensure_codex_agent()  # make sure provider is up
    # Extract variants from payload (supports both mapping and autogen list)
    try:
        results = codeworld_payload.get("results") or []
        item = (results[0] if results else {})
        ctx = (item.get("item") or {}).get("context") or {}
        variants_map: Dict[str, str] = {}
        if isinstance(ctx.get("code_variants"), dict):
            for k, v in ctx.get("code_variants").items():
                if isinstance(v, str):
                    variants_map[str(k)] = v
                elif isinstance(v, dict) and isinstance(v.get("code"), str):
                    variants_map[str(k)] = v.get("code")
        elif isinstance(item.get("code_variants"), list):
            for v in item.get("code_variants"):
                if isinstance(v, dict) and isinstance(v.get("id"), str) and isinstance(v.get("code"), str):
                    variants_map[v["id"]] = v["code"]
        task = (item.get("item") or {}).get("task") or "code task"
        metrics = item.get("scores") or {}
    except Exception:
        variants_map, task, metrics = {}, "code task", {}

    if not variants_map:
        return {"best_id": None, "rationale_short": "no_variants"}

    # Always judge via codex-agent sidecar with reasoning allowed like other scillm codex calls
    base = (codex_api_base or os.getenv("CODEX_AGENT_API_BASE") or os.getenv("CODEX_BASE") or "http://127.0.0.1:8089").rstrip("/")
    jm, jsrc = _resolve_judge_model(judge_model)
    if jsrc != "user":
        judge_model = jm
    _dbg(f"codex_judge base={base} model={judge_model} src={jsrc}")
    # Prefer a direct sidecar POST to avoid provider routing differences and API key requirements
    content = None
    try:
        import json as _json
        import urllib.request as _rq
        # Normalize aliases 'codex/<id>' and 'codex-agent/<id>' to '<id>' for chat endpoints
        mid = str(judge_model)
        if mid.startswith("codex-agent/"):
            mid = mid.split("/", 1)[1]
        elif mid.startswith("codex/"):
            mid = mid.split("/", 1)[1]
        payload = {
            "model": mid,
            "messages": _codex_judge_messages(str(task), variants_map, metrics),
            "temperature": 0,
            "max_tokens": 256,
            # reasoning passthrough compatible with scillm codex-agent
            "reasoning_effort": "high",
            "reasoning": {"effort": "high"},
            "response_format": {"type": "json_object"},
        }
        data = _json.dumps(payload).encode("utf-8")
        req = _rq.Request(url=base + "/v1/chat/completions", data=data, headers={"Content-Type": "application/json"}, method="POST")
        with _rq.urlopen(req, timeout=float(timeout)) as resp:
            obj = _json.loads(resp.read().decode("utf-8", "ignore"))
            try:
                content = obj.get("choices", [{}])[0].get("message", {}).get("content")
            except Exception:
                content = None
        _dbg("codex_judge via sidecar ok")
    except Exception as e:
        _dbg(f"codex_judge sidecar_error err={type(e).__name__}:{str(e)[:160]}; falling back to litellm")
        try:
            judge_resp = litellm.completion(
                model=judge_model,
                messages=_codex_judge_messages(str(task), variants_map, metrics),
                custom_llm_provider="codex-agent",
                api_base=base,
                response_format={"type": "json_object"},
                request_timeout=timeout,
                allowed_openai_params=["reasoning_effort", "reasoning", "reasoning_tokens"],
                reasoning_effort="high",
            )
            try:
                content = judge_resp.choices[0].message["content"]  # type: ignore[index]
            except Exception:
                content = getattr(judge_resp.choices[0].message, "content", None)
        except Exception as ee:
            _dbg(f"codex_judge litellm_fallback_error err={type(ee).__name__}:{str(ee)[:160]}")
    try:
        data = json.loads(content) if isinstance(content, str) else {}
    except Exception:
        # Try to salvage a JSON object from the text
        try:
            import re as _re
            s = content or ""
            m = _re.search(r"\{.*\}", s, _re.S)
            data = json.loads(m.group(0)) if m else {"best_id": None, "rationale_short": "parse_error"}
        except Exception as e:
            _dbg(f"codex_judge parse_error err={type(e).__name__}:{str(e)[:120]} raw={str(content)[:160]}")
            data = {"best_id": None, "rationale_short": "parse_error"}
    _dbg(f"codex_judge parsed={data}")
    return data


def answer_code_mcts_autogen_and_judge(
    items: List[Dict[str, Any]],
    *,
    n_variants: int = 6,
    rollouts: int = 48,
    depth: int = 6,
    uct_c: float = 1.25,
    temperature: float = 0.0,
    codeworld_base: Optional[str] = None,
    judge_model: str = "codex-agent/gpt-5",
    autostart_codex: bool = True,
    timeout: float = 120.0,
) -> Dict[str, Any]:
    """One-shot: autogenerate variants → MCTS → codex-agent high-reasoning judge.

    Returns a dict with keys: codeworld (payload), judge (parsed JSON).
    """
    if autostart_codex:
        ensure_codex_agent()
    cw = answer_code_mcts_autogen(
        items,
        n_variants=n_variants,
        rollouts=rollouts,
        depth=depth,
        uct_c=uct_c,
        temperature=temperature,
        codeworld_base=codeworld_base,
        autostart_codex=False,
        request_timeout=timeout,
    )
    payload = getattr(cw, "additional_kwargs", {}).get("codeworld") or {}
    judge = codex_judge_codeworld_result(payload, judge_model=judge_model, timeout=timeout)
    return {"codeworld": payload, "judge": judge}


__all__ = [
    "spawn_codex_agents",
    "answer_text_multi",
    "answer_code_mcts",
    "answer_code_mcts_autogen",
    "codex_judge_codeworld_result",
    "answer_code_mcts_autogen_and_judge",
]


# ----------------------------- Codex-only Option A ----------------------------

def _build_codex_autogen_prompt(task: Any, context: Optional[Dict[str, Any]], n: int) -> str:
    try:
        ctx = json.dumps(context or {}, ensure_ascii=False, sort_keys=True)
    except Exception:
        ctx = repr(context)
    t = task if isinstance(task, str) else json.dumps(task, ensure_ascii=False)
    return (
        f"Generate exactly {n} JSON variants for the task below with fields id,title,complexity_tier,rationale,code,notes. "
        f"Return STRICT JSON with top-level key 'variants'.\nTask:\n{t}\nContext:\n{ctx}"
    )


def answer_code_autogen_and_judge_codex_only(
    items: List[Dict[str, Any]],
    *,
    n_variants: int = 6,
    generator_model: str = "gpt-5",
    temperature: float = 0.0,
    max_tokens: int = 2000,
    judge_model: str = "codex-agent/gpt-5",
    timeout: float = 120.0,
    codex_api_base: Optional[str] = None,
) -> Dict[str, Any]:
    """Autogenerate N variants and judge the best using codex-agent only (no CodeWorld).

    Returns dict: {"variants": [...], "judge": {best_id, rationale_short}}
    """
    # 1) Generate variants via codex-agent
    try:
        from scillm.extras.codex import chat as codex_chat  # local import to avoid hard dep on import time
    except Exception as e:
        raise RuntimeError(f"codex helper unavailable: {e}")

    item = items[0] if items else {"task": "code task", "context": {}}
    prompt = _build_codex_autogen_prompt(item.get("task"), item.get("context"), int(n_variants))
    gen = codex_chat(
        messages=[
            {"role": "system", "content": "You produce strict JSON only."},
            {"role": "user", "content": prompt},
        ],
        model=str(generator_model).split("/", 1)[-1],  # normalize codex-agent/<id> → <id>
        temperature=float(temperature),
        max_tokens=int(max_tokens),
        response_format={"type": "json_object"},
        timeout=float(timeout),
        base=codex_api_base,
    )
    content = (((gen.get("choices") or [{}])[0] or {}).get("message", {}) or {}).get("content", "")
    try:
        obj = json.loads(content) if isinstance(content, str) else {}
    except Exception:
        import re as _re
        m = _re.search(r"\{.*\}", content or "", _re.S)
        obj = json.loads(m.group(0)) if m else {}
    variants = obj.get("variants", []) if isinstance(obj, dict) else []
    norm: List[Dict[str, Any]] = []
    for i, v in enumerate(variants, 1):
        if not isinstance(v, dict):
            continue
        vid = v.get("id") or f"v{i}"
        norm.append({
            "id": vid,
            "title": v.get("title"),
            "complexity_tier": v.get("complexity_tier"),
            "rationale": v.get("rationale"),
            "code": v.get("code") if isinstance(v.get("code"), str) else "",
            "notes": v.get("notes"),
        })

    # 2) Judge via codex-agent using the same schema as the CodeWorld judge
    variants_map = {v["id"]: v.get("code", "") for v in norm if isinstance(v.get("id"), str)}
    judge = {"best_id": None, "rationale_short": "no_variants"}
    if variants_map:
        judge = codex_judge_codeworld_result(
            {"results": [{"item": {"task": item.get("task", "code task"), "context": {}}, "code_variants": norm}]},
            judge_model=judge_model,
            timeout=timeout,
            codex_api_base=codex_api_base,
        )
    return {"variants": norm, "judge": judge}


__all__.append("answer_code_autogen_and_judge_codex_only")
