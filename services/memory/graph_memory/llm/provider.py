from __future__ import annotations
import os
import time
import json
from typing import Any, Dict, List, Optional, Tuple
from loguru import logger

def _retry_after_seconds(exc: Exception) -> Optional[float]:
    try:
        ra = None
        if hasattr(exc, 'response') and getattr(exc, 'response') is not None:
            headers = getattr(exc.response, 'headers', {}) or {}
            ra = headers.get('Retry-After') or headers.get('retry-after')
        elif hasattr(exc, 'headers'):
            headers = getattr(exc, 'headers', {}) or {}
            ra = headers.get('Retry-After') or headers.get('retry-after')
        if ra is not None:
            try:
                return float(ra)
            except Exception as exc:
                logger.error("Suppressed error in provider: {}", exc)
                return None
    except Exception as exc:
        logger.error("Suppressed error in provider: {}", exc)
        return None
    return None


def _log(event: str, **fields: Any) -> None:
    if os.getenv('LOG_JSON','1') in ('1','true','TRUE'):
        try:
            print(json.dumps({'event':event, **fields}))
            return
        except Exception as exc:
            logger.error("Suppressed error in provider: {}", exc)
    print(f"{event} {fields}")


def completion(*, model: str, messages: List[Dict[str,str]], api_base: Optional[str] = None, api_key: Optional[str] = None, timeout: float = 60.0, max_tokens: int = 512) -> Tuple[Optional[str], float, str]:
    """Call scillm proxy via httpx. Returns (text, duration_s, provider).

    scillm is an HTTP API at localhost:4001, NOT an importable Python package.
    The proxy handles retries, fallbacks, and model cascading internally.
    """
    import httpx

    api_base = api_base or os.getenv('SCILLM_API_BASE', 'http://localhost:4001')
    api_key  = api_key or os.getenv('SCILLM_PROXY_KEY', 'sk-dev-proxy-123')
    t0 = time.time()

    try:
        resp = httpx.post(
            f"{api_base}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "x-expect-json": "true",  # Enable JSON repair for Claude
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": 0.2,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        txt = data.get("choices", [{}])[0].get("message", {}).get("content")
        return (txt if isinstance(txt, str) else None, time.time()-t0, 'scillm')
    except Exception as e:
        _log('llm.failed', error=str(e), model=model)
        return (None, time.time()-t0, 'none')


def resolve_default_model(kind: str = 'text') -> str:
    """Resolve default model alias for scillm proxy.

    Use scillm aliases (text, vlm) — the proxy handles provider routing.
    kind: 'text'|'vlm'|'vllm'|'default'.
    """
    envs = os.environ
    if kind == 'text':
        return envs.get('SCILLM_SMALL_TEXT_MODEL') or 'text'
    if kind == 'vlm':
        return envs.get('SCILLM_SMALL_VLM_MODEL') or 'vlm'
    if kind == 'vllm':
        return envs.get('SCILLM_LARGE_VLLM_MODEL') or 'text'
    return envs.get('SCILLM_DEFAULT_MODEL') or 'text'

