"""Utilities to ensure the Codex sidecar is running locally."""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
from dataclasses import dataclass

import httpx


class SidecarError(RuntimeError):
    pass


@dataclass
class _State:
    process: multiprocessing.Process | None = None
    base_url: str | None = None


class CodexSidecarManager:
    """Lazily starts and monitors a local codex sidecar."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = _State()

    def ensure(self) -> str:
        """Return a base URL for the sidecar, starting it if necessary."""

        if "CODEX_AGENT_API_BASE" in os.environ:
            return os.environ["CODEX_AGENT_API_BASE"].rstrip("/")

        with self._lock:
            if self._state.process and self._state.process.is_alive() and self._state.base_url:
                return self._state.base_url

            host = os.getenv("CODEX_SIDECAR_HOST", "127.0.0.1")
            port = int(os.getenv("CODEX_SIDECAR_PORT", "8077"))

            # Lazy import to avoid module import side-effects if CLI is not present
            from .codex_sidecar_server import serve  # type: ignore
            proc = multiprocessing.Process(target=serve, args=(host, port), daemon=True)
            proc.start()

            base_url = f"http://{host}:{port}"
            if not self._wait_for_health(base_url):
                proc.terminate()
                raise SidecarError("Codex sidecar failed to start (health check timeout)")

            self._state = _State(process=proc, base_url=base_url.rstrip("/"))
            return self._state.base_url

    @staticmethod
    def _wait_for_health(base_url: str, timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        with httpx.Client(timeout=1.0) as client:
            while time.time() < deadline:
                try:
                    resp = client.get(f"{base_url}/healthz")
                    if resp.status_code == 200:
                        return True
                except Exception:
                    pass
                time.sleep(0.3)
        return False


_MANAGER = CodexSidecarManager()


def ensure_sidecar() -> str:
    """Public helper to get the sidecar base URL."""

    return _MANAGER.ensure()


def restart_sidecar(timeout: float = 20.0) -> str:
    """Force a fresh embedded sidecar instance and return its base URL.

    Local-only path used for ephemeral judge calls (no CODEX_AGENT_API_BASE set).
    """
    # If an explicit base is set, we must not mutate it here
    if "CODEX_AGENT_API_BASE" in os.environ:
        return os.environ["CODEX_AGENT_API_BASE"].rstrip("/")
    try:
        # Stop existing
        if _MANAGER._state.process and _MANAGER._state.process.is_alive():  # type: ignore[attr-defined]
            _MANAGER._state.process.terminate()  # type: ignore[attr-defined]
            _MANAGER._state.process.join(timeout=5.0)  # type: ignore[attr-defined]
    except Exception:
        pass
    # Start fresh
    base = _MANAGER.ensure()
    # Optionally wait a bit longer for health on cold start
    end = time.time() + max(0.0, float(timeout))
    with httpx.Client(timeout=1.0) as c:
        while time.time() < end:
            try:
                r = c.get(f"{base}/healthz")
                if r.status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.25)
    return base
