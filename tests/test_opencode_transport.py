from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scillm.proxy.errors import proxy_error_handler, ProxyError
from scillm.proxy.opencode_serve_api import create_opencode_serve_router
from scillm.proxy.opencode_transport import (
    OpenCodeTransport,
    TransportStore,
    build_capability_flags,
    git_diff_empty,
    prompt_async_allowed,
)


def test_build_capability_flags_bans_prompt_async_by_default() -> None:
    caps = build_capability_flags(health={"health": {"version": "1.15.13"}}, opencode_url="http://127.0.0.1:4098")
    assert caps["sync_message"] is True
    assert caps["prompt_async_core"] is prompt_async_allowed()


def test_transport_store_roundtrip(tmp_path: Path) -> None:
    from scillm.proxy.opencode_transport import TransportState

    store = TransportStore(tmp_path)
    state = TransportState(transport_run_id="otr-test", dag_node_id="node-1", workspace=str(tmp_path))
    store.save(state)
    loaded = store.load("otr-test")
    assert loaded.dag_node_id == "node-1"


def test_git_diff_empty_ignores_dirty_parent_outside_workspace(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace = repo / "nested" / "workspace"
    workspace.mkdir(parents=True)
    (repo / "outside.txt").write_text("before\n", encoding="utf-8")
    (workspace / "inside.txt").write_text("inside\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)

    (repo / "outside.txt").write_text("after\n", encoding="utf-8")

    assert git_diff_empty(workspace) is True


def test_git_diff_empty_detects_workspace_changes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace = repo / "nested" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "inside.txt").write_text("inside\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)

    (workspace / "inside.txt").write_text("changed\n", encoding="utf-8")

    assert git_diff_empty(workspace) is False


@pytest.mark.asyncio
async def test_parent_dialog_mirror_on_create_and_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scillm.proxy.opencode_transport import OpenCodeTransport, TransportState, parent_dialog_enabled

    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    assert parent_dialog_enabled()

    transport = OpenCodeTransport()
    state = TransportState(
        transport_run_id="otr-dialog",
        dag_node_id="n1",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
        opencode_url="http://127.0.0.1:4098",
    )
    child_row = {
        "subagent_run_id": "otr-dialog-reviewer-1",
        "role": "reviewer",
        "child_session_id": "ses-child",
        "agent": "scillm-worker",
        "attempt_id": 1,
        "delivery_state": "created",
        "active": True,
        "mode": "advisory",
    }

    client = AsyncMock()
    client.send_message = AsyncMock(return_value={})

    await transport.mirror_run_started(client, state)
    from scillm.proxy.opencode_transport import ChildAttempt

    child = ChildAttempt(**child_row)
    await transport.mirror_child_created(client, state, child)

    assert client.send_message.await_count == 2
    first = client.send_message.await_args_list[0]
    assert first.kwargs.get("no_reply") is True
    assert first.args[0] == "ses-parent"
    assert "collaboration transcript" in first.kwargs["parts"][0]["text"].lower()


def test_build_transport_observation_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scillm.proxy.opencode_serve import OpenCodeServeSettings
    from scillm.proxy.opencode_transport import TransportState, build_transport_observation

    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    state = TransportState(
        transport_run_id="otr-obs",
        dag_node_id="n1",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
        opencode_url="http://127.0.0.1:4098",
        children=[
            {
                "subagent_run_id": "otr-obs-patch-1",
                "role": "patch",
                "child_session_id": "ses-child",
                "agent": "explore",
                "attempt_id": 1,
                "delivery_state": "created",
                "active": True,
                "mode": "propose_patches",
            }
        ],
        active_subagent_run_id="otr-obs-patch-1",
    )
    settings = OpenCodeServeSettings(
        base_url="http://127.0.0.1:4098",
        username="opencode",
        password="secret",
        timeout_s=60.0,
    )
    obs = build_transport_observation(transport_run_id="otr-obs", state=state, settings=settings)
    assert obs["schema"] == "scillm.opencode_transport.observation.v1"
    assert obs["parent_session_id"] == "ses-parent"
    assert obs["child_session_ids"] == ["ses-child"]
    assert obs["active_child_session_id"] == "ses-child"
    assert obs["auth_required"] is True
    assert obs["auth_username"] == "opencode"
    assert "password" not in obs
    assert obs["opencode_children_api"].endswith("/session/ses-parent/children")
    assert obs["scillm_events_stream"].endswith("/otr-obs/events/stream")


def test_transport_run_index_projects_active_child_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scillm.proxy.opencode_transport import ChildAttempt, TransportState, TransportStore, list_transport_run_index

    transport_dir = tmp_path / "transport"
    monkeypatch.setenv("SCILLM_OPENCODE_TRANSPORT_DIR", str(transport_dir))
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path / "serve"))
    child = ChildAttempt(
        subagent_run_id="oc-pdf-lab-p45-d4f3-default-e2e",
        role="patch",
        child_session_id="ses_16c3f0096ffe9dOmNNpicQ5kTM",
        agent="build",
        attempt_id=1,
        delivery_state="posted",
        active=True,
    )
    state = TransportState(
        transport_run_id="otr-wrapper-completed",
        dag_node_id="pdf_lab",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
        active_subagent_run_id=child.subagent_run_id,
        children=[child.to_dict()],
    )
    store = TransportStore(transport_dir)
    store.save(state)
    state_path = store.state_path(state.transport_run_id)
    data = json.loads(state_path.read_text(encoding="utf-8"))
    data["state"] = "completed"
    data["phase"] = "done"
    state_path.write_text(json.dumps(data), encoding="utf-8")

    row = next(r for r in list_transport_run_index() if r["run_id"] == "otr-wrapper-completed")
    assert row["state"] == "running"
    assert row["phase"] == "active_child:posted"
    assert row["wrapper_state"] == "completed"
    assert row["active_child_run_id"] == "oc-pdf-lab-p45-d4f3-default-e2e"
    assert row["active_child_session_id"] == "ses_16c3f0096ffe9dOmNNpicQ5kTM"




def test_transport_capabilities_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    class _Settings:
        base_url = "http://127.0.0.1:4098"

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.settings = _Settings()
    mock_client.health.return_value = {"health": {"version": "1.15.13"}}

    with patch("scillm.proxy.opencode_transport_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        resp = client.get(
            "/v1/scillm/opencode/transport/capabilities",
            headers={"X-Caller-Skill": "test-transport"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["transport_api"] is True
    assert body["opencode_version"] == "1.15.13"


def test_loop2_capabilities_endpoint_projects_required_transport_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    class _Settings:
        base_url = "http://127.0.0.1:4098"

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.settings = _Settings()
    mock_client.health.return_value = {"health": {"version": "1.15.13"}}

    with patch("scillm.proxy.opencode_transport_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        resp = client.get(
            "/v1/scillm/loop2/capabilities",
            headers={"X-Caller-Skill": "loop2"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schema"] == "scillm.loop2.capabilities.v1"
    assert body["loop2_api"] is True
    assert body["caller_skill_header_required"] is True
    assert "POST /v1/scillm/opencode/transport/runs" in body["required_endpoints"]


def test_transport_create_and_message_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    parent = {"id": "ses-parent"}
    child = {"id": "ses-child"}
    message = {
        "info": {"role": "assistant", "id": "msg-1"},
        "parts": [{"type": "text", "text": "```diff\n+patch\n```"}],
    }

    class _Settings:
        base_url = "http://127.0.0.1:4098"

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.settings = _Settings()
    mock_client.create_session.side_effect = [parent, child]
    mock_client.send_message.return_value = message
    mock_client.list_messages.return_value = [message]
    mock_client.session_status_map.return_value = {"ses-child": {"status": "idle"}}
    mock_client.diff.return_value = []
    mock_client.abort.return_value = True

    with patch("scillm.proxy.opencode_transport_api.OpenCodeServeClient", return_value=mock_client), patch(
        "scillm.proxy.opencode_transport.git_diff_empty", return_value=True
    ):
        client = TestClient(app)
        headers = {"X-Caller-Skill": "test-transport"}
        created = client.post(
            "/v1/scillm/opencode/transport/runs",
            headers=headers,
            json={"dag_node_id": "node-1", "workspace": str(tmp_path)},
        )
        assert created.status_code == 200, created.text
        run_id = created.json()["transport_run_id"]
        obs = created.json()["observation"]
        assert obs["schema"] == "scillm.opencode_transport.observation.v1"
        assert obs["parent_session_id"] == "ses-parent"
        assert obs["scillm_events_stream"].endswith(f"/{run_id}/events/stream")


        child_resp = client.post(
            f"/v1/scillm/opencode/transport/runs/{run_id}/children",
            headers=headers,
            json={"role": "patch", "agent": "explore", "mode": "propose_patches"},
        )
        assert child_resp.status_code == 200, child_resp.text
        child_obs = child_resp.json()["observation"]
        assert child_obs["child_session_ids"] == ["ses-child"]


        msg_resp = client.post(
            f"/v1/scillm/opencode/transport/runs/{run_id}/message",
            headers=headers,
            json={"prompt": "propose patch only", "wait_idle": True, "stream": False},
        )
        assert msg_resp.status_code == 200, msg_resp.text
        assert "patch" in msg_resp.json().get("assistant_text", "")

        state_resp = client.get(
            f"/v1/scillm/opencode/transport/runs/{run_id}",
            headers=headers,
        )
        assert state_resp.status_code == 200
        assert state_resp.json()["state"]["parent_session_id"] == "ses-parent"


def _opencode_sse_bytes(
    opencode_type: str,
    *,
    session_id: str = "ses-child",
    part_type: str = "reasoning",
) -> bytes:
    import json as _json

    payload: dict = {"type": opencode_type, "properties": {}}
    if opencode_type == "message.part.updated":
        payload["properties"] = {
            "part": {
                "id": "p1",
                "type": part_type,
                "text": "trace",
                "sessionID": session_id,
                "messageID": "msg-1",
            },
            "delta": " live",
        }
    elif opencode_type == "permission.asked":
        payload["properties"] = {"permission": "write"}
        payload["sessionID"] = session_id
    elif opencode_type == "session.idle":
        payload["sessionID"] = session_id
    return f"data: {_json.dumps(payload)}\n\n".encode()


def test_transport_message_agent_id_auto_child_uses_worker_catalog_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    agents_root = Path.home() / "workspace" / "experiments" / "agent-skills" / "agents"
    monkeypatch.setenv("SCILLM_WORKER_AGENTS_ROOT", str(agents_root))
    skill_root = tmp_path / "skills"
    for name in ("memory", "scillm", "best-practices-python", "best-practices-scillm", "code-runner"):
        d = skill_root / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    monkeypatch.setenv("SCILLM_OPENCODE_SKILL_ROOTS", str(skill_root))
    from scillm.proxy.worker_agents import reload_worker_index

    reload_worker_index()
    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    parent = {"id": "ses-parent"}
    child = {"id": "ses-child"}
    message = {
        "info": {"role": "assistant", "id": "msg-1"},
        "parts": [{"type": "text", "text": "patched"}],
    }

    class _Settings:
        base_url = "http://127.0.0.1:4098"

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.settings = _Settings()
    mock_client.create_session.side_effect = [parent, child]
    mock_client.send_message.return_value = message
    mock_client.list_messages.return_value = [message]
    mock_client.session_status_map.return_value = {"ses-child": {"status": "idle"}}
    mock_client.diff.return_value = [{"path": "evidence/canary.json", "status": "added"}]
    mock_client.abort.return_value = True

    with patch("scillm.proxy.opencode_transport_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        headers = {"X-Caller-Skill": "test-transport"}
        created = client.post(
            "/v1/scillm/opencode/transport/runs",
            headers=headers,
            json={"dag_node_id": "node-1", "workspace": str(tmp_path)},
        )
        assert created.status_code == 200, created.text
        run_id = created.json()["transport_run_id"]
        msg_resp = client.post(
            f"/v1/scillm/opencode/transport/runs/{run_id}/message",
            headers=headers,
            json={"prompt": "write evidence", "agent_id": "patch-worker", "stream": False},
        )
        assert msg_resp.status_code == 200, msg_resp.text
        state_resp = client.get(
            f"/v1/scillm/opencode/transport/runs/{run_id}",
            headers=headers,
        )
        child_state = state_resp.json()["state"]["children"][0]
        assert child_state["agent_id"] == "patch-worker"
        assert child_state["agent"] == "build"
        assert child_state["mode"] == "workspace_write"


async def _iter_opencode_sse(*chunks: bytes):
    for chunk in chunks:
        yield chunk


def test_transport_message_stream_emits_reasoning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    parent = {"id": "ses-parent"}
    child = {"id": "ses-child"}
    message = {
        "info": {"role": "assistant", "id": "msg-1"},
        "parts": [{"type": "text", "text": "done"}],
    }

    class _Settings:
        base_url = "http://127.0.0.1:4098"

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.settings = _Settings()
    mock_client.create_session.side_effect = [parent, child]
    mock_client.send_message.return_value = message
    mock_client.list_messages.return_value = [message]
    mock_client.session_status_map.return_value = {"ses-child": {"status": "idle"}}
    mock_client.diff.return_value = []
    mock_client.abort.return_value = True

    mock_client.iter_event_stream = lambda: _iter_opencode_sse(
            _opencode_sse_bytes("message.part.updated", session_id="ses-child")
        )

    with patch("scillm.proxy.opencode_transport_api.OpenCodeServeClient", return_value=mock_client), patch(
        "scillm.proxy.opencode_transport.git_diff_empty", return_value=True
    ), patch(
        "scillm.proxy.opencode_transport_stream.git_diff_empty", return_value=True
    ):
        client = TestClient(app)
        headers = {"X-Caller-Skill": "test-transport"}
        created = client.post(
            "/v1/scillm/opencode/transport/runs",
            headers=headers,
            json={"dag_node_id": "node-1", "workspace": str(tmp_path)},
        )
        run_id = created.json()["transport_run_id"]
        client.post(
            f"/v1/scillm/opencode/transport/runs/{run_id}/children",
            headers=headers,
            json={"role": "patch", "agent": "explore", "mode": "propose_patches"},
        )
        with client.stream(
            "POST",
            f"/v1/scillm/opencode/transport/runs/{run_id}/message",
            headers=headers,
            json={"prompt": "stream me", "stream": True, "timeout_s": 30.0},
        ) as resp:
            assert resp.status_code == 200
            body = "".join(resp.iter_text())
        assert "reasoning_delta" in body
        assert "message.completed" in body


def test_transport_message_defaults_to_sse_when_stream_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TV-001: omitting stream must hit the default SSE branch (stream=True on model)."""
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    parent = {"id": "ses-parent"}
    child = {"id": "ses-child"}
    message = {
        "info": {"role": "assistant", "id": "msg-1"},
        "parts": [{"type": "text", "text": "done"}],
    }

    class _Settings:
        base_url = "http://127.0.0.1:4098"

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.settings = _Settings()
    mock_client.create_session.side_effect = [parent, child]
    mock_client.send_message.return_value = message
    mock_client.list_messages.return_value = [message]
    mock_client.session_status_map.return_value = {"ses-child": {"status": "idle"}}
    mock_client.diff.return_value = []
    mock_client.abort.return_value = True

    mock_client.iter_event_stream = lambda: _iter_opencode_sse(
            _opencode_sse_bytes("message.part.updated", session_id="ses-child")
    )

    with patch(
        "scillm.proxy.opencode_transport_api.OpenCodeServeClient", return_value=mock_client
    ), patch("scillm.proxy.opencode_transport.git_diff_empty", return_value=True), patch(
        "scillm.proxy.opencode_transport_stream.git_diff_empty", return_value=True
    ):
        client = TestClient(app)
        headers = {"X-Caller-Skill": "test-transport"}
        created = client.post(
            "/v1/scillm/opencode/transport/runs",
            headers=headers,
            json={"dag_node_id": "node-1", "workspace": str(tmp_path)},
        )
        run_id = created.json()["transport_run_id"]
        client.post(
            f"/v1/scillm/opencode/transport/runs/{run_id}/children",
            headers=headers,
            json={"role": "patch", "agent": "explore", "mode": "propose_patches"},
        )
        with client.stream(
            "POST",
            f"/v1/scillm/opencode/transport/runs/{run_id}/message",
            headers=headers,
            json={"prompt": "default stream", "timeout_s": 30.0},
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in (resp.headers.get("content-type") or "")
            body = "".join(resp.iter_text())
        assert "message.completed" in body


def test_parse_sse_json_payload_rejects_invalid_and_done() -> None:
    from scillm.proxy.opencode_transport_events import parse_sse_json_payload

    assert parse_sse_json_payload("") is None
    assert parse_sse_json_payload("[DONE]") is None
    assert parse_sse_json_payload("not-json") is None


def test_normalize_opencode_bus_event_watch_session_and_branches() -> None:
    from scillm.proxy.opencode_transport_events import normalize_opencode_bus_event

    other = {
        "type": "message.part.updated",
        "properties": {
            "part": {"sessionID": "ses-other", "type": "text", "text": "ignored"},
        },
    }
    assert normalize_opencode_bus_event(other, watch_session_ids=frozenset({"ses-a"})) is None

    perm = {
        "type": "permission.asked",
        "properties": {"permission": "write"},
        "sessionID": "ses-a",
    }
    out = normalize_opencode_bus_event(perm, watch_session_ids=frozenset({"ses-a"}))
    assert out is not None
    assert out["event_type"] == "permission_requested"
    assert out.get("needs_attention") is True

    err = {"type": "session.error", "properties": {"error": {"message": "boom"}}}
    out_err = normalize_opencode_bus_event(err, watch_session_ids=frozenset())
    assert out_err is not None
    assert out_err["event_type"] == "session_error"

    idle = {"type": "session.idle", "properties": {}}
    out_idle = normalize_opencode_bus_event(idle, watch_session_ids=frozenset())
    assert out_idle is not None
    assert out_idle["event_type"] == "session_idle"

    synthetic = {
        "type": "message.part.updated",
        "properties": {
            "part": {
                "sessionID": "ses-a",
                "type": "text",
                "text": "x",
                "synthetic": True,
            },
        },
    }
    assert normalize_opencode_bus_event(synthetic, watch_session_ids=frozenset({"ses-a"})) is None

    no_sid = {"type": "session.idle", "properties": {}}
    assert normalize_opencode_bus_event(no_sid, watch_session_ids=frozenset({"ses-a"})) is None

def test_transport_events_stream_emits_normalized_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TV-SM002-001: GET events/stream yields normalized transport SSE, filters by watch set."""
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    class _Settings:
        base_url = "http://127.0.0.1:4098"

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.settings = _Settings()
    mock_client.create_session.side_effect = [{"id": "ses-parent"}, {"id": "ses-child"}]

    watched_reasoning = _opencode_sse_bytes("message.part.updated", session_id="ses-child")
    other_idle = _opencode_sse_bytes("session.idle", session_id="ses-other")
    mock_client.iter_event_stream = lambda: _iter_opencode_sse(watched_reasoning, other_idle)

    with patch("scillm.proxy.opencode_transport_api.OpenCodeServeClient", return_value=mock_client), patch(
        "scillm.proxy.opencode_transport.OpenCodeTransport.tail_transport_events",
        return_value=([], 0),
    ):
        client = TestClient(app)
        headers = {"X-Caller-Skill": "test-transport"}
        created = client.post(
            "/v1/scillm/opencode/transport/runs",
            headers=headers,
            json={"dag_node_id": "node-1", "workspace": str(tmp_path)},
        )
        run_id = created.json()["transport_run_id"]
        client.post(
            f"/v1/scillm/opencode/transport/runs/{run_id}/children",
            headers=headers,
            json={"role": "patch", "agent": "explore", "mode": "propose_patches"},
        )
        with client.stream(
            "GET",
            f"/v1/scillm/opencode/transport/runs/{run_id}/events/stream",
            headers=headers,
            params={"timeout_s": 30.0, "heartbeat_s": 5.0},
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in (resp.headers.get("content-type") or "")
            body = "".join(resp.iter_text())
    assert "reasoning_delta" in body
    assert "scillm.opencode_transport.event.v1" in body
    assert "session_idle" not in body
    assert "ses-other" not in body

def test_transport_events_stream_tails_persisted_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TV-001: /events/stream emits tail rows and passes after_line to tail_transport_events."""
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    class _Settings:
        base_url = "http://127.0.0.1:4098"

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.settings = _Settings()
    mock_client.create_session.return_value = {"id": "ses-parent"}

    async def _empty_stream():
        if False:
            yield b""

    mock_client.iter_event_stream = lambda: _empty_stream()

    historical = {
        "schema": "scillm.opencode_transport.event.v1",
        "event_type": "child.created",
        "transport_run_id": "placeholder",
    }
    tail_calls: list[int] = []

    def _fake_tail(_run_id: str, *, after_line: int = 0):
        tail_calls.append(after_line)
        return ([historical], after_line)

    with patch("scillm.proxy.opencode_transport_api.OpenCodeServeClient", return_value=mock_client), patch.object(
        OpenCodeTransport,
        "tail_transport_events",
        side_effect=_fake_tail,
    ):
        client = TestClient(app)
        headers = {"X-Caller-Skill": "test-transport"}
        created = client.post(
            "/v1/scillm/opencode/transport/runs",
            headers=headers,
            json={"dag_node_id": "node-1", "workspace": str(tmp_path)},
        )
        run_id = created.json()["transport_run_id"]
        with client.stream(
            "GET",
            f"/v1/scillm/opencode/transport/runs/{run_id}/events/stream",
            headers=headers,
            params={"after_line": 7, "timeout_s": 15.0, "heartbeat_s": 5.0},
        ) as resp:
            assert resp.status_code == 200
            body = "".join(resp.iter_text())
    assert tail_calls == [7]
    assert "child.created" in body



def test_session_create_model_uses_id_field() -> None:
    from scillm.proxy.opencode_serve import _session_create_model

    assert _session_create_model("gpt-5.5") == {"id": "gpt-5.5", "providerID": "openai"}
    assert _session_create_model("openai/gpt-5.2-codex") == {
        "id": "gpt-5.2-codex",
        "providerID": "openai",
    }
    assert _session_create_model(None) is None


def test_parent_ui_model_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCILLM_OPENCODE_TRANSPORT_PARENT_MODEL", raising=False)
    from scillm.proxy.opencode_transport import parent_ui_model

    assert parent_ui_model() == "gpt-5.5"


def test_parent_ui_model_sanitizes_unsupported_oauth_pro(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_TRANSPORT_PARENT_MODEL", "gpt-5.5-pro")
    monkeypatch.delenv("SCILLM_OPENCODE_TRANSPORT_WORKER_MODEL", raising=False)
    from scillm.proxy.opencode_transport import parent_ui_model, worker_message_model

    assert parent_ui_model() == "gpt-5.5"
    assert worker_message_model() == "gpt-5.5"


@pytest.mark.asyncio
async def test_create_transport_run_pins_parent_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("SCILLM_OPENCODE_TRANSPORT_PARENT_MODEL", "gpt-5.2-codex")
    transport = OpenCodeTransport()
    mock_client = AsyncMock()
    mock_client.create_session = AsyncMock(return_value={"id": "ses-parent"})
    mock_client.settings.base_url = "http://127.0.0.1:4098"

    await transport.create_transport_run(
        mock_client,
        dag_node_id="dag-pin",
        workspace=str(tmp_path),
        transport_run_id="otr-pin-model",
    )
    mock_client.create_session.assert_awaited_once()
    kwargs = mock_client.create_session.await_args.kwargs
    assert kwargs.get("model") == "gpt-5.2-codex"


@pytest.mark.asyncio
async def test_post_message_sync_fails_closed_on_opencode_message_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scillm.proxy.opencode_transport import ChildAttempt, TransportState, TransportStore

    monkeypatch.setenv("SCILLM_OPENCODE_TRANSPORT_PARENT_MODEL", "gpt-5.5-pro")
    monkeypatch.delenv("SCILLM_OPENCODE_TRANSPORT_WORKER_MODEL", raising=False)
    store = TransportStore(tmp_path / "transport")
    transport = OpenCodeTransport(store=store)
    transport.prepare_worker_prompt = AsyncMock(return_value=("do work", []))
    transport.mirror_worker_dispatch = AsyncMock()
    transport.mirror_worker_completed = AsyncMock()
    child = ChildAttempt(
        subagent_run_id="otr-error-patch-1",
        role="patch",
        child_session_id="ses-child",
        agent="build",
        attempt_id=1,
        mode="workspace_write",
        agent_id="patch-worker",
    )
    state = TransportState(
        transport_run_id="otr-error",
        dag_node_id="node-1",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
        opencode_url="http://127.0.0.1:4098",
        active_subagent_run_id=child.subagent_run_id,
        children=[child.to_dict()],
    )
    store.save(state)
    payload = {
        "info": {
            "id": "msg-error",
            "error": {
                "name": "APIError",
                "data": {
                    "message": "Bad Request: gpt-5.5-pro is not supported",
                    "statusCode": 400,
                    "isRetryable": False,
                    "responseBody": "{\"detail\":\"unsupported\"}",
                },
            },
        },
        "parts": [],
    }
    client = AsyncMock()
    client.session_status_map = AsyncMock(return_value={})
    client.send_message = AsyncMock(return_value=payload)

    with pytest.raises(ProxyError) as exc_info:
        await transport.post_message_sync(
            client,
            state,
            child,
            prompt="do work",
            timeout_s=30,
            wait_idle=False,
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.error_type == "blocked_substrate"
    assert exc_info.value.details["failure_type"] == "provider_error"
    assert exc_info.value.details["provider_error"]["status_code"] == 400
    assert exc_info.value.details["terminal_result"]["delivery_state"] == "blocked"
    assert client.send_message.await_args.kwargs["model"] == "gpt-5.5"
    loaded = store.load("otr-error")
    assert loaded.active_subagent_run_id == ""
    assert loaded.children[0]["delivery_state"] == "blocked"
    assert loaded.children[0]["active"] is False
    events = [
        json.loads(line)
        for line in store.events_path("otr-error").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert events[-1]["event_type"] == "message.blocked"
    assert events[-1]["agent_id"] == "patch-worker"
    assert events[-1]["failure_type"] == "provider_error"
    assert "gpt-5.5-pro is not supported" in events[-1]["blocked_reason"]
    assert events[-1]["provider_error"]["status_code"] == 400


@pytest.mark.asyncio
async def test_post_message_sync_blocks_on_concrete_worker_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scillm.proxy.opencode_transport import ChildAttempt, TransportState, TransportStore

    monkeypatch.setenv("SCILLM_OPENCODE_TRANSPORT_WORKER_MODEL", "gpt-5.5")
    store = TransportStore(tmp_path / "transport")
    transport = OpenCodeTransport(store=store)
    transport.prepare_worker_prompt = AsyncMock(return_value=("do work", []))
    transport.mirror_worker_dispatch = AsyncMock()
    transport.mirror_worker_completed = AsyncMock()
    child = ChildAttempt(
        subagent_run_id="otr-blocker-patch-1",
        role="patch",
        child_session_id="ses-child",
        agent="build",
        attempt_id=1,
        mode="workspace_write",
        agent_id="patch-worker",
    )
    state = TransportState(
        transport_run_id="otr-blocker",
        dag_node_id="node-1",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
        opencode_url="http://127.0.0.1:4098",
        active_subagent_run_id=child.subagent_run_id,
        children=[child.to_dict()],
    )
    store.save(state)
    payload = {
        "info": {"id": "msg-blocker", "role": "assistant"},
        "parts": [
            {
                "type": "text",
                "text": "PATCH_DELEGATE_BLOCKED - permission denied writing src/calc.py",
            }
        ],
    }
    client = AsyncMock()
    client.session_status_map = AsyncMock(return_value={})
    client.send_message = AsyncMock(return_value=payload)
    client.diff = AsyncMock(return_value=[{"path": "should-not-be-read"}])

    result = await transport.post_message_sync(
        client,
        state,
        child,
        prompt="do work",
        timeout_s=30,
        wait_idle=False,
    )

    assert result["delivery_state"] == "blocked"
    assert result["blocked_reason"] == "permission_denied"
    assert result["receipt_classifier"]["has_concrete_blocker"] is True
    client.diff.assert_not_awaited()
    transport.mirror_worker_completed.assert_not_awaited()
    loaded = store.load("otr-blocker")
    assert loaded.active_subagent_run_id == ""
    assert loaded.children[0]["delivery_state"] == "blocked"
    assert loaded.children[0]["active"] is False
    events = [
        json.loads(line)
        for line in store.events_path("otr-blocker").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert events[-1]["event_type"] == "message.blocked"
    assert events[-1]["blocked_reason"] == "permission_denied"


@pytest.mark.asyncio
async def test_post_message_sync_blocks_workspace_write_without_materialized_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scillm.proxy.opencode_transport import ChildAttempt, TransportState, TransportStore

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "target.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)

    monkeypatch.setenv("SCILLM_OPENCODE_TRANSPORT_WORKER_MODEL", "gpt-5.5")
    store = TransportStore(tmp_path / "transport")
    transport = OpenCodeTransport(store=store)
    transport.prepare_worker_prompt = AsyncMock(return_value=("change target", []))
    transport.mirror_worker_dispatch = AsyncMock()
    transport.mirror_worker_completed = AsyncMock()
    child = ChildAttempt(
        subagent_run_id="otr-no-change-patch-1",
        role="patch",
        child_session_id="ses-child",
        agent="build",
        attempt_id=1,
        mode="workspace_write",
        agent_id="patch-worker",
    )
    state = TransportState(
        transport_run_id="otr-no-change",
        dag_node_id="node-1",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
        opencode_url="http://127.0.0.1:4098",
        active_subagent_run_id=child.subagent_run_id,
        children=[child.to_dict()],
    )
    store.save(state)
    payload = {
        "info": {"id": "msg-no-change", "role": "assistant"},
        "parts": [{"type": "text", "text": "I changed target.txt."}],
    }
    client = AsyncMock()
    client.session_status_map = AsyncMock(return_value={})
    client.send_message = AsyncMock(return_value=payload)
    client.diff = AsyncMock(return_value=[])

    result = await transport.post_message_sync(
        client,
        state,
        child,
        prompt="do work",
        timeout_s=30,
        wait_idle=False,
    )

    assert result["delivery_state"] == "blocked"
    assert result["failure_type"] == "materialization_missing"
    assert result["blocked_reason"] == "no_materialized_change"
    assert result["assistant_text_trusted_as_change_proof"] is False
    assert result["materialization"]["materialized_change"] is False
    assert result["opencode_diff"] == []
    transport.mirror_worker_completed.assert_not_awaited()
    loaded = store.load("otr-no-change")
    assert loaded.active_subagent_run_id == ""
    assert loaded.children[0]["delivery_state"] == "blocked"


@pytest.mark.asyncio
async def test_post_message_sync_blocks_workspace_write_when_workspace_uninspectable(tmp_path: Path) -> None:
    from scillm.proxy.opencode_transport import ChildAttempt, TransportState, TransportStore

    missing_workspace = tmp_path / "missing-workspace"
    store = TransportStore(tmp_path / "transport")
    transport = OpenCodeTransport(store=store)
    transport.prepare_worker_prompt = AsyncMock(return_value=("change target", []))
    transport.mirror_worker_dispatch = AsyncMock()
    transport.mirror_worker_completed = AsyncMock()
    child = ChildAttempt(
        subagent_run_id="otr-missing-workspace-patch-1",
        role="patch",
        child_session_id="ses-child",
        agent="build",
        attempt_id=1,
        mode="workspace_write",
        agent_id="patch-worker",
    )
    state = TransportState(
        transport_run_id="otr-missing-workspace",
        dag_node_id="node-1",
        parent_session_id="ses-parent",
        workspace=str(missing_workspace),
        opencode_url="http://127.0.0.1:4098",
        active_subagent_run_id=child.subagent_run_id,
        children=[child.to_dict()],
    )
    store.save(state)
    payload = {
        "info": {"id": "msg-no-change", "role": "assistant"},
        "parts": [{"type": "text", "text": "I changed target.txt."}],
    }
    client = AsyncMock()
    client.session_status_map = AsyncMock(return_value={})
    client.send_message = AsyncMock(return_value=payload)
    client.diff = AsyncMock(return_value=[])

    result = await transport.post_message_sync(
        client,
        state,
        child,
        prompt="do work",
        timeout_s=30,
        wait_idle=False,
    )

    assert result["delivery_state"] == "blocked"
    assert result["failure_type"] == "materialization_missing"
    assert result["blocked_reason"] == "no_materialized_change"
    assert result["materialization"]["workspace_exists"] is False
    assert result["materialization"]["error"] == "workspace_missing"
    assert result["opencode_diff"] == []
    transport.mirror_worker_completed.assert_not_awaited()


def test_model_body_value_maps_gpt_alias_to_provider_object() -> None:
    from scillm.proxy.opencode_serve import _model_body_value

    assert _model_body_value("gpt-5.5") == {"providerID": "openai", "modelID": "gpt-5.5"}
    assert _model_body_value("openai/gpt-5.5") == {"providerID": "openai", "modelID": "gpt-5.5"}
    assert _model_body_value(None) is None

def test_transport_events_stream_unknown_run_returns_error_before_sse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TV-CR002: missing run must fail before StreamingResponse (no 200 SSE shell)."""
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")
    client = TestClient(app)
    headers = {"X-Caller-Skill": "test-transport"}
    resp = client.get(
        "/v1/scillm/opencode/transport/runs/otr-does-not-exist/events/stream",
        headers=headers,
    )
    assert resp.status_code == 404
    assert resp.headers.get("content-type", "").startswith("application/json")


def test_transport_events_stream_tail_failure_returns_error_before_sse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    class _Settings:
        base_url = "http://127.0.0.1:4098"

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.settings = _Settings()
    mock_client.create_session.return_value = {"id": "ses-parent"}

    async def _empty_stream():
        if False:
            yield b""

    mock_client.iter_event_stream = lambda: _empty_stream()

    def _boom_tail(_run_id: str, *, after_line: int = 0):
        raise OSError("tail failed")

    with patch("scillm.proxy.opencode_transport_api.OpenCodeServeClient", return_value=mock_client), patch.object(
        OpenCodeTransport,
        "tail_transport_events",
        side_effect=_boom_tail,
    ):
        client = TestClient(app)
        headers = {"X-Caller-Skill": "test-transport"}
        created = client.post(
            "/v1/scillm/opencode/transport/runs",
            headers=headers,
            json={"dag_node_id": "node-1", "workspace": str(tmp_path)},
        )
        run_id = created.json()["transport_run_id"]
        client_no_raise = TestClient(app, raise_server_exceptions=False)
        resp = client_no_raise.get(
            f"/v1/scillm/opencode/transport/runs/{run_id}/events/stream",
            headers=headers,
        )
    assert resp.status_code == 500
    assert "text/event-stream" not in resp.headers.get("content-type", "")


def test_transport_message_stream_rejects_nonempty_git_diff_in_propose_patches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TV-STREAM-WRITE-ALLOWLIST: default streaming path must reject dirty worktree."""
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    class _Settings:
        base_url = "http://127.0.0.1:4098"

    parent = {"id": "ses-parent"}
    child = {"id": "ses-child"}
    message = {
        "info": {"id": "msg-1", "sessionID": "ses-child"},
        "parts": [{"type": "text", "text": "done"}],
    }
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.settings = _Settings()
    mock_client.create_session.side_effect = [parent, child]
    mock_client.send_message.return_value = message
    mock_client.list_messages.return_value = [message]
    mock_client.session_status_map.return_value = {"ses-child": {"status": "idle"}}
    mock_client.diff.return_value = []
    mock_client.abort.return_value = True
    mock_client.iter_event_stream = lambda: _iter_opencode_sse(
        _opencode_sse_bytes("session.idle", session_id="ses-child")
    )

    with patch("scillm.proxy.opencode_transport_api.OpenCodeServeClient", return_value=mock_client), patch(
        "scillm.proxy.opencode_transport_stream.git_diff_empty", return_value=False
    ):
        client = TestClient(app)
        headers = {"X-Caller-Skill": "test-transport"}
        created = client.post(
            "/v1/scillm/opencode/transport/runs",
            headers=headers,
            json={"dag_node_id": "node-1", "workspace": str(tmp_path)},
        )
        run_id = created.json()["transport_run_id"]
        client.post(
            f"/v1/scillm/opencode/transport/runs/{run_id}/children",
            headers=headers,
            json={"role": "patch", "agent": "explore", "mode": "propose_patches"},
        )
        with client.stream(
            "POST",
            f"/v1/scillm/opencode/transport/runs/{run_id}/message",
            headers=headers,
            json={"prompt": "stream me", "stream": True, "timeout_s": 30.0},
        ) as resp:
            assert resp.status_code == 200
            body = "".join(resp.iter_text())
    assert "write_allowlist_violation" in body or "empty git diff" in body
    assert "message.completed" not in body


@pytest.mark.asyncio
async def test_transport_message_stream_blocks_workspace_write_without_materialized_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scillm.proxy.opencode_transport import ChildAttempt, TransportState, TransportStore

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "target.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)

    store = TransportStore(tmp_path / "transport")
    transport = OpenCodeTransport(store=store)
    transport.prepare_worker_prompt = AsyncMock(return_value=("change target", []))
    transport.mirror_worker_dispatch = AsyncMock()
    transport.mirror_worker_completed = AsyncMock()
    child = ChildAttempt(
        subagent_run_id="otr-stream-no-change-patch-1",
        role="patch",
        child_session_id="ses-child",
        agent="build",
        attempt_id=1,
        mode="workspace_write",
        agent_id="patch-worker",
    )
    state = TransportState(
        transport_run_id="otr-stream-no-change",
        dag_node_id="node-1",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
        opencode_url="http://127.0.0.1:4098",
        active_subagent_run_id=child.subagent_run_id,
        children=[child.to_dict()],
    )
    store.save(state)
    payload = {
        "info": {"id": "msg-no-change", "role": "assistant"},
        "parts": [{"type": "text", "text": "I changed target.txt."}],
    }
    client = AsyncMock()
    client.session_status_map = AsyncMock(return_value={})
    client.send_message = AsyncMock(return_value=payload)
    client.diff = AsyncMock(return_value=[])
    client.iter_event_stream = lambda: _iter_opencode_sse(
        _opencode_sse_bytes("session.idle", session_id="ses-child")
    )
    client.abort = AsyncMock(return_value=True)

    rows = [
        row
        async for row in transport.iter_transport_message_stream(
            client,
            state,
            child,
            prompt="do work",
            timeout_s=30,
            heartbeat_s=1,
            wait_idle=False,
        )
    ]

    terminal = rows[-1]
    assert terminal["event_type"] == "message.blocked"
    result = terminal["result"]
    assert result["delivery_state"] == "blocked"
    assert result["failure_type"] == "materialization_missing"
    assert result["blocked_reason"] == "no_materialized_change"
    assert result["materialization"]["materialized_change"] is False
    assert result["opencode_diff"] == []
    transport.mirror_worker_completed.assert_not_awaited()

def test_parse_dialog_speaker_and_human_classification() -> None:
    from scillm.proxy.opencode_transport import (
        dialog_turn_from_message,
        incorporate_human_dialog,
        is_human_turn,
        parse_dialog_speaker,
    )

    assert parse_dialog_speaker("**Project agent**\n\nhello") == "Project agent"
    assert parse_dialog_speaker("plain human question") is None

    human = dialog_turn_from_message(
        {
            "id": "msg-human-1",
            "info": {"role": "user"},
            "parts": [{"type": "text", "text": "Please focus on transport UX parity."}],
        }
    )
    assert human is not None
    assert is_human_turn(human)
    assert human.collaborator == "human"

    agent = dialog_turn_from_message(
        {
            "id": "msg-agent-1",
            "info": {"role": "user"},
            "parts": [{"type": "text", "text": "**Project agent**\n\ndispatching worker"}],
        }
    )
    assert agent is not None
    assert agent.collaborator == "project_agent"
    assert not is_human_turn(agent)

    merged = incorporate_human_dialog(
        "Review the transport contract.",
        [human],
    )
    assert "Human input" in merged
    assert "transport UX parity" in merged
    assert merged.endswith("Review the transport contract.")


@pytest.mark.asyncio
async def test_prepare_worker_prompt_incorporates_pending_human(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scillm.proxy.opencode_transport import OpenCodeTransport, TransportState

    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    transport = OpenCodeTransport()
    state = TransportState(
        transport_run_id="otr-human",
        dag_node_id="n1",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
        dialog_last_human_message_id="",
    )
    transport.store.save(state)

    client = AsyncMock()
    client.list_messages = AsyncMock(
        return_value=[
            {
                "id": "msg-1",
                "info": {"role": "user"},
                "parts": [{"type": "text", "text": "Human: check parent dialog URL first."}],
            }
        ]
    )

    effective, human = await transport.prepare_worker_prompt(client, state, "Do the review.")
    assert len(human) == 1
    assert "parent dialog URL" in effective
    transport.acknowledge_human_turns(state, human)
    assert state.dialog_last_human_message_id == "msg-1"

    client.list_messages.return_value = [
        {
            "id": "msg-1",
            "info": {"role": "user"},
            "parts": [{"type": "text", "text": "Human: check parent dialog URL first."}],
        }
    ]
    effective2, human2 = await transport.prepare_worker_prompt(client, state, "Again.")
    assert human2 == []
    assert effective2 == "Again."


def test_build_transport_observation_three_way_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scillm.proxy.opencode_transport import TransportState, build_transport_observation

    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    state = TransportState(
        transport_run_id="otr-3way",
        dag_node_id="n1",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
        opencode_url="http://127.0.0.1:4098",
    )
    obs = build_transport_observation(transport_run_id="otr-3way", state=state)
    assert obs.get("collaboration_mode") == "three_way"
    assert obs.get("human_can_participate") is True
    assert obs.get("scillm_dialog_api", "").endswith("/otr-3way/dialog")

def test_subagent_kind_label() -> None:
    from scillm.proxy.opencode_transport import subagent_kind_label

    assert subagent_kind_label("debugger") == "Debugger"
    assert subagent_kind_label("reviewer") == "Reviewer"


def test_default_skills_for_debugger_agent() -> None:
    from scillm.proxy.opencode_transport import default_skills_for_child

    skills = default_skills_for_child(role="patch", agent="scillm-debugger")
    assert "memory" in skills
    assert "debugger" in skills
    assert "scillm" in skills


def test_build_dialog_collaboration_contract_includes_children(tmp_path: Path) -> None:
    from scillm.proxy.opencode_transport import (
        ChildAttempt,
        TransportState,
        build_dialog_collaboration_contract,
    )

    child = ChildAttempt(
        subagent_run_id="otr-x-reviewer-1",
        role="reviewer",
        child_session_id="ses-child",
        agent="scillm-worker",
        attempt_id=1,
        skills=["memory", "scillm"],
        skills_materialized=["memory", "scillm"],
    )
    state = TransportState(
        transport_run_id="otr-x",
        dag_node_id="n1",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
        active_subagent_run_id=child.subagent_run_id,
        children=[child.to_dict()],
    )
    contract = build_dialog_collaboration_contract(transport_run_id="otr-x", state=state)
    assert contract["active_subagent"]["subagent_kind"] == "Reviewer"
    assert contract["children"][0]["skills_materialized"] == ["memory", "scillm"]


def test_enrich_dialog_turns_worker_and_spawn(tmp_path: Path) -> None:
    from scillm.proxy.opencode_transport import (
        ChildAttempt,
        DialogTurn,
        TransportState,
        enrich_dialog_turns,
    )

    child = ChildAttempt(
        subagent_run_id="otr-y-reviewer-1",
        role="reviewer",
        child_session_id="ses-c",
        agent="scillm-worker",
        attempt_id=1,
        skills_materialized=["memory", "scillm"],
    )
    state = TransportState(
        transport_run_id="otr-y",
        dag_node_id="n1",
        parent_session_id="ses-p",
        workspace=str(tmp_path),
        children=[child.to_dict()],
    )
    turns = [
        DialogTurn(
            message_id="m1",
            collaborator="project_agent",
            speaker="Project agent",
            text="**Project agent**\n\nSpawned **Reviewer** (`reviewer`) attempt 1.",
        ),
        DialogTurn(
            message_id="m2",
            collaborator="worker",
            speaker="Worker (reviewer)",
            text="**Worker (reviewer)**\n\nDone.",
        ),
    ]
    enriched = enrich_dialog_turns(turns, state)
    assert enriched[1].subagent_kind == "Reviewer"
    assert enriched[1].agent == "scillm-worker"
    assert enriched[1].skills == ["memory", "scillm"]


@pytest.mark.asyncio
async def test_create_child_materializes_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scillm.proxy.opencode_transport import OpenCodeTransport, TransportState

    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    transport = OpenCodeTransport()
    state = TransportState(
        transport_run_id="otr-skills",
        dag_node_id="n1",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
        opencode_url="http://127.0.0.1:4098",
    )
    transport.store.save(state)

    client = AsyncMock()
    client.create_session = AsyncMock(return_value={"id": "ses-child-1"})
    client.send_message = AsyncMock()

    child = await transport.create_child(
        client,
        state,
        role="debugger",
        agent="scillm-debugger",
        skills=["memory", "scillm"],
    )
    assert child.skills == ["memory", "scillm"]
    assert "memory" in child.skills_materialized or child.skills_materialized == []
    client.send_message.assert_awaited()

def test_default_skills_for_child_agent_id_code_reviewer() -> None:
    from scillm.proxy.opencode_transport import default_skills_for_child
    from scillm.proxy.worker_agents import reload_worker_index
    import os
    from pathlib import Path

    root = Path.home() / "workspace" / "experiments" / "agent-skills" / "agents"
    os.environ["SCILLM_WORKER_AGENTS_ROOT"] = str(root)
    reload_worker_index()
    skills = default_skills_for_child(role="patch", agent="explore", agent_id="code-reviewer")
    assert "review-code" in skills


def test_default_skills_for_child_unknown_agent_id_fails_closed() -> None:
    from scillm.proxy.opencode_transport import default_skills_for_child
    from scillm.proxy.worker_agents import WorkerAgentResolutionError

    with pytest.raises(WorkerAgentResolutionError, match="unknown worker agent_id"):
        default_skills_for_child(role="patch", agent="explore", agent_id="no-such-agent")


@pytest.mark.asyncio
async def test_create_child_agent_id_materializes_registry_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    from scillm.proxy.opencode_transport import OpenCodeTransport, TransportState
    from scillm.proxy.worker_agents import reload_worker_index

    agents_root = Path.home() / "workspace" / "experiments" / "agent-skills" / "agents"
    monkeypatch.setenv("SCILLM_WORKER_AGENTS_ROOT", str(agents_root))
    reload_worker_index()

    skill_root = tmp_path / "skills"
    for name in ("memory", "scillm", "review-code", "best-practices-python", "best-practices-scillm"):
        d = skill_root / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    monkeypatch.setenv("SCILLM_OPENCODE_SKILL_ROOTS", str(skill_root))
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))

    transport = OpenCodeTransport()
    state = TransportState(
        transport_run_id="otr-agent-id",
        dag_node_id="n1",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
        opencode_url="http://127.0.0.1:4098",
    )
    transport.store.save(state)

    client = AsyncMock()
    client.create_session = AsyncMock(return_value={"id": "ses-child-agent"})

    child = await transport.create_child(client, state, agent_id="code-reviewer")
    assert child.role == "reviewer"
    assert child.agent_id == "code-reviewer"
    assert "review-code" in child.skills


@pytest.mark.asyncio
async def test_fork_supersede_preserves_active_child_agent_id(tmp_path: Path) -> None:
    from scillm.proxy.opencode_transport import ChildAttempt, OpenCodeTransport, TransportState, TransportStore

    store = TransportStore(tmp_path / "transport")
    transport = OpenCodeTransport(store=store)
    active = ChildAttempt(
        subagent_run_id="otr-retry-patch-1",
        role="patch",
        child_session_id="ses-child-1",
        agent="build",
        attempt_id=1,
        delivery_state="completed",
        active=True,
        last_message_id="msg-1",
        mode="workspace_write",
        agent_id="patch-worker",
        skills=["code-runner"],
        skills_materialized=["code-runner"],
    )
    state = TransportState(
        transport_run_id="otr-retry",
        dag_node_id="node-1",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
        opencode_url="http://127.0.0.1:4098",
        active_subagent_run_id=active.subagent_run_id,
        children=[active.to_dict()],
    )
    store.save(state)

    client = AsyncMock()
    client.fork_session = AsyncMock(return_value={"id": "ses-child-2"})

    child = await transport.fork_supersede(
        client,
        state,
        role="patch",
        agent="build",
        reason="validation_failed",
        mode="workspace_write",
    )

    client.fork_session.assert_awaited_once_with("ses-child-1", message_id="msg-1", directory=str(tmp_path))
    assert child.agent_id == "patch-worker"
    loaded = store.load("otr-retry")
    assert loaded.children[0]["delivery_state"] == "superseded"
    assert loaded.children[0]["active"] is False
    assert loaded.children[1]["agent_id"] == "patch-worker"
    assert loaded.children[1]["active"] is True
    assert loaded.active_subagent_run_id == "otr-retry-patch-2"
    events = [
        json.loads(line)
        for line in store.events_path("otr-retry").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert events[-1]["event_type"] == "child.superseded"
    assert events[-1]["agent_id"] == "patch-worker"


@pytest.mark.asyncio
async def test_abort_active_child_marks_state_and_event(tmp_path: Path) -> None:
    from scillm.proxy.opencode_transport import ChildAttempt, OpenCodeTransport, TransportState, TransportStore

    store = TransportStore(tmp_path / "transport")
    transport = OpenCodeTransport(store=store)
    active = ChildAttempt(
        subagent_run_id="otr-abort-patch-1",
        role="patch",
        child_session_id="ses-child-1",
        agent="build",
        attempt_id=1,
        delivery_state="posted",
        active=True,
        last_message_id="msg-1",
        mode="workspace_write",
        agent_id="patch-worker",
    )
    state = TransportState(
        transport_run_id="otr-abort",
        dag_node_id="node-1",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
        opencode_url="http://127.0.0.1:4098",
        active_subagent_run_id=active.subagent_run_id,
        children=[active.to_dict()],
    )
    store.save(state)

    client = AsyncMock()
    client.abort = AsyncMock(return_value=True)

    result = await transport.abort_active_child(client, state, reason="canary_abort")

    client.abort.assert_awaited_once_with("ses-child-1", directory=str(tmp_path))
    assert result["schema"] == "scillm.opencode_transport.abort.v1"
    assert result["aborted"] is True
    assert result["delivery_state"] == "aborted"
    loaded = store.load("otr-abort")
    assert loaded.active_subagent_run_id == ""
    assert loaded.children[0]["active"] is False
    assert loaded.children[0]["delivery_state"] == "aborted"
    events = [
        json.loads(line)
        for line in store.events_path("otr-abort").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert events[-1]["event_type"] == "child.aborted"
    assert events[-1]["agent_id"] == "patch-worker"
    assert events[-1]["reason"] == "canary_abort"


@pytest.mark.asyncio
async def test_post_message_sync_preserves_abort_race_terminal_state(tmp_path: Path) -> None:
    from scillm.proxy.opencode_transport import ChildAttempt, OpenCodeTransport, TransportState, TransportStore

    store = TransportStore(tmp_path / "transport")
    transport = OpenCodeTransport(store=store)
    transport.prepare_worker_prompt = AsyncMock(return_value=("do work", []))
    transport.mirror_worker_dispatch = AsyncMock()
    transport.mirror_worker_completed = AsyncMock()
    child = ChildAttempt(
        subagent_run_id="otr-abort-race-patch-1",
        role="patch",
        child_session_id="ses-child-1",
        agent="build",
        attempt_id=1,
        delivery_state="posted",
        active=True,
        mode="workspace_write",
        agent_id="patch-worker",
    )
    state = TransportState(
        transport_run_id="otr-abort-race",
        dag_node_id="node-1",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
        opencode_url="http://127.0.0.1:4098",
        active_subagent_run_id=child.subagent_run_id,
        children=[child.to_dict()],
    )
    store.save(state)
    payload = {
        "info": {"role": "assistant", "id": "msg-after-abort"},
        "parts": [{"type": "text", "text": "late completion"}],
    }

    async def _send_message_after_abort(*_args, **_kwargs):
        latest = store.load("otr-abort-race")
        latest.children[0]["delivery_state"] = "aborted"
        latest.children[0]["active"] = False
        latest.active_subagent_run_id = ""
        store.save(latest)
        return payload

    client = AsyncMock()
    client.session_status_map = AsyncMock(return_value={})
    client.send_message = AsyncMock(side_effect=_send_message_after_abort)
    client.diff = AsyncMock(return_value=[{"path": "should-not-be-read"}])

    result = await transport.post_message_sync(
        client,
        state,
        child,
        prompt="do work",
        timeout_s=30,
        wait_idle=False,
    )

    assert result["delivery_state"] == "aborted"
    assert result["assistant_text"] == "late completion"
    client.diff.assert_not_awaited()
    transport.mirror_worker_completed.assert_not_awaited()
    loaded = store.load("otr-abort-race")
    assert loaded.active_subagent_run_id == ""
    assert loaded.children[0]["delivery_state"] == "aborted"
    assert loaded.children[0]["active"] is False
    events = [
        json.loads(line)
        for line in store.events_path("otr-abort-race").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    event_types = [row["event_type"] for row in events]
    assert "message.aborted" in event_types
    assert "message.completed" not in event_types


@pytest.mark.asyncio
async def test_update_child_does_not_overwrite_persisted_abort(tmp_path: Path) -> None:
    from scillm.proxy.opencode_transport import ChildAttempt, OpenCodeTransport, TransportState, TransportStore

    store = TransportStore(tmp_path / "transport")
    transport = OpenCodeTransport(store=store)
    child = ChildAttempt(
        subagent_run_id="otr-update-race-patch-1",
        role="patch",
        child_session_id="ses-child-1",
        agent="build",
        attempt_id=1,
        delivery_state="posted",
        active=True,
        mode="workspace_write",
        agent_id="patch-worker",
    )
    stale_state = TransportState(
        transport_run_id="otr-update-race",
        dag_node_id="node-1",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
        opencode_url="http://127.0.0.1:4098",
        active_subagent_run_id=child.subagent_run_id,
        children=[child.to_dict()],
    )
    store.save(stale_state)
    persisted = store.load("otr-update-race")
    persisted.children[0]["delivery_state"] = "aborted"
    persisted.children[0]["active"] = False
    persisted.active_subagent_run_id = ""
    store.save(persisted)

    stale_child = ChildAttempt.from_dict(stale_state.children[0])
    stale_child.delivery_state = "completed"
    stale_child.active = True
    transport._update_child(stale_state, stale_child)

    loaded = store.load("otr-update-race")
    assert loaded.active_subagent_run_id == ""
    assert loaded.children[0]["delivery_state"] == "aborted"
    assert loaded.children[0]["active"] is False
    assert stale_state.active_subagent_run_id == ""
    assert stale_state.children[0]["delivery_state"] == "aborted"


@pytest.mark.asyncio
async def test_post_message_sync_classifies_opencode_aborted_error_as_aborted(tmp_path: Path) -> None:
    from scillm.proxy.opencode_transport import ChildAttempt, OpenCodeTransport, TransportState, TransportStore

    store = TransportStore(tmp_path / "transport")
    transport = OpenCodeTransport(store=store)
    transport.prepare_worker_prompt = AsyncMock(return_value=("do work", []))
    transport.mirror_worker_dispatch = AsyncMock()
    transport.mirror_worker_completed = AsyncMock()
    child = ChildAttempt(
        subagent_run_id="otr-aborted-error-patch-1",
        role="patch",
        child_session_id="ses-child-1",
        agent="build",
        attempt_id=1,
        delivery_state="posted",
        active=True,
        mode="workspace_write",
        agent_id="patch-worker",
    )
    state = TransportState(
        transport_run_id="otr-aborted-error",
        dag_node_id="node-1",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
        opencode_url="http://127.0.0.1:4098",
        active_subagent_run_id=child.subagent_run_id,
        children=[child.to_dict()],
    )
    store.save(state)
    payload = {
        "info": {
            "id": "msg-aborted",
            "error": {
                "name": "MessageAbortedError",
                "data": {"message": "Aborted"},
            },
        },
        "parts": [],
    }
    client = AsyncMock()
    client.session_status_map = AsyncMock(return_value={})
    client.send_message = AsyncMock(return_value=payload)
    client.diff = AsyncMock(return_value=[{"path": "should-not-be-read"}])

    result = await transport.post_message_sync(
        client,
        state,
        child,
        prompt="do work",
        timeout_s=30,
        wait_idle=False,
    )

    assert result["delivery_state"] == "aborted"
    client.diff.assert_not_awaited()
    transport.mirror_worker_completed.assert_not_awaited()
    loaded = store.load("otr-aborted-error")
    assert loaded.active_subagent_run_id == ""
    assert loaded.children[0]["delivery_state"] == "aborted"
    assert loaded.children[0]["active"] is False
    events = [
        json.loads(line)
        for line in store.events_path("otr-aborted-error").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert events[-1]["event_type"] == "message.aborted"
    assert events[-1]["agent_id"] == "patch-worker"


@pytest.mark.asyncio
async def test_post_message_sync_marks_wait_idle_timeout_terminal(tmp_path: Path) -> None:
    from scillm.proxy.opencode_transport import ChildAttempt, OpenCodeTransport, TransportState, TransportStore

    store = TransportStore(tmp_path / "transport")
    transport = OpenCodeTransport(store=store)
    transport.prepare_worker_prompt = AsyncMock(return_value=("do work", []))
    transport.mirror_worker_dispatch = AsyncMock()
    transport.mirror_worker_completed = AsyncMock()
    transport.wait_idle = AsyncMock(side_effect=ProxyError(504, "session ses-child-1 did not become idle", "timeout"))
    child = ChildAttempt(
        subagent_run_id="otr-timeout-patch-1",
        role="patch",
        child_session_id="ses-child-1",
        agent="build",
        attempt_id=1,
        delivery_state="posted",
        active=True,
        mode="workspace_write",
        agent_id="patch-worker",
    )
    state = TransportState(
        transport_run_id="otr-timeout",
        dag_node_id="node-1",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
        opencode_url="http://127.0.0.1:4098",
        active_subagent_run_id=child.subagent_run_id,
        children=[child.to_dict()],
    )
    store.save(state)
    payload = {
        "info": {"role": "assistant", "id": "msg-timeout"},
        "parts": [],
    }
    client = AsyncMock()
    client.session_status_map = AsyncMock(return_value={})
    client.send_message = AsyncMock(return_value=payload)
    client.abort = AsyncMock(return_value=True)
    client.diff = AsyncMock(return_value=[{"path": "should-not-be-read"}])

    result = await transport.post_message_sync(
        client,
        state,
        child,
        prompt="do work",
        timeout_s=30,
        wait_idle=True,
    )

    assert result["delivery_state"] == "timed_out"
    assert result["failure_type"] == "timeout"
    assert result["abort_succeeded"] is True
    client.abort.assert_awaited_once_with("ses-child-1", directory=str(tmp_path))
    client.diff.assert_not_awaited()
    transport.mirror_worker_completed.assert_not_awaited()
    loaded = store.load("otr-timeout")
    assert loaded.active_subagent_run_id == ""
    assert loaded.children[0]["delivery_state"] == "timed_out"
    assert loaded.children[0]["active"] is False
    events = [
        json.loads(line)
        for line in store.events_path("otr-timeout").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert events[-1]["event_type"] == "message.timed_out"
    assert events[-1]["failure_type"] == "timeout"
    assert "message.completed" not in [row["event_type"] for row in events]


def test_transport_message_stream_marks_timeout_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    class _Settings:
        base_url = "http://127.0.0.1:4098"

    parent = {"id": "ses-parent"}
    child = {"id": "ses-child"}
    message = {
        "info": {"id": "msg-timeout", "sessionID": "ses-child"},
        "parts": [],
    }
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.settings = _Settings()
    mock_client.create_session.side_effect = [parent, child]
    mock_client.send_message.return_value = message
    mock_client.session_status_map.return_value = {"ses-child": {"status": "idle"}}
    mock_client.abort.return_value = True

    async def _empty_stream():
        if False:
            yield b""

    mock_client.iter_event_stream = lambda: _empty_stream()

    with patch("scillm.proxy.opencode_transport_api.OpenCodeServeClient", return_value=mock_client), patch.object(
        OpenCodeTransport,
        "wait_idle",
        side_effect=ProxyError(504, "session ses-child did not become idle", "timeout"),
    ):
        client = TestClient(app)
        headers = {"X-Caller-Skill": "test-transport"}
        created = client.post(
            "/v1/scillm/opencode/transport/runs",
            headers=headers,
            json={"dag_node_id": "node-1", "workspace": str(tmp_path)},
        )
        run_id = created.json()["transport_run_id"]
        client.post(
            f"/v1/scillm/opencode/transport/runs/{run_id}/children",
            headers=headers,
            json={"role": "patch", "agent": "build", "mode": "workspace_write"},
        )
        with client.stream(
            "POST",
            f"/v1/scillm/opencode/transport/runs/{run_id}/message",
            headers=headers,
            json={"prompt": "stream timeout", "stream": True, "timeout_s": 30.0},
        ) as resp:
            assert resp.status_code == 200
            body = "".join(resp.iter_text())

    assert "message.timed_out" in body
    assert "message.completed" not in body
    state = TransportStore(tmp_path / "transport").load(run_id)
    assert state.active_subagent_run_id == ""
    assert state.children[0]["delivery_state"] == "timed_out"
    assert state.children[0]["active"] is False


def test_transport_message_stream_marks_provider_error_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    class _Settings:
        base_url = "http://127.0.0.1:4098"

    parent = {"id": "ses-parent"}
    child = {"id": "ses-child"}
    message = {
        "info": {
            "id": "msg-blocked",
            "sessionID": "ses-child",
            "error": {
                "name": "APIError",
                "data": {
                    "message": "Bad Request: model unavailable",
                    "statusCode": 400,
                    "isRetryable": False,
                },
            },
        },
        "parts": [],
    }
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.settings = _Settings()
    mock_client.create_session.side_effect = [parent, child]
    mock_client.send_message.return_value = message
    mock_client.session_status_map.return_value = {"ses-child": {"status": "idle"}}
    mock_client.abort.return_value = True

    async def _empty_stream():
        if False:
            yield b""

    mock_client.iter_event_stream = lambda: _empty_stream()

    with patch("scillm.proxy.opencode_transport_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        headers = {"X-Caller-Skill": "test-transport"}
        created = client.post(
            "/v1/scillm/opencode/transport/runs",
            headers=headers,
            json={"dag_node_id": "node-1", "workspace": str(tmp_path)},
        )
        run_id = created.json()["transport_run_id"]
        client.post(
            f"/v1/scillm/opencode/transport/runs/{run_id}/children",
            headers=headers,
            json={"role": "patch", "agent": "build", "mode": "workspace_write"},
        )
        with client.stream(
            "POST",
            f"/v1/scillm/opencode/transport/runs/{run_id}/message",
            headers=headers,
            json={"prompt": "stream blocked", "stream": True, "timeout_s": 30.0},
        ) as resp:
            assert resp.status_code == 200
            body = "".join(resp.iter_text())

    assert "message.blocked" in body
    assert "message.completed" not in body
    assert "message.failed" not in body
    state = TransportStore(tmp_path / "transport").load(run_id)
    assert state.active_subagent_run_id == ""
    assert state.children[0]["delivery_state"] == "blocked"
    assert state.children[0]["active"] is False


def test_transport_message_stream_blocks_on_concrete_worker_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    class _Settings:
        base_url = "http://127.0.0.1:4098"

    parent = {"id": "ses-parent"}
    child = {"id": "ses-child"}
    message = {
        "info": {"id": "msg-blocker", "sessionID": "ses-child", "role": "assistant"},
        "parts": [
            {
                "type": "text",
                "text": "PATCH_DELEGATE_BLOCKED - permission denied writing src/calc.py",
            }
        ],
    }
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.settings = _Settings()
    mock_client.create_session.side_effect = [parent, child]
    mock_client.send_message.return_value = message
    mock_client.session_status_map.return_value = {"ses-child": {"status": "idle"}}
    mock_client.diff.return_value = [{"path": "should-not-be-read"}]

    async def _empty_stream():
        if False:
            yield b""

    mock_client.iter_event_stream = lambda: _empty_stream()

    with patch("scillm.proxy.opencode_transport_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        headers = {"X-Caller-Skill": "test-transport"}
        created = client.post(
            "/v1/scillm/opencode/transport/runs",
            headers=headers,
            json={"dag_node_id": "node-1", "workspace": str(tmp_path)},
        )
        run_id = created.json()["transport_run_id"]
        client.post(
            f"/v1/scillm/opencode/transport/runs/{run_id}/children",
            headers=headers,
            json={"role": "patch", "agent": "build", "mode": "workspace_write"},
        )
        with client.stream(
            "POST",
            f"/v1/scillm/opencode/transport/runs/{run_id}/message",
            headers=headers,
            json={"prompt": "stream blocker", "stream": True, "timeout_s": 30.0},
        ) as resp:
            assert resp.status_code == 200
            body = "".join(resp.iter_text())

    assert "message.blocked" in body
    assert "permission_denied" in body
    assert "message.completed" not in body
    assert "message.failed" not in body
    mock_client.diff.assert_not_awaited()
    state = TransportStore(tmp_path / "transport").load(run_id)
    assert state.active_subagent_run_id == ""
    assert state.children[0]["delivery_state"] == "blocked"
    assert state.children[0]["active"] is False


def test_transport_abort_endpoint_aborts_active_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scillm.proxy.opencode_transport import ChildAttempt, TransportState, TransportStore

    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    store = TransportStore(tmp_path / "transport")
    active = ChildAttempt(
        subagent_run_id="otr-abort-route-patch-1",
        role="patch",
        child_session_id="ses-child-route",
        agent="build",
        attempt_id=1,
        delivery_state="posted",
        active=True,
        mode="workspace_write",
        agent_id="patch-worker",
    )
    state = TransportState(
        transport_run_id="otr-abort-route",
        dag_node_id="node-1",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
        opencode_url="http://127.0.0.1:4098",
        active_subagent_run_id=active.subagent_run_id,
        children=[active.to_dict()],
    )
    store.save(state)

    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    class _Settings:
        base_url = "http://127.0.0.1:4098"

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.settings = _Settings()
    mock_client.abort.return_value = True

    with patch("scillm.proxy.opencode_transport_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        response = client.post(
            "/v1/scillm/opencode/transport/runs/otr-abort-route/abort",
            headers={"X-Caller-Skill": "test-transport"},
            json={"reason": "route_canary_abort"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema"] == "scillm.opencode_transport.abort.v1"
    assert body["aborted"] is True
    assert body["delivery_state"] == "aborted"
    assert body["observation"]["active_child_session_id"] == ""
    mock_client.abort.assert_awaited_once_with("ses-child-route", directory=str(tmp_path))


def test_transport_permission_reply_reject_blocks_active_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scillm.proxy.opencode_transport import ChildAttempt, TransportState, TransportStore

    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    store = TransportStore(tmp_path / "transport")
    active = ChildAttempt(
        subagent_run_id="otr-permission-route-patch-1",
        role="patch",
        child_session_id="ses-child-route",
        agent="build",
        attempt_id=1,
        delivery_state="waiting_permission",
        active=True,
        mode="workspace_write",
        agent_id="patch-worker",
    )
    state = TransportState(
        transport_run_id="otr-permission-route",
        dag_node_id="node-1",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
        opencode_url="http://127.0.0.1:4098",
        active_subagent_run_id=active.subagent_run_id,
        children=[active.to_dict()],
    )
    store.save(state)

    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    class _Settings:
        base_url = "http://127.0.0.1:4098"

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.settings = _Settings()
    mock_client.reply_permission.return_value = True

    with patch("scillm.proxy.opencode_transport_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        response = client.post(
            "/v1/scillm/opencode/transport/runs/otr-permission-route/permission/reply",
            headers={"X-Caller-Skill": "test-transport"},
            json={"permission_id": "perm-route-1", "response": "reject"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["delivery_state"] == "blocked"
    assert body["blocked_reason"] == "permission_rejected"
    assert body["permission_id"] == "perm-route-1"
    assert body["permission_response"] == "reject"
    assert body["reply_succeeded"] is True
    assert body["observation"]["active_child_session_id"] == ""
    mock_client.reply_permission.assert_awaited_once_with(
        "ses-child-route",
        permission_id="perm-route-1",
        response="reject",
        directory=str(tmp_path),
    )
    loaded = store.load("otr-permission-route")
    assert loaded.active_subagent_run_id == ""
    assert loaded.children[0]["delivery_state"] == "blocked"
    assert loaded.children[0]["active"] is False
    events = [
        json.loads(line)
        for line in store.events_path("otr-permission-route").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert events[-2]["event_type"] == "permission.replied"
    assert events[-1]["event_type"] == "message.blocked"
    assert events[-1]["permission_response"] == "reject"


@pytest.mark.asyncio
async def test_mark_child_timed_out_wins_over_abort_race(tmp_path: Path) -> None:
    from scillm.proxy.opencode_transport import ChildAttempt, TransportState, TransportStore

    store = TransportStore(tmp_path / "transport")
    transport = OpenCodeTransport(store=store)
    child = ChildAttempt(
        subagent_run_id="otr-timeout-race-patch-1",
        role="patch",
        child_session_id="ses-child-timeout-race",
        agent="build",
        attempt_id=1,
        delivery_state="posted",
        active=True,
        mode="workspace_write",
    )
    state = TransportState(
        transport_run_id="otr-timeout-race",
        dag_node_id="node-1",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
        opencode_url="http://127.0.0.1:4098",
        active_subagent_run_id=child.subagent_run_id,
        children=[child.to_dict()],
    )
    store.save(state)

    async def _abort_side_effect(*_args, **_kwargs):
        racing_child = ChildAttempt.from_dict(store.load("otr-timeout-race").children[0])
        racing_child.delivery_state = "aborted"
        racing_child.active = False
        transport._update_child(state, racing_child)
        return True

    client = AsyncMock()
    client.abort.side_effect = _abort_side_effect

    result = await transport.mark_child_timed_out(
        client,
        state,
        child,
        reason="test timeout",
        message_id="msg-timeout-race",
    )

    assert result["delivery_state"] == "timed_out"
    loaded = store.load("otr-timeout-race")
    assert loaded.active_subagent_run_id == ""
    assert loaded.children[0]["delivery_state"] == "timed_out"
    assert loaded.children[0]["active"] is False


@pytest.mark.asyncio
async def test_transport_stream_polls_pending_permission_when_bus_misses_it(tmp_path: Path) -> None:
    from scillm.proxy.opencode_transport import ChildAttempt, TransportState, TransportStore
    from scillm.proxy.opencode_transport_stream import TransportMessageStream

    store = TransportStore(tmp_path / "transport")
    transport = OpenCodeTransport(store=store)
    child = ChildAttempt(
        subagent_run_id="otr-permission-poll-patch-1",
        role="patch",
        child_session_id="ses-child-permission-poll",
        agent="build",
        attempt_id=1,
        delivery_state="posted",
        active=True,
        mode="workspace_write",
    )
    state = TransportState(
        transport_run_id="otr-permission-poll",
        dag_node_id="node-1",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
        opencode_url="http://127.0.0.1:4098",
        active_subagent_run_id=child.subagent_run_id,
        children=[child.to_dict()],
    )
    store.save(state)

    client = AsyncMock()
    client.list_permissions.return_value = [
        {
            "id": "per-poll-1",
            "sessionID": "ses-child-permission-poll",
            "permission": "bash",
            "patterns": ["printf *"],
        }
    ]
    stream = TransportMessageStream(
        transport,
        client,
        state,
        child,
        prompt="probe",
        system=None,
        model="gpt-5.5",
        timeout_s=30,
        heartbeat_s=1,
        wait_idle=True,
    )

    await stream._poll_pending_permissions()

    row = await stream.queue.get()
    assert row["event_type"] == "permission_requested"
    assert row["permission_id"] == "per-poll-1"
    assert row["source_hint"] == "opencode_permission_poll"
    loaded = store.load("otr-permission-poll")
    assert loaded.children[0]["delivery_state"] == "waiting_permission"


@pytest.mark.asyncio
async def test_create_child_unknown_agent_id_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    from scillm.proxy.errors import ProxyError
    from scillm.proxy.opencode_transport import OpenCodeTransport, TransportState
    from scillm.proxy.worker_agents import reload_worker_index

    agents_root = Path.home() / "workspace" / "experiments" / "agent-skills" / "agents"
    monkeypatch.setenv("SCILLM_WORKER_AGENTS_ROOT", str(agents_root))
    reload_worker_index()

    transport = OpenCodeTransport()
    state = TransportState(
        transport_run_id="otr-agent-id-unknown",
        dag_node_id="n1",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
        opencode_url="http://127.0.0.1:4098",
    )
    transport.store.save(state)

    client = AsyncMock()
    client.create_session = AsyncMock(return_value={"id": "ses-should-not-create"})

    with pytest.raises(ProxyError) as exc_info:
        await transport.create_child(client, state, agent_id="no-such-agent")
    assert exc_info.value.status_code == 400
    assert exc_info.value.error_type == "unknown_worker_agent"
    client.create_session.assert_not_awaited()


def test_projection_authoritative_child_serve_status_overrides_completed_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #12: wrapper+child rows read completed, but the child serve run's
    authoritative status.json says running — projection must say running."""
    from scillm.proxy.opencode_transport import ChildAttempt, TransportState, project_transport_state

    serve_root = tmp_path / "serve"
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(serve_root))
    child_run = serve_root / "oc-live-child"
    child_run.mkdir(parents=True)
    (child_run / "status.json").write_text(
        json.dumps({"run_id": "oc-live-child", "state": "running", "phase": "prompting"}),
        encoding="utf-8",
    )
    child = ChildAttempt(
        subagent_run_id="oc-live-child",
        role="patch",
        child_session_id="ses-child",
        agent="build",
        attempt_id=1,
        delivery_state="completed",  # transport-local row is stale
        active=True,
    )
    state = TransportState(
        transport_run_id="otr-stale-completed",
        active_subagent_run_id=child.subagent_run_id,
        children=[child.to_dict()],
    ).to_dict()
    state["state"] = "completed"
    state["phase"] = "done"

    projection = project_transport_state(state)
    assert projection["state"] == "running"
    assert projection["phase"] == "active_child:prompting"
    assert projection["wrapper_state"] == "completed"
    assert projection["active_child_serve_state"] == "running"


def test_projection_respects_genuinely_terminal_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scillm.proxy.opencode_transport import ChildAttempt, TransportState, project_transport_state

    serve_root = tmp_path / "serve"
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(serve_root))
    child_run = serve_root / "oc-done-child"
    child_run.mkdir(parents=True)
    (child_run / "status.json").write_text(
        json.dumps({"run_id": "oc-done-child", "state": "completed", "phase": "done"}),
        encoding="utf-8",
    )
    child = ChildAttempt(
        subagent_run_id="oc-done-child",
        role="patch",
        child_session_id="ses-child",
        agent="build",
        attempt_id=1,
        delivery_state="completed",
        active=True,
    )
    state = TransportState(
        transport_run_id="otr-genuine-complete",
        active_subagent_run_id=child.subagent_run_id,
        children=[child.to_dict()],
    ).to_dict()
    state["state"] = "completed"
    state["phase"] = "done"

    projection = project_transport_state(state)
    assert projection["state"] == "completed"
    assert projection["phase"] == "done"
