"""Centralized HTTP session singletons for connection pooling.

Every module that makes HTTP requests MUST use these shared sessions
instead of creating per-call clients.  This prevents TCP connection
exhaustion under concurrent load (the root cause of failures at 5-6
concurrent requests).

Usage:
    from graph_memory.http_clients import get_session, get_httpx_client

    # For requests-based code:
    session = get_session()
    resp = session.post(url, json=payload, timeout=10)

    # For httpx-based code:
    client = get_httpx_client()
    resp = client.post(url, json=payload, timeout=10)
"""
from __future__ import annotations

import threading

import requests
import httpx

_lock = threading.Lock()
_session: requests.Session | None = None
_httpx: httpx.Client | None = None


def get_session() -> requests.Session:
    """Return a shared requests.Session with connection pooling.

    Thread-safe.  The session maintains a urllib3 connection pool that
    reuses TCP connections across calls to the same host.
    """
    global _session
    if _session is not None:
        return _session
    with _lock:
        if _session is not None:
            return _session
        s = requests.Session()
        # Increase pool size for concurrent use from thread-pool executor
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=20,
            pool_maxsize=20,
            max_retries=0,  # callers handle retries
        )
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        _session = s
        return _session


def get_httpx_client() -> httpx.Client:
    """Return a shared httpx.Client with connection pooling.

    Thread-safe.  For sync code that uses httpx instead of requests.
    """
    global _httpx
    if _httpx is not None:
        return _httpx
    with _lock:
        if _httpx is not None:
            return _httpx
        _httpx = httpx.Client(
            timeout=30.0,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
            ),
        )
        return _httpx


_uds_httpx: httpx.Client | None = None


def get_uds_httpx_client(socket_path: str) -> httpx.Client:
    """Unix-socket access is deprecated; callers must use the Docker API."""
    raise RuntimeError(
        "Unix-socket memory access is no longer supported. "
        f"Attempted socket: {socket_path}. Use the Docker memory API on http://127.0.0.1:8601."
    )
