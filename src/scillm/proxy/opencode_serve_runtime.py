"""Docker lifecycle control for the ``opencode-serve`` compose sidecar.

Uses the Docker Engine HTTP API over ``/var/run/docker.sock`` so scillm-proxy does
not need the compose plugin at runtime for restart/start. Optional ``docker
compose up`` is used only when the service container is missing.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from scillm.proxy.errors import ProxyError


@dataclass(frozen=True)
class OpenCodeServeRuntimeSettings:
    docker_sock: str
    compose_project: str
    compose_service: str
    compose_file: str
    container_name: str | None
    enabled: bool


def load_opencode_serve_runtime_settings() -> OpenCodeServeRuntimeSettings:
    sock = os.environ.get("SCILLM_OPENCODE_SERVE_DOCKER_SOCK", "/var/run/docker.sock").strip()
    project = os.environ.get("SCILLM_OPENCODE_SERVE_COMPOSE_PROJECT", "scillm").strip() or "scillm"
    service = os.environ.get("SCILLM_OPENCODE_SERVE_COMPOSE_SERVICE", "opencode-serve").strip() or "opencode-serve"
    compose_file = os.environ.get(
        "SCILLM_OPENCODE_SERVE_COMPOSE_FILE",
        "/app/deploy/docker/compose.scillm.core.yml",
    ).strip()
    container_name = os.environ.get("SCILLM_OPENCODE_SERVE_CONTAINER_NAME", "").strip() or None
    enabled = os.environ.get("SCILLM_OPENCODE_SERVE_DOCKER_CONTROL", "true").lower() not in {
        "0",
        "false",
        "no",
    }
    return OpenCodeServeRuntimeSettings(
        docker_sock=sock,
        compose_project=project,
        compose_service=service,
        compose_file=compose_file,
        container_name=container_name,
        enabled=enabled and bool(sock) and Path(sock).exists(),
    )


def _docker_client(
    settings: OpenCodeServeRuntimeSettings,
    *,
    timeout_s: float = 30.0,
) -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(uds=settings.docker_sock)
    return httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=timeout_s)


def _label_filters(settings: OpenCodeServeRuntimeSettings) -> str:
    payload = {
        "label": [
            f"com.docker.compose.project={settings.compose_project}",
            f"com.docker.compose.service={settings.compose_service}",
        ]
    }
    return json.dumps(payload)


async def _list_matching_containers(
    client: httpx.AsyncClient,
    settings: OpenCodeServeRuntimeSettings,
) -> list[dict[str, Any]]:
    if settings.container_name:
        resp = await client.get(
            "/v1.45/containers/json",
            params={"all": "true", "filters": json.dumps({"name": [settings.container_name]})},
        )
    else:
        resp = await client.get(
            "/v1.45/containers/json",
            params={"all": "true", "filters": _label_filters(settings)},
        )
    if resp.status_code >= 400:
        raise ProxyError(
            502,
            f"docker list containers failed: HTTP {resp.status_code}: {resp.text[:500]}",
            "provider_error",
        )
    rows = resp.json()
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _pick_container(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    running = [row for row in rows if str(row.get("State", "")).lower() == "running"]
    return running[0] if running else rows[0]


def _container_summary(row: dict[str, Any]) -> dict[str, Any]:
    names = row.get("Names") or []
    name = names[0].lstrip("/") if names else str(row.get("Id", ""))[:12]
    return {
        "id": row.get("Id"),
        "name": name,
        "state": row.get("State"),
        "status": row.get("Status"),
        "labels": row.get("Labels") or {},
    }


async def inspect_opencode_serve_runtime(
    settings: OpenCodeServeRuntimeSettings | None = None,
) -> dict[str, Any]:
    settings = settings or load_opencode_serve_runtime_settings()
    result: dict[str, Any] = {
        "schema": "scillm.opencode_serve_runtime.v1",
        "docker_control_enabled": settings.enabled,
        "docker_sock": settings.docker_sock,
        "compose_project": settings.compose_project,
        "compose_service": settings.compose_service,
        "compose_file": settings.compose_file,
        "container_name_override": settings.container_name,
        "container": None,
        "project_agent_message": None,
    }
    if not settings.enabled:
        result["project_agent_message"] = (
            "Docker control disabled or socket missing. Mount /var/run/docker.sock into scillm-proxy "
            "and set SCILLM_OPENCODE_SERVE_DOCKER_CONTROL=true."
        )
        return result

    async with _docker_client(settings) as client:
        rows = await _list_matching_containers(client, settings)
    picked = _pick_container(rows)
    if picked:
        result["container"] = _container_summary(picked)
    else:
        result["project_agent_message"] = (
            f"No container found for compose project={settings.compose_project} "
            f"service={settings.compose_service}. Run docker compose up -d opencode-serve."
        )
    return result


async def _post_container_action(
    client: httpx.AsyncClient,
    container_id: str,
    action: str,
    *,
    docker_timeout_s: int = 120,
) -> None:
    resp = await client.post(f"/v1.45/containers/{container_id}/{action}", params={"t": docker_timeout_s})
    if resp.status_code in {204, 304}:
        return
    if resp.status_code >= 400:
        raise ProxyError(
            502,
            f"docker {action} failed: HTTP {resp.status_code}: {resp.text[:500]}",
            "provider_error",
        )


async def _compose_up(settings: OpenCodeServeRuntimeSettings) -> dict[str, Any]:
    docker_bin = shutil.which("docker")
    if not docker_bin:
        raise ProxyError(
            503,
            "docker CLI not available in scillm-proxy; run compose up on the host",
            "service_unavailable",
            advice=(
                f"docker compose -p {settings.compose_project} -f {settings.compose_file} "
                f"up -d {settings.compose_service}"
            ),
        )
    if not Path(settings.compose_file).exists():
        raise ProxyError(
            503,
            f"compose file not visible in scillm-proxy: {settings.compose_file}",
            "service_unavailable",
        )

    cmd = [
        docker_bin,
        "compose",
        "-p",
        settings.compose_project,
        "-f",
        settings.compose_file,
        "up",
        "-d",
        settings.compose_service,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    if proc.returncode != 0:
        detail = (stderr_b or stdout_b).decode(errors="replace")[:2000]
        raise ProxyError(
            502,
            f"docker compose up failed (exit {proc.returncode}): {detail}",
            "provider_error",
        )
    return {
        "action": "compose_up",
        "command": cmd,
        "stdout": stdout_b.decode(errors="replace")[:2000],
    }


async def restart_opencode_serve_runtime(
    settings: OpenCodeServeRuntimeSettings | None = None,
    *,
    ensure_up: bool = False,
) -> dict[str, Any]:
    settings = settings or load_opencode_serve_runtime_settings()
    if not settings.enabled:
        raise ProxyError(
            503,
            "OpenCode serve docker control is disabled",
            "service_unavailable",
            advice="Mount /var/run/docker.sock and set SCILLM_OPENCODE_SERVE_DOCKER_CONTROL=true.",
        )

    actions: list[dict[str, Any]] = []
    async with _docker_client(settings, timeout_s=120.0) as client:
        rows = await _list_matching_containers(client, settings)
        picked = _pick_container(rows)
        if not picked and ensure_up:
            actions.append(await _compose_up(settings))
            rows = await _list_matching_containers(client, settings)
            picked = _pick_container(rows)

        if not picked:
            raise ProxyError(
                404,
                "opencode-serve container not found",
                "not_found",
                advice=(
                    f"docker compose -p {settings.compose_project} -f {settings.compose_file} "
                    f"up -d {settings.compose_service}"
                ),
            )

        container_id = str(picked["Id"])
        state = str(picked.get("State", "")).lower()
        summary = _container_summary(picked)

        if state == "running":
            await _post_container_action(client, container_id, "restart")
            actions.append({"action": "restart", "container": summary})
        elif state in {"exited", "created", "paused"}:
            await _post_container_action(client, container_id, "start")
            actions.append({"action": "start", "container": summary})
        else:
            await _post_container_action(client, container_id, "restart")
            actions.append({"action": "restart", "container": summary, "prior_state": state})

    await asyncio.sleep(2.0)
    after = await inspect_opencode_serve_runtime(settings)
    return {
        "schema": "scillm.opencode_serve_runtime_action.v1",
        "actions": actions,
        "runtime": after,
    }
