from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional
import uuid
from urllib import request as rq


def chat(
    messages: List[Dict[str, Any]],
    *,
    model: str = "gpt-5",
    base: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    reasoning_effort: Optional[str] = None,
    response_format: Optional[Dict[str, Any]] = None,
    timeout: float = 60.0,
) -> Dict[str, Any]:
    """Direct call to codex-agent sidecar (no Router/CodeWorld).

    Returns the raw OpenAI-compatible JSON dict.
    """
    b = (base or os.getenv("CODEX_AGENT_API_BASE") or "http://127.0.0.1:8089").rstrip("/")
    # Context rot guard: keep system + last few turns (default 8), no caller changes
    raw_msgs = list(messages or [])
    try:
        max_hist = int(os.getenv("SCILLM_CODEX_MAX_HISTORY", "8"))
        if max_hist > 0 and len(raw_msgs) > max_hist:
            system_msgs = [m for m in raw_msgs if isinstance(m, dict) and m.get("role") == "system"]
            non_system = [m for m in raw_msgs if not (isinstance(m, dict) and m.get("role") == "system")]
            raw_msgs = system_msgs + non_system[-max_hist:]
    except Exception:
        pass

    body: Dict[str, Any] = {"model": model, "messages": raw_msgs}
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if reasoning_effort is not None:
        body["reasoning_effort"] = reasoning_effort
        body.setdefault("reasoning", {"effort": reasoning_effort})
    if response_format is not None:
        body["response_format"] = response_format
    # Reserve output space when not specified (sane default, still override-able)
    if "max_tokens" not in body and os.getenv("SCILLM_CODEX_MAX_TOKENS"):
        try:
            body["max_tokens"] = int(os.getenv("SCILLM_CODEX_MAX_TOKENS", "256"))
        except Exception:
            pass

    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Codex-Session": uuid.uuid4().hex}
    req = rq.Request(url=b + "/v1/chat/completions", data=data, headers=headers, method="POST")
    with rq.urlopen(req, timeout=timeout) as resp:
        if int(getattr(resp, "status", 0) or 0) != 200:
            raise RuntimeError(f"codex-agent HTTP {getattr(resp,'status',0)}")
        return json.loads(resp.read().decode("utf-8", "ignore"))


__all__ = ["chat"]
