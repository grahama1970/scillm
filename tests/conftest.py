"""Shared fixtures for scillm e2e tests.

All tests hit the live proxy — zero mocks.
"""
from __future__ import annotations

import os

import httpx
import pytest


PROXY_BASE = os.getenv("PROXY_BASE", "http://127.0.0.1:4001")
MASTER_KEY = os.getenv("MASTER_KEY", os.getenv("LITELLM_MASTER_KEY", "sk-dev-proxy-123"))


@pytest.fixture(scope="session")
def proxy_base() -> str:
    return PROXY_BASE.rstrip("/")


@pytest.fixture(scope="session")
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {MASTER_KEY}"}


@pytest.fixture(scope="session")
def client() -> httpx.Client:
    """Synchronous client with auth pre-configured."""
    with httpx.Client(
        base_url=PROXY_BASE.rstrip("/"),
        headers={"Authorization": f"Bearer {MASTER_KEY}"},
        timeout=30.0,
    ) as c:
        yield c


@pytest.fixture(scope="session")
def proxy_reachable(client: httpx.Client) -> bool:
    """True if the proxy is up. Tests can skip if not."""
    try:
        r = client.get("/health/liveliness")
        return r.status_code == 200
    except httpx.ConnectError:
        return False


@pytest.fixture(scope="session")
def any_model_live(client: httpx.Client) -> str | None:
    """Return a model group name that actually responds, or None."""
    # Try text-gemini first (cheapest), then text, then local-text
    for model in ("text-gemini", "text", "local-text"):
        try:
            r = client.post(
                "/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Reply: OK"}],
                    "max_tokens": 4,
                    "temperature": 0,
                },
                timeout=20.0,
            )
            if r.status_code == 200:
                return model
        except Exception:
            continue
    return None
