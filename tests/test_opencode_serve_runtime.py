from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from scillm.proxy.opencode_serve_runtime import (
    OpenCodeServeRuntimeSettings,
    inspect_opencode_serve_runtime,
    restart_opencode_serve_runtime,
)


@pytest.mark.asyncio
async def test_inspect_runtime_disabled() -> None:
    settings = OpenCodeServeRuntimeSettings(
        docker_sock="/var/run/docker.sock",
        compose_project="scillm",
        compose_service="opencode-serve",
        compose_file="/app/deploy/docker/compose.scillm.core.yml",
        container_name=None,
        enabled=False,
    )
    payload = await inspect_opencode_serve_runtime(settings)
    assert payload["docker_control_enabled"] is False
    assert payload["container"] is None


@pytest.mark.asyncio
async def test_restart_running_container() -> None:
    settings = OpenCodeServeRuntimeSettings(
        docker_sock="/var/run/docker.sock",
        compose_project="scillm",
        compose_service="opencode-serve",
        compose_file="/app/deploy/docker/compose.scillm.core.yml",
        container_name="scillm-opencode-serve-1",
        enabled=True,
    )
    container_row = {
        "Id": "abc123",
        "Names": ["/scillm-opencode-serve-1"],
        "State": "running",
        "Status": "Up 1 minute",
        "Labels": {},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1.45/containers/json":
            return httpx.Response(200, json=[container_row])
        if request.method == "POST" and request.url.path.endswith("/restart"):
            return httpx.Response(204)
        return httpx.Response(404, text="unexpected")

    transport = httpx.MockTransport(handler)

    def _client(_settings: OpenCodeServeRuntimeSettings, **_: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, base_url="http://docker")

    with patch("scillm.proxy.opencode_serve_runtime._docker_client", side_effect=_client):
        payload = await restart_opencode_serve_runtime(settings)

    assert payload["actions"][0]["action"] == "restart"
    assert payload["actions"][0]["container"]["name"] == "scillm-opencode-serve-1"
