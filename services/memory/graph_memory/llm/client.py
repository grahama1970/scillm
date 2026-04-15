from __future__ import annotations

import json
import os
import random
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# SCILLM-only paved path: no litellm, no manual headers, no raw HTTP.

DEFAULT_FAST_TIMEOUT_MS = 10_000
DEFAULT_ACCURATE_TIMEOUT_MS = 45_000
MAX_TOKENS_DEFAULT = 512


def _brace_json_extract(text: str) -> Dict[str, Any]:
    try:
        return json.loads(text)
    except Exception as exc:  # was bare
        pass
    try:
        s = text.find('{'); e = text.rfind('}')
        if s != -1 and e != -1 and e > s:
            return json.loads(text[s:e+1])
    except Exception as exc:  # was bare
        pass
    return {'error': 'no-json'}


def _profile_defaults(profile: str) -> Tuple[int, int]:
    if profile.lower() == "accurate":
        return DEFAULT_ACCURATE_TIMEOUT_MS, 3
    return DEFAULT_FAST_TIMEOUT_MS, 2


def _exp_backoff_sleep(attempt: int, base: float = 0.5) -> None:
    delay = base * (2 ** max(0, attempt - 1))
    jitter = random.uniform(0, 0.25)
    time.sleep(delay + jitter)


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    try:
        headers = getattr(getattr(exc, "response", None), "headers", {}) or getattr(exc, "headers", {}) or {}
        ra = headers.get("Retry-After") or headers.get("retry-after")
        return float(ra) if ra is not None else None
    except Exception as exc:  # was bare
        return None


def _log(event: str, **fields: Any) -> None:
    level = os.getenv("LOG_LEVEL", "info").lower()
    want_json = os.getenv("LOG_JSON", "1") in ("1", "true", "TRUE")
    rec = {"event": event, **fields}
    try:
        line = json.dumps(rec, ensure_ascii=False)
    except Exception as exc:
        line = f"{event} {fields}"
    if want_json:
        sys.stderr.write(line + "\n")
    else:
        sys.stderr.write(f"{event} {fields}\n")


def resolve_model(preferred: Optional[str] = None) -> Tuple[str, str]:
    if preferred and str(preferred).strip():
        return str(preferred).strip(), "flag"
    env_model = (os.getenv("GM_LLM_MODEL") or "").strip()
    if env_model:
        return env_model, "env:GM_LLM_MODEL"
    sc_def = (os.getenv("SCILLM_DEFAULT_MODEL") or "").strip()
    if sc_def:
        return sc_def, "env:SCILLM_DEFAULT_MODEL"
    return "text", "fallback"


def call_llm_json(
    prompt: str,
    *,
    profile: Optional[str] = None,
    timeout_ms: Optional[int] = None,
    max_tokens: Optional[int] = None,
    request_id: Optional[str] = None,
    model: Optional[str] = None,
) -> Tuple[Dict[str, Any], str]:
    """Call scillm proxy via httpx. No retries — the proxy handles cascading.

    Per /scillm: httpx to localhost:4001, model "text", Bearer sk-dev-proxy-123.
    SCILLM_API_BASE env var overrides the URL. max_tokens is ignored (stripped by proxy).
    """
    import httpx

    # max_tokens intentionally not passed — causes empty output on reasoning models
    _ = max_tokens  # Keep param for backwards compat
    to_ms = int(os.getenv("GM_LLM_TIMEOUT_MS") or timeout_ms or DEFAULT_FAST_TIMEOUT_MS)
    api_base = os.getenv("SCILLM_API_BASE", "http://localhost:4001")
    api_key = os.getenv("SCILLM_PROXY_KEY", "sk-dev-proxy-123")

    t0 = time.time()
    resp = httpx.post(
        f"{api_base}/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "x-expect-json": "true",  # Enable JSON repair for Claude
        },
        json={
            "model": model or "text",
            "messages": [
                {"role": "system", "content": "Return strict JSON only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": float(os.getenv("GM_LLM_TEMPERATURE", "0.2")),
            "response_format": {"type": "json_object"},
        },
        timeout=max(1.0, float(to_ms) / 1000.0),
    )
    resp.raise_for_status()
    data = resp.json()
    text = data.get("choices", [{}])[0].get("message", {}).get("content")
    if not isinstance(text, str):
        raise RuntimeError("no-text-in-response")
    payload = _brace_json_extract(text)
    eff_model = data.get("model", model or "text")
    _log("llm.ok", backend="scillm", model=eff_model, request_id=request_id, duration_ms=int((time.time()-t0)*1000))
    return payload, eff_model


def call_llm_json_parallel(
    prompts: List[str] | str,
    *,
    profile: Optional[str] = None,
    timeout_ms: Optional[int] = None,
    max_tokens: Optional[int] = None,
    model: Optional[str] = None,
) -> List[Tuple[Dict[str, Any], str]]:
    """Parallel batch via scillm proxy using asyncio + httpx.

    Per /scillm: httpx to proxy, no retries, proxy handles cascading.
    """
    import asyncio
    import httpx

    if isinstance(prompts, str):
        prompt_list = [prompts]
    else:
        prompt_list = list(prompts or [])
    if not prompt_list:
        return []

    api_base = os.getenv("SCILLM_API_BASE", "http://localhost:4001")
    api_key = os.getenv("SCILLM_PROXY_KEY", "sk-dev-proxy-123")
    url = f"{api_base}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "x-expect-json": "true",  # Enable JSON repair for Claude
    }
    timeout_s = max(1, int(timeout_ms) // 1000) if timeout_ms else 30
    sem = asyncio.Semaphore(6)

    async def _one(prompt_text: str) -> Tuple[Dict[str, Any], str]:
        body = {
            "model": model or "text",
            "messages": [
                {"role": "system", "content": "Return strict JSON only."},
                {"role": "user", "content": prompt_text},
            ],
            "response_format": {"type": "json_object"},
            "temperature": float(os.getenv("GM_LLM_TEMPERATURE", "0.2")),
        }
        async with sem:
            async with httpx.AsyncClient(timeout=timeout_s) as ac:
                resp = await ac.post(url, json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                eff_model = data.get("model", model or "text")
                return (_brace_json_extract(text) if isinstance(text, str) else {"error": "no-text"}, eff_model)

    async def _run():
        tasks = [_one(p) for p in prompt_list]
        return await asyncio.gather(*tasks, return_exceptions=True)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            resps = pool.submit(asyncio.run, _run()).result()
    else:
        resps = asyncio.run(_run())

    results: List[Tuple[Dict[str, Any], str]] = []
    for r in resps:
        if isinstance(r, Exception):
            results.append(({"error": str(r)}, model or "text"))
        elif isinstance(r, tuple):
            results.append(r)
        else:
            results.append(({"error": f"unexpected: {type(r)}"}, model or "text"))
    return results
