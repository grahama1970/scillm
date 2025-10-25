"""
Auto-start and auto-restart helper for the experimental Mini‑Agent HTTP sidecar.

Usage:
    from litellm.extras.mini_agent_bootstrap import ensure_mini_agent
    base = ensure_mini_agent(os.getenv('MINI_AGENT_API_BASE'))
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Optional


def _ready(base: str, timeout: float = 2.0) -> bool:
    import urllib.request as rq
    try:
        with rq.urlopen(base.rstrip('/') + '/ready', timeout=timeout) as resp:
            return int(getattr(resp, 'status', 0) or 0) == 200
    except Exception:
        return False


def ensure_mini_agent(base: Optional[str] = None) -> str:
    """Ensure a Mini‑Agent HTTP base is available and return it (no trailing slash).

    Order of precedence:
      1) Explicit base argument
      2) MINI_AGENT_API_BASE env or host+port envs
      3) Auto‑start docker mini‑agent (local/docker/compose.agents.yml) under project 'scillm-bridges'
    If base is localhost and unhealthy, try fast docker restart before compose up.
    """
    # Resolve desired base
    if not base:
        host = os.getenv('MINI_AGENT_API_HOST', '127.0.0.1')
        port = os.getenv('MINI_AGENT_API_PORT', '8788')
        base = os.getenv('MINI_AGENT_API_BASE', f'http://{host}:{port}')
    desired = base.rstrip('/')

    # Only manage local endpoints automatically
    is_local = desired.startswith('http://127.0.0.1:') or desired.startswith('http://localhost:')
    if _ready(desired):
        return desired
    if not is_local:
        return desired

    # Attempt fast restart of likely container names
    try:
        for name in ('litellm-mini-agent', 'docker-mini-agent', 'scillm-bridges-mini-agent-1'):
            subprocess.run(['docker','restart', name], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(5):
            if _ready(desired):
                return desired
            time.sleep(0.6)
    except Exception:
        pass

    # Compose up only the mini-agent service
    try:
        here = os.path.dirname(os.path.dirname(__file__))  # litellm/
        compose = os.path.join(here, 'local', 'docker', 'compose.agents.yml')
        env = os.environ.copy()
        env.setdefault('COMPOSE_PROJECT_NAME', 'scillm-bridges')
        subprocess.run(['docker','compose','-f', compose, 'up','-d','mini-agent'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        for _ in range(10):
            if _ready(desired):
                break
            time.sleep(0.6)
    except Exception:
        pass

    # No in-process fallback. If compose failed and service is not ready, return desired
    # so callers can surface a clear error and remediation.
    return desired


__all__ = ['ensure_mini_agent']
