"""
Bootstrap helpers to run a codex-agent sidecar automatically and return its base URL.

Intended to make mcts:auto (variant generation) seamless:
- If CODEX_AGENT_API_BASE is set, verify health and return it.
- Else, start the built-in codex sidecar server in a background process
  using litellm.llms.codex_sidecar_manager and wait for /healthz.
"""

from __future__ import annotations

import os
from typing import Optional
import subprocess
import time

from litellm.llms.codex_sidecar_manager import ensure_sidecar

_SCILLM_DEBUG = str(os.getenv("SCILLM_DEBUG", "")).lower() in {"1", "true", "yes"}


def _dbg(msg: str) -> None:
    if _SCILLM_DEBUG:
        try:
            print(f"[codex_bootstrap][debug] {msg}")
        except Exception:
            pass


def _strip_v1(base: str) -> str:
    base = (base or "").rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def _probe_chat_base(base: str, api_key: str | None = None, timeout: float = 3.0) -> bool:
    """Lightweight health: GET /v1/models or POST /v1/chat/completions with a noop payload.

    Returns True on a plausible 200.
    """
    import json
    import urllib.request as rq
    base = _strip_v1(base)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    # Try /v1/models first
    try:
        with rq.urlopen(rq.Request(url=base + "/v1/models", headers=headers), timeout=timeout) as resp:
            return int(getattr(resp, "status", 0) or 0) == 200
    except Exception:
        pass
    # Minimal chat probe (should be cheap on a local/echo gateway)
    try:
        payload = {"model": "probe", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
        data = json.dumps(payload).encode("utf-8")
        with rq.urlopen(rq.Request(url=base + "/v1/chat/completions", data=data, headers=headers, method="POST"), timeout=timeout) as resp:
            return int(getattr(resp, "status", 0) or 0) == 200
    except Exception:
        return False


def ensure_codex_agent(base: Optional[str] = None) -> str:
    """Ensure a codex-agent HTTP base is available and return it (no trailing slash).

    Order of precedence:
    1) Explicit `base` argument
    2) `CODEX_AGENT_API_BASE` environment variable
    3) Start embedded sidecar via CodexSidecarManager (requires codex CLI installed)
    """
    # Ensure provider module is imported so custom provider registers
    try:
        import litellm.llms.codex_agent as _codex_agent  # noqa: F401
    except Exception:
        pass
    if base:
        base = _strip_v1(base)
        os.environ["CODEX_AGENT_API_BASE"] = base
        _dbg(f"using explicit base={base}")
        return base

    env_base = os.getenv("CODEX_AGENT_API_BASE")
    if env_base:
        env_base = _strip_v1(env_base)
        # If local and unhealthy, try to auto-start the docker sidecar
        is_local = env_base.startswith("http://127.0.0.1:") or env_base.startswith("http://localhost:")
        if is_local and not _probe_chat_base(env_base, os.getenv("OPENAI_API_KEY")):
            try:
                compose = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'local', 'docker', 'compose.agents.yml')
                # 1) Attempt restart if an existing container is present
                try:
                    subprocess.run(['docker','restart','litellm-codex-agent'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    for _ in range(5):
                        if _probe_chat_base(env_base, os.getenv("OPENAI_API_KEY")):
                            return env_base
                        time.sleep(0.6)
                except Exception:
                    pass
                subprocess.run(['docker','compose','-f', compose, 'up','-d','codex-sidecar'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for _ in range(10):
                    if _probe_chat_base(env_base, os.getenv("OPENAI_API_KEY")):
                        break
                    time.sleep(0.6)
            except Exception as e:
                _dbg(f"auto-start codex-sidecar failed: {e}")
        _dbg(f"using env base={env_base}")
        return env_base

    # Start sidecar lazily (embedded) if no base set
    try:
        resolved = ensure_sidecar()
        os.environ["CODEX_AGENT_API_BASE"] = resolved
        _dbg(f"started sidecar at {resolved}")
        # Probe quickly; if broken and fallback allowed, try fallbacks
        ok = _probe_chat_base(resolved, os.getenv("OPENAI_API_KEY"))
        if ok:
            return resolved
        _dbg("sidecar probe failed; attempting fallbacks")
    except Exception as e:
        _dbg(f"sidecar start failed: {e}")

    # Fallbacks (optional, OpenAI-compatible gateways)
    for key in ("SCILLM_AUTOGEN_FALLBACK_BASE", "OPENAI_BASE_URL", "CHUTES_API_BASE", "RUNPOD_API_BASE"):
        fb = os.getenv(key)
        if not fb:
            continue
        fb = _strip_v1(fb)
        if _probe_chat_base(fb, os.getenv("OPENAI_API_KEY")):
            os.environ["CODEX_AGENT_API_BASE"] = fb
            _dbg(f"using fallback {key}={fb}")
            return fb
        else:
            _dbg(f"fallback {key} failed probe: {fb}")

    raise RuntimeError(
        "No working codex-agent or fallback chat endpoint available. "
        "Set CODEX_AGENT_API_BASE to a healthy OpenAI-compatible gateway or install the Codex CLI and set CODEX_CMD."
    )


__all__ = ["ensure_codex_agent"]
