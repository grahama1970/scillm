from __future__ import annotations
import os
from typing import Any, Dict, List, Optional

import scillm

def json_chat(
    *,
    model: str,
    messages: List[Dict[str, Any]],
    api_base: str,
    api_key: Optional[str] = None,
    custom_llm_provider: str = "openai_like",
    timeout: float = 45.0,
    temperature: float = 0.0,
    max_tokens: Optional[int] = None,
    extra_headers: Optional[Dict[str,str]] = None,
    sanitize: Optional[bool] = None,
    require_nonempty: Optional[bool] = None,
    require_nonempty_keys: Optional[list[str]] = None,
) -> Any:
    """Strict JSON completion helper with hard timeout and optional sanitization.

    - Forces response_format={"type":"json_object"}
    - Applies per-request timeout
    - Honors SCILLM_JSON_SANITIZE env by default
    """
    if sanitize is None:
        sanitize = str(os.getenv("SCILLM_JSON_SANITIZE", "0")).lower() in {"1","true","yes","on"}
    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "api_base": api_base,
        "api_key": api_key,
        "custom_llm_provider": custom_llm_provider,
        "response_format": {"type": "json_object"},
        "temperature": temperature,
        "timeout": timeout,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if extra_headers:
        kwargs["extra_headers"] = extra_headers
    if sanitize:
        kwargs["auto_json_sanitize"] = True
    resp = scillm.completion(**kwargs)
    # Optional post-processing: map empty strings to null for specified keys
    try:
        if require_nonempty or require_nonempty_keys:
            content = getattr(resp.choices[0].message, "content", None)
            if isinstance(content, str) and content.strip():
                import json as _json
                try:
                    obj = _json.loads(content)
                    if isinstance(obj, dict):
                        keys = None
                        if require_nonempty_keys:
                            keys = set(require_nonempty_keys)
                        elif require_nonempty:
                            # apply to all top-level string keys
                            keys = set(k for k, v in obj.items() if isinstance(v, str))
                        if keys:
                            changed = False
                            for k in keys:
                                if k in obj and isinstance(obj.get(k), str) and obj.get(k) == "":
                                    obj[k] = None
                                    changed = True
                            if changed:
                                resp.choices[0].message["content"] = _json.dumps(obj, ensure_ascii=False)
                except Exception:
                    # If not parseable JSON, leave as-is
                    pass
    except Exception:
        # Never let helper post-processing raise
        pass
    return resp

# Aliases for clarity; keep API flexible without churn
def strict_json_chat(**kwargs):
    return json_chat(**kwargs)

def strict_json_completion(**kwargs):
    """Preferred name for strict JSON single-call completion."""
    return json_chat(**kwargs)
