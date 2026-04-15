"""Shared helpers for the memory-agent CLI package."""
from __future__ import annotations

import json
import os
from typing import Any

import typer

from ..http_clients import get_session as _get_session

app = typer.Typer(
    add_completion=False,
    help="""Memory Agent - Query memory BEFORE scanning codebase.

SETUP (once per project):
    memory-agent ingest .

AGENT WORKFLOW:
    1. memory-agent recall --q "your problem"
    2. If found=true: apply solution, STOP
    3. If found=false: scan codebase, solve, then:
       memory-agent learn --problem "..." --solution "..."

That's it. Everything else is operator maintenance.
""",
)


_DOCKER_MEMORY_API = "http://127.0.0.1:8601"
_RAW_SERVICE_URL = next(
    (os.getenv(name) for name in ("MEMORY_SERVICE_URL", "MEMORY_API_URL", "MEMORY_SERVER_URL") if os.getenv(name)),
    "",
)
_SERVICE_URL = _RAW_SERVICE_URL or _DOCKER_MEMORY_API
if _SERVICE_URL.startswith(("unix://", "http+unix://")):
    _SERVICE_URL = _DOCKER_MEMORY_API
_SERVICE_TIMEOUT = float(os.getenv("MEMORY_SERVICE_TIMEOUT", "30.0"))


def _service_post(path: str, payload: dict) -> dict:
    """POST to the Docker-backed memory API. Fails loudly if unreachable."""
    url = _SERVICE_URL.rstrip("/")
    endpoint = f"{url}{path}"
    response = _get_session().post(endpoint, json=payload, timeout=_SERVICE_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _service_get(path: str) -> dict:
    """GET from the Docker-backed memory API. Fails loudly if unreachable."""
    url = _SERVICE_URL.rstrip("/")
    endpoint = f"{url}{path}"
    response = _get_session().get(endpoint, timeout=_SERVICE_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _json_output(data: Any) -> None:
    """Print JSON output with standard formatting."""
    print(json.dumps(data, indent=2, default=str))
