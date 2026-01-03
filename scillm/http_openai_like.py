from __future__ import annotations

import os as _os
import json as _json
import subprocess as _sp
import shlex as _shlex
from typing import Dict


def direct_openai_chat(api_base: str, api_key: str | None, *, payload: Dict, timeout: float = 20.0) -> Dict:
    """Direct POST to {api_base}/chat/completions using Bearer.

    Mirrors the working curl the user provided. If SCILLM_USE_CURL_BIN=1, falls back
    to invoking curl and parsing the JSON when httpx raises.
    """
    import httpx as _httpx

    url = f"{api_base.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}" if api_key else "",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "User-Agent": "curl/8.5.0",
    }
    if not headers["Authorization"]:
        headers.pop("Authorization", None)
    body = dict(payload)
    body.setdefault("stream", False)
    try:
        r = _httpx.post(url, json=body, headers=headers, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        # Normalize: when providers place text in reasoning_content, map it to content
        try:
            choice0 = (data.get("choices") or [{}])[0]
            msg = choice0.get("message") or {}
            if msg.get("content") in (None, "") and isinstance(msg.get("reasoning_content"), str) and msg["reasoning_content"].strip():
                msg["content"] = msg["reasoning_content"]
                choice0["message"] = msg
                data["choices"][0] = choice0
        except Exception:
            pass
        return data
    except Exception as e:
        if str(_os.getenv("SCILLM_USE_CURL_BIN", "0")).lower() in {"1", "true", "yes", "on"}:
            data = _json.dumps(body, ensure_ascii=False)
            cmd = f"curl -sS -X POST {_shlex.quote(url)} -H {_shlex.quote('Authorization: Bearer ' + (api_key or ''))} -H Content-Type:application/json --data {_shlex.quote(data)}"
            out = _sp.check_output(cmd, shell=True, text=True, timeout=int(timeout))
            return _json.loads(out)
        raise e
