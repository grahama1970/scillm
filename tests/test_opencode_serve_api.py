from __future__ import annotations

import asyncio
import httpx
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scillm.proxy.opencode_serve import extract_assistant_text, text_parts
from scillm.proxy.opencode_skill_view import SkillViewReceipt
from scillm.proxy.errors import ProxyError, proxy_error_handler
from scillm.proxy.opencode_serve_api import (
    OpenCodeRunRequest,
    OpenCodeServeRun,
    build_human_monitor,
    create_opencode_serve_router,
)


def test_extract_assistant_text_from_parts() -> None:
    payload = {
        "info": {"role": "assistant"},
        "parts": [{"type": "text", "text": "Breakpoint at line 42."}],
    }
    assert extract_assistant_text(payload) == "Breakpoint at line 42."


def test_build_human_monitor_separates_workspace_ui_from_session_api(tmp_path: Path) -> None:
    class _Settings:
        base_url = "http://127.0.0.1:4098"

    workspace = "/home/graham/workspace/project"
    run = OpenCodeServeRun(
        run_id="oc-test",
        artifact_root=tmp_path,
        caller_skill="pdf-lab",
        agent="build",
        session_id="ses-test",
        request_payload={},
        directory=workspace,
    )

    monitor = build_human_monitor(
        run=run,
        scillm_base_url="http://localhost:4001",
        opencode_settings=_Settings(),
    )

    assert monitor["schema"] == "scillm.opencode_run.human_monitor.v1"
    assert monitor["opencode_workspace_url"].startswith("http://127.0.0.1:4098/")
    assert monitor["opencode_session_api_url"] == "http://127.0.0.1:4098/session/ses-test"
    assert monitor["opencode_messages_api_url"] == "http://127.0.0.1:4098/session/ses-test/message"
    assert monitor["scillm_events_url"] == "http://localhost:4001/v1/scillm/opencode/runs/oc-test/events?tail=200"
    assert "opencode_session_url" not in monitor
    assert "opencode_browser_session_url" not in monitor
    assert "scillm_chat_monitor_url" in monitor["human_instruction"]
    assert monitor.get("human_monitor_url") == monitor["scillm_chat_monitor_url"]
    assert "/monitor?token=" in monitor["scillm_chat_monitor_url"]
    assert "monitor_token" in monitor


def test_human_monitor_written_before_prompt_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """status.json must carry human_monitor after session create, before run returns."""
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))

    app = FastAPI()
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    workspace = str(tmp_path / "isolated")
    (tmp_path / "isolated").mkdir()
    session = {"id": "sess-early", "directory": workspace}
    message = {
        "info": {"role": "assistant", "id": "msg-1"},
        "parts": [{"type": "text", "text": "done"}],
    }

    async def slow_send_message(*_args: object, **_kwargs: object) -> dict[str, object]:
        status_paths = list(tmp_path.glob("*/status.json"))
        assert status_paths, "status.json should exist before prompt completes"
        status = json.loads(status_paths[0].read_text(encoding="utf-8"))
        monitor = status.get("human_monitor")
        assert isinstance(monitor, dict)
        workspace_url = str(monitor.get("opencode_workspace_url") or "")
        assert workspace_url.startswith("http://")
        assert "/session/" not in workspace_url
        assert "/monitor?token=" in monitor.get("scillm_chat_monitor_url", "")
        assert monitor.get("opencode_session_api_url", "").endswith("/session/sess-early")
        assert "opencode_session_url" not in monitor
        assert "opencode_browser_session_url" not in monitor
        return message

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.list_agents = AsyncMock(return_value=[{"name": "build"}])
    mock_client.create_session = AsyncMock(return_value=session)
    mock_client.send_message = AsyncMock(side_effect=slow_send_message)
    mock_client.list_messages = AsyncMock(return_value=[message])
    mock_client.session_status_map = AsyncMock(return_value={"sess-early": {"status": "idle"}})
    mock_client.diff = AsyncMock(return_value=[])
    mock_client.kill_session = AsyncMock(return_value={"session_id": "sess-early", "aborted": True, "deleted": True})

    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        resp = client.post(
            "/v1/scillm/opencode/runs",
            headers={"X-Caller-Skill": "test-opencode-serve"},
            json={"prompt": "hi", "agent": "build", "cwd": workspace},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["human_monitor"]["opencode_workspace_url"]




def test_opencode_chat_monitor_html_renders_messages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))

    run = OpenCodeServeRun(
        run_id="oc-monitor",
        artifact_root=tmp_path,
        caller_skill="test",
        agent="build",
        session_id="sess-mon",
        request_payload={"title": "scillm build oc-monitor"},
        directory=str(tmp_path / "ws"),
    )
    (tmp_path / "ws").mkdir()
    run.human_monitor = build_human_monitor(
        run=run,
        scillm_base_url="http://localhost:4001",
        opencode_settings=type("S", (), {"base_url": "http://127.0.0.1:4098"})(),
        session_title="scillm build oc-monitor",
    )
    run.write_status(state="running", phase="prompting", human_monitor=run.human_monitor)
    token = run.human_monitor["monitor_token"]

    app = FastAPI()
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    messages = [
        {
            "info": {"role": "assistant", "agent": "build"},
            "parts": [{"type": "text", "text": "Reading block_classifier.rs"}],
        }
    ]

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.list_messages = AsyncMock(return_value=messages)

    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        resp = client.get(f"/v1/scillm/opencode/runs/oc-monitor/monitor?token={token}")

    assert resp.status_code == 200, resp.text
    assert "text/html" in resp.headers.get("content-type", "")
    assert "block_classifier.rs" in resp.text
    assert "scillm build oc-monitor" in resp.text


def test_opencode_debugger_run_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))

    app = FastAPI()
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = {"id": "sess-abc", "directory": str(workspace)}
    message = {
        "info": {"role": "assistant", "id": "msg-1"},
        "parts": [{"type": "text", "text": "Likely null deref in handler."}],
    }

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.create_session.return_value = session
    mock_client.send_message.return_value = message
    mock_client.list_messages.return_value = [message]
    mock_client.session_status_map.return_value = {"sess-abc": {"status": "idle"}}
    mock_client.diff.return_value = []
    mock_client.kill_session.return_value = {"session_id": "sess-abc", "aborted": True, "deleted": True}

    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        resp = client.post(
            "/v1/scillm/opencode/serve/debugger/run",
            headers={"X-Caller-Skill": "test-opencode-serve"},
            json={"prompt": "Debug this pytest failure", "agent": "scillm-debugger", "cwd": str(workspace)},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schema"] == "scillm.opencode_run.result.v1"
    assert body["agent"] == "scillm-debugger"
    assert body["session_id"] == "sess-abc"
    assert body["human_monitor"]["session_id"] == "sess-abc"
    assert body["human_monitor"]["opencode_session_api_url"].endswith("/session/sess-abc")
    assert body.get("human_monitor_url") == body["human_monitor"]["scillm_chat_monitor_url"]
    assert "opencode_session_url" not in body["human_monitor"]
    assert "opencode_browser_session_url" not in body["human_monitor"]
    assert "null deref" in body["assistant_text"]
    assert body["collaboration_item"] == {
        "schema": "scillm.collaboration_item.v1",
        "thread_type": "subagent",
        "icon": "debugger",
        "person_or_persona_name": "scillm-debugger",
        "model": "",
        "response": "Likely null deref in handler.",
        "status": "completed",
    }
    assert (tmp_path / body["run_id"] / "status.json").exists()
    assert (tmp_path / body["run_id"] / "events.jsonl").exists()



def test_timeout_run_preserves_reasoning_excerpt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scillm.proxy.opencode_serve_api import _timeout_run_result, OpenCodeRunRequest

    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    run = OpenCodeServeRun(
        run_id="oc-timeout",
        artifact_root=tmp_path,
        caller_skill="pdf-lab",
        agent="build",
        session_id="sess-timeout",
        request_payload={"patch_mode": "live", "prompt": "patch delegate"},
        directory=str(tmp_path / "ws"),
    )
    (tmp_path / "ws").mkdir()
    run.human_monitor = build_human_monitor(
        run=run,
        scillm_base_url="http://localhost:4001",
        opencode_settings=type("S", (), {"base_url": "http://127.0.0.1:4098"})(),
        session_title="scillm build oc-timeout",
        session_slug="hidden-star",
    )
    spec = OpenCodeRunRequest(prompt="PATCH delegate", agent="build", patch_mode="live")
    messages = [
        {"info": {"role": "user", "id": "u1"}, "parts": [{"type": "text", "text": "fix it"}]},
        {
            "info": {"role": "assistant", "id": "a1", "completed": 1, "finish": None},
            "parts": [
                {"type": "reasoning", "text": "Plan patch for block_classifier.rs"},
                {"type": "tool", "tool": "read", "state": "error", "error": "permission denied"},
            ],
        },
    ]

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.list_messages = AsyncMock(return_value=messages)
    mock_client.diff = AsyncMock(return_value=[])
    mock_client.abort = AsyncMock(return_value={})
    mock_client.session_status_map = AsyncMock(return_value={"sess-timeout": {"status": "busy"}})

    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        result = asyncio.run(
            _timeout_run_result(run, spec, receipt=SkillViewReceipt((), (), (), None), timeout_s=300.0)
        )

    assert result["status"] == "timeout"
    assert "block_classifier" in result["assistant_text"]
    assert result["timeout_summary"]["message_count"] == 2
    assert result["terminal_blocker"]["primary_reason"] == "tool_error"
    assert result["terminal_blocker"]["last_tool_errors"]
    assert result["patch_delegate_status"] == "PATCH_DELEGATE_BLOCKED"
    status = json.loads((tmp_path / "oc-timeout" / "status.json").read_text(encoding="utf-8"))
    assert status["terminal_blocker"]["primary_reason"] == "tool_error"



def test_opencode_run_requires_caller_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")
    client = TestClient(app)
    resp = client.post(
        "/v1/scillm/opencode/runs",
        json={"prompt": "hi", "agent": "scillm-debugger"},
    )
    assert resp.status_code == 400


def test_opencode_health_is_compact_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    class _Settings:
        base_url = "http://127.0.0.1:4098"

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.health.return_value = {"healthy": True, "version": "1.14.31"}

    with patch("scillm.proxy.opencode_serve_api.load_opencode_serve_settings", return_value=_Settings()), patch(
        "scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client
    ):
        client = TestClient(app)
        resp = client.get(
            "/v1/scillm/opencode/health",
            headers={"X-Caller-Skill": "test-opencode-serve"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schema"] == "scillm.opencode_health.v1"
    assert body["full"] is False
    assert body["agents"] == [
        "build",
        "compaction",
        "explore",
        "general",
        "plan",
        "summary",
        "title",
    ]
    assert body["agent_count"] == 7
    assert body["agent_catalog_source"] == "static_default"
    assert "agents_full" not in body
    mock_client.health.assert_awaited_once()
    mock_client.list_agents.assert_not_awaited()


def test_opencode_health_full_includes_agent_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    class _Settings:
        base_url = "http://127.0.0.1:4098"

    agents = [{"name": "build", "permission": {"edit": "allow"}}]
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.health.return_value = {"healthy": True, "version": "1.14.31"}
    mock_client.list_agents.return_value = agents

    with patch("scillm.proxy.opencode_serve_api.load_opencode_serve_settings", return_value=_Settings()), patch(
        "scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client
    ):
        client = TestClient(app)
        resp = client.get(
            "/v1/scillm/opencode/health?full=true",
            headers={"X-Caller-Skill": "test-opencode-serve"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["full"] is True
    assert body["agents"] == ["build"]
    assert body["agent_catalog_source"] == "opencode_agent_endpoint"
    assert body["agents_full"] == agents
    mock_client.health.assert_awaited_once()
    mock_client.list_agents.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_result_snapshots_messages_before_abort(tmp_path: Path) -> None:
    from scillm.proxy import opencode_serve_api as api_mod
    from scillm.proxy.opencode_serve_api import OpenCodeServeRun

    run = OpenCodeServeRun(
        run_id="oc-timeout-snapshot",
        artifact_root=tmp_path,
        caller_skill="pdf-lab",
        agent="build",
        session_id="sess-timeout",
        request_payload={"prompt": "patch"},
        directory="/workspace/case",
    )
    spec = OpenCodeRunRequest(
        prompt="patch",
        agent="build",
        scillm_metadata={"batch_id": "batch-1", "item_id": "page_case_0001_p0001"},
    )
    receipt = api_mod.SkillViewReceipt(
        skills_requested=tuple(),
        skills_materialized=tuple(),
        skills_missing=tuple(),
        skill_view_dir=None,
    )
    message = {
        "info": {"role": "assistant", "id": "msg-1"},
        "parts": [{"type": "text", "text": "Patched normalize_status."}],
    }

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.list_messages.return_value = [message]
    mock_client.diff.return_value = [{"path": "src/pdf_lab_canary.py"}]
    mock_client.abort.return_value = True
    mock_client.session_status_map.return_value = {}

    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        result = await api_mod._timeout_run_result(run, spec, receipt=receipt, timeout_s=10)

    assert result["status"] == "timeout"
    assert result["assistant_text"] == "Patched normalize_status."
    assert result["collaboration_item"]["thread_type"] == "subagent"
    assert result["collaboration_item"]["person_or_persona_name"] == "build"
    assert result["collaboration_item"]["response"] == "Patched normalize_status."
    assert result["message"] == message
    assert result["messages_snapshot_count"] == 1
    assert result["diff"] == [{"path": "src/pdf_lab_canary.py"}]
    assert (run.run_dir / "messages_snapshot.json").exists()
    mock_client.list_messages.assert_awaited_once_with("sess-timeout", limit=50, directory="/workspace/case")
    mock_client.diff.assert_awaited_once_with("sess-timeout", directory="/workspace/case")
    mock_client.abort.assert_awaited_once_with("sess-timeout", directory="/workspace/case")


def test_artifact_run_messages_use_snapshot_and_do_not_overwrite_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))

    run_dir = tmp_path / "oc-existing"
    run_dir.mkdir()
    request_payload = {"prompt": "original prompt"}
    (run_dir / "request.json").write_text(json.dumps(request_payload), encoding="utf-8")
    (run_dir / "status.json").write_text(
        json.dumps(
            {
                "schema": "scillm.opencode_run.status.v1",
                "run_id": "oc-existing",
                "agent": "build",
                "session_id": "sess-existing",
                "caller_skill": "pdf-lab",
                "directory": "/workspace/case",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "messages_snapshot.json").write_text(
        json.dumps(
            {
                "schema": "scillm.opencode_run.messages_snapshot.v1",
                "run_id": "oc-existing",
                "session_id": "sess-existing",
                "directory": "/workspace/case",
                "messages": [{"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "saved"}]}],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "opencode_result.json").write_text(
        json.dumps({"schema": "scillm.opencode_run.result.v1", "diff": [{"path": "src/foo.py"}]}),
        encoding="utf-8",
    )

    app = FastAPI()
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")
    client = TestClient(app)

    messages = client.get(
        "/v1/scillm/opencode/runs/oc-existing/messages",
        headers={"X-Caller-Skill": "pdf-lab"},
    )
    assert messages.status_code == 200, messages.text
    assert messages.json()["source"] == "artifact_snapshot"
    assert messages.json()["messages"][0]["parts"][0]["text"] == "saved"
    assert json.loads((run_dir / "request.json").read_text(encoding="utf-8")) == request_payload

    diff = client.get(
        "/v1/scillm/opencode/runs/oc-existing/diff",
        headers={"X-Caller-Skill": "pdf-lab"},
    )
    assert diff.status_code == 200, diff.text
    assert diff.json()["source"] == "artifact_result"
    assert diff.json()["diff"] == [{"path": "src/foo.py"}]


def test_artifact_run_messages_fall_back_to_collaboration_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    run_dir = tmp_path / "oc-collab"
    run_dir.mkdir()
    (run_dir / "status.json").write_text(
        json.dumps(
            {
                "schema": "scillm.opencode_run.status.v1",
                "run_id": "oc-collab",
                "agent": "build",
                "session_id": "sess-collab",
                "caller_skill": "pdf-lab",
                "directory": "/workspace/case",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "opencode_result.json").write_text(
        json.dumps(
            {
                "schema": "scillm.opencode_run.result.v1",
                "collaboration_item": {
                    "schema": "scillm.collaboration_item.v1",
                    "thread_type": "subagent",
                    "icon": "agent",
                    "person_or_persona_name": "build",
                    "model": "opencode-go/kimi-k2.6",
                    "response": "Patch evidence response.",
                    "status": "completed",
                },
            }
        ),
        encoding="utf-8",
    )

    app = FastAPI()
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")
    client = TestClient(app)
    messages = client.get(
        "/v1/scillm/opencode/runs/oc-collab/messages",
        headers={"X-Caller-Skill": "pdf-lab"},
    )
    assert messages.status_code == 200, messages.text
    body = messages.json()
    assert body["source"] == "artifact_collaboration_item"
    assert body["messages"][0]["info"]["thread_type"] == "subagent"
    assert body["messages"][0]["parts"][0]["text"] == "Patch evidence response."


def test_text_parts_helper() -> None:
    assert text_parts("hello") == [{"type": "text", "text": "hello"}]


def test_run_request_validation() -> None:
    spec = OpenCodeRunRequest(prompt="x", parts=text_parts("multipart"))
    assert spec.parts is not None
    assert spec.wait is True


def test_opencode_list_sessions_and_purge_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))

    app = FastAPI()
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    sessions = [
        {"id": "sess-busy", "title": "stuck", "updated": "2020-01-01T00:00:00Z"},
        {"id": "sess-fresh", "title": "ok", "updated": "2099-01-01T00:00:00Z"},
    ]
    status_map = {
        "sess-busy": {"status": "busy"},
        "sess-fresh": {"status": "idle"},
    }

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.list_sessions.return_value = sessions
    mock_client.session_status_map.return_value = status_map
    mock_client.kill_session.return_value = {"session_id": "sess-busy", "aborted": True, "deleted": True}

    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        listed = client.get(
            "/v1/scillm/opencode/sessions",
            headers={"X-Caller-Skill": "test-opencode-serve"},
        )
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["zombie_count"] == 1
        assert body["zombies"][0]["session_id"] == "sess-busy"

        purged = client.post(
            "/v1/scillm/opencode/sessions/purge",
            headers={"X-Caller-Skill": "test-opencode-serve"},
            json={"dry_run": True, "stale_busy_s": 60},
        )
        assert purged.status_code == 200, purged.text
        purge_body = purged.json()
        assert purge_body["dry_run"] is True
        assert purge_body["target_count"] == 1
        assert purge_body["killed"][0]["session_id"] == "sess-busy"
        mock_client.kill_session.assert_not_called()

        purged_live = client.post(
            "/v1/scillm/opencode/sessions/purge",
            headers={"X-Caller-Skill": "test-opencode-serve"},
            json={"dry_run": False, "stale_busy_s": 60},
        )
        assert purged_live.status_code == 200
        mock_client.kill_session.assert_called_once_with("sess-busy")


def test_opencode_kill_session_protects_active_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))

    from scillm.proxy import opencode_serve_api as api_mod

    run = api_mod.OpenCodeServeRun(
        run_id="oc-run1",
        artifact_root=tmp_path,
        caller_skill="test",
        agent="build",
        session_id="sess-live",
        request_payload={},
    )
    api_mod._ACTIVE_RUNS["oc-run1"] = run

    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    try:
        with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
            client = TestClient(app)
            resp = client.post(
                "/v1/scillm/opencode/sessions/sess-live/kill",
                headers={"X-Caller-Skill": "test-opencode-serve"},
            )
            assert resp.status_code == 409
            mock_client.kill_session.assert_not_called()
    finally:
        api_mod._ACTIVE_RUNS.pop("oc-run1", None)


def test_mount_guard_opencode_go_does_not_block_serve() -> None:
    """Regression: /v1/scillm/opencode-go/* must not prevent serve router mount."""
    from scillm.proxy import app_with_exec

    prefix = app_with_exec._OPENCODE_SERVE_ROUTE_PREFIX
    assert prefix.endswith("/")
    assert not "/v1/scillm/opencode-go/models".startswith(prefix)
    assert "/v1/scillm/opencode/health".startswith(prefix)

    paths = [str(getattr(route, "path", "")) for route in app_with_exec.app.router.routes]
    assert any(p.startswith("/v1/scillm/opencode/health") for p in paths)


def test_opencode_run_forks_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))

    app = FastAPI()
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    parent_session = {"id": "sess-parent"}
    child_session = {"id": "sess-child"}
    message = {
        "info": {"role": "assistant", "id": "msg-1"},
        "parts": [{"type": "text", "text": "Retry succeeded."}],
    }

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.fork_session.return_value = {**child_session, "directory": str(tmp_path)}
    mock_client.get_session.return_value = {"id": "sess-parent", "directory": str(tmp_path)}
    mock_client.send_message.return_value = message
    mock_client.list_messages.return_value = [message]
    mock_client.session_status_map.return_value = {"sess-child": {"status": "idle"}}
    mock_client.diff.return_value = []
    mock_client.kill_session.return_value = {"session_id": "sess-child", "aborted": True, "deleted": True}

    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        resp = client.post(
            "/v1/scillm/opencode/runs",
            headers={"X-Caller-Skill": "test-opencode-serve"},
            json={
                "prompt": "Try again with a smaller fix",
                "agent": "build",
                "fork_from_session_id": "sess-parent",
                "fork_at_message_id": "msg-before-bad-edit",
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"] == "sess-child"
    assert body["session_lineage"]["parent_session_id"] == "sess-parent"
    assert body["session_lineage"]["fork_at_message_id"] == "msg-before-bad-edit"
    mock_client.fork_session.assert_awaited_once_with(
        "sess-parent",
        message_id="msg-before-bad-edit",
        directory=str(tmp_path),
    )
    mock_client.create_session.assert_not_awaited()


def test_opencode_fork_session_endpoint() -> None:
    app = FastAPI()
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.fork_session.return_value = {"id": "sess-child-2"}

    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        resp = client.post(
            "/v1/scillm/opencode/sessions/sess-parent/fork",
            headers={"X-Caller-Skill": "test-opencode-serve"},
            json={"message_id": "msg-9"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schema"] == "scillm.opencode_session_fork.v1"
    assert body["parent_session_id"] == "sess-parent"
    assert body["child_session_id"] == "sess-child-2"
    mock_client.fork_session.assert_awaited_once_with("sess-parent", message_id="msg-9")


def test_opencode_session_children_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    app = FastAPI()
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    children = [{"id": "child-1", "parentID": "parent-1"}]
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.list_session_children.return_value = children

    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        resp = client.get(
            "/v1/scillm/opencode/sessions/parent-1/children",
            headers={"X-Caller-Skill": "test-opencode-serve"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schema"] == "scillm.opencode_session_children.v1"
    assert body["count"] == 1
    assert body["children"][0]["id"] == "child-1"


def test_opencode_summarize_and_revert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    app = FastAPI()
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.summarize.return_value = True
    mock_client.revert.return_value = True
    mock_client.unrevert.return_value = True

    headers = {"X-Caller-Skill": "test-opencode-serve"}
    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        summarized = client.post(
            "/v1/scillm/opencode/sessions/sess-1/summarize",
            headers=headers,
            json={"provider_id": "openai", "model_id": "gpt-4"},
        )
        assert summarized.status_code == 200, summarized.text
        assert summarized.json()["ok"] is True
        mock_client.summarize.assert_called_once_with("sess-1", provider_id="openai", model_id="gpt-4")

        reverted = client.post(
            "/v1/scillm/opencode/sessions/sess-1/revert",
            headers=headers,
            json={"message_id": "msg-9", "part_id": "part-1"},
        )
        assert reverted.status_code == 200, reverted.text
        mock_client.revert.assert_called_once_with("sess-1", message_id="msg-9", part_id="part-1")

        unreverted = client.post(
            "/v1/scillm/opencode/sessions/sess-1/unrevert",
            headers=headers,
        )
        assert unreverted.status_code == 200, unreverted.text
        mock_client.unrevert.assert_called_once_with("sess-1")


async def _fake_event_stream():
    yield b"event: server.connected\ndata: {}\n\n"
    yield b"data: {\"type\":\"session.updated\"}\n\n"


def test_opencode_events_sse_proxy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    app = FastAPI()
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    async def iter_event_stream():
        async for chunk in _fake_event_stream():
            yield chunk

    mock_client.iter_event_stream = iter_event_stream

    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        with client.stream(
            "GET",
            "/v1/scillm/opencode/events",
            headers={"X-Caller-Skill": "test-opencode-serve"},
        ) as resp:
            assert resp.status_code == 200
            body = b"".join(resp.iter_bytes())
    assert b"server.connected" in body

def test_opencode_events_returns_404_when_unmounted() -> None:
    """Sanity contract: missing mount must 404, not fall through to catch-all chat."""
    app = FastAPI()
    client = TestClient(app)
    resp = client.get(
        "/v1/scillm/opencode/events",
        headers={"X-Caller-Skill": "test-opencode-serve"},
    )
    assert resp.status_code == 404



def test_opencode_create_session_passes_directory_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    cwd = str(tmp_path)

    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.list_agents = AsyncMock(return_value=[{"name": "build"}])
    message = {
        "info": {"role": "assistant", "id": "msg-1"},
        "parts": [{"type": "text", "text": "ok"}],
    }
    mock_client.create_session = AsyncMock(return_value={"id": "sess-cwd", "directory": cwd})
    mock_client.send_prompt_async = AsyncMock()
    mock_client.list_messages = AsyncMock(return_value=[message])
    mock_client.session_status_map = AsyncMock(return_value={"sess-cwd": {"status": "idle"}})
    mock_client.diff = AsyncMock(return_value=[])
    mock_client.kill_session = AsyncMock(return_value={"session_id": "sess-cwd", "aborted": True, "deleted": True})

    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        resp = client.post(
            "/v1/scillm/opencode/runs",
            headers={"X-Caller-Skill": "test-opencode-serve"},
            json={
                "prompt": "read AGENTS.md",
                "agent": "build",
                "cwd": cwd,
                "wait": False,
                "timeout_s": 30,
            },
        )
    assert resp.status_code == 200
    assert resp.json()["schema"] == "scillm.opencode_run.receipt.v1"
    mock_client.create_session.assert_awaited()
    kwargs = mock_client.create_session.await_args.kwargs
    assert kwargs.get("directory") == cwd


def test_wait_false_returns_receipt_before_prompt_delivery_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))

    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    async def _blocked_prompt(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(20)

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.list_agents = AsyncMock(return_value=[{"name": "build"}])
    mock_client.create_session = AsyncMock(return_value={"id": "sess-receipt", "directory": str(tmp_path)})
    mock_client.send_prompt_async = AsyncMock(side_effect=_blocked_prompt)
    mock_client.list_messages = AsyncMock(return_value=[])
    mock_client.session_status_map = AsyncMock(return_value={})
    mock_client.diff = AsyncMock(return_value=[])
    mock_client.abort = AsyncMock(return_value=True)
    mock_client.kill_session = AsyncMock(return_value={"session_id": "sess-receipt", "aborted": True, "deleted": True})

    started = time.monotonic()
    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        resp = client.post(
            "/v1/scillm/opencode/runs",
            headers={"X-Caller-Skill": "test-opencode-serve"},
            json={
                "prompt": "slow prompt delivery",
                "agent": "build",
                "cwd": str(tmp_path),
                "wait": False,
                "timeout_s": 10,
            },
        )
    elapsed = time.monotonic() - started

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schema"] == "scillm.opencode_run.receipt.v1"
    assert body["status"] == "running"
    assert body["session_id"] == "sess-receipt"
    assert body["human_monitor"]["session_id"] == "sess-receipt"
    assert elapsed < 2.0
    status = json.loads((tmp_path / body["run_id"] / "status.json").read_text(encoding="utf-8"))
    assert status["run_id"] == body["run_id"]
    assert status["session_id"] == "sess-receipt"


@pytest.mark.asyncio
async def test_execute_run_emits_prompt_delivery_and_first_delta_events(tmp_path: Path) -> None:
    from scillm.proxy import opencode_serve_api as api_mod

    run = OpenCodeServeRun(
        run_id="oc-events",
        artifact_root=tmp_path,
        caller_skill="pdf-lab",
        agent="build",
        session_id="sess-events",
        request_payload={"prompt": "patch"},
        directory=str(tmp_path),
    )
    spec = OpenCodeRunRequest(prompt="patch", agent="build", wait=False, timeout_s=10)
    receipt = SkillViewReceipt((), (), (), None)
    message = {
        "info": {"role": "assistant", "id": "msg-1"},
        "parts": [{"type": "text", "text": "I can inspect this."}],
    }

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.send_prompt_async = AsyncMock(return_value=None)
    mock_client.list_messages = AsyncMock(return_value=[message])
    mock_client.session_status_map = AsyncMock(return_value={"sess-events": {"status": "idle"}})
    mock_client.diff = AsyncMock(return_value=[])

    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        result = await api_mod._execute_run(run, spec, skill_receipt=receipt)

    assert result["status"] == "completed"
    events = [
        json.loads(line)["event"]
        for line in run.events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert "prompt_delivery_started" in events
    assert "prompt_submitted_to_opencode" in events
    assert "first_assistant_or_tool_delta" in events


@pytest.mark.asyncio
async def test_execute_run_does_not_complete_while_tool_call_pending(tmp_path: Path) -> None:
    from scillm.proxy import opencode_serve_api as api_mod

    run = OpenCodeServeRun(
        run_id="oc-pending-tool",
        artifact_root=tmp_path,
        caller_skill="pdf-lab",
        agent="build",
        session_id="sess-pending-tool",
        request_payload={"prompt": "patch"},
        directory=str(tmp_path),
    )
    spec = OpenCodeRunRequest(prompt="patch", agent="build", wait=False, timeout_s=10)
    receipt = SkillViewReceipt((), (), (), None)
    pending_message = {
        "info": {"role": "assistant", "id": "msg-1"},
        "parts": [
            {"type": "reasoning", "text": "I will inspect the file first."},
            {
                "type": "tool",
                "tool": "read",
                "state": {"status": "pending", "input": {}, "raw": ""},
            },
        ],
    }
    completed_message = {
        "info": {"role": "assistant", "id": "msg-1"},
        "parts": [
            {"type": "reasoning", "text": "I inspected the file and found the issue."},
            {
                "type": "tool",
                "tool": "read",
                "state": {"status": "completed", "input": {"filePath": "src/foo.py"}, "raw": ""},
            },
            {"type": "text", "text": "PATCH_DELEGATE_BLOCKED reason=no writable target in fixture"},
        ],
    }

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.send_prompt_async = AsyncMock(return_value=None)
    mock_client.list_messages = AsyncMock(side_effect=[[pending_message], [pending_message], [completed_message]])
    mock_client.session_status_map = AsyncMock(return_value={"sess-pending-tool": {"status": "idle"}})
    mock_client.diff = AsyncMock(return_value=[])

    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        result = await api_mod._execute_run(run, spec, skill_receipt=receipt)

    assert result["status"] == "completed"
    assert "PATCH_DELEGATE_BLOCKED" in result["assistant_text"]
    assert mock_client.list_messages.await_count >= 3
    events = [
        json.loads(line)["event"]
        for line in run.events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert "first_assistant_or_tool_delta" in events
    assert "assistant_tool_pending" in events
    assert events.index("assistant_tool_pending") < events.index("run_completed")


@pytest.mark.asyncio
async def test_execute_run_blocks_pending_tool_outside_run_directory(tmp_path: Path) -> None:
    from scillm.proxy import opencode_serve_api as api_mod

    isolated = tmp_path / "pdf_oxide" / "artifacts" / "pdf_lab" / "case" / "clean_head_worktree"
    isolated.mkdir(parents=True)
    parent_checkout = tmp_path / "pdf_oxide"
    run = OpenCodeServeRun(
        run_id="oc-scope-violation",
        artifact_root=tmp_path / "runs",
        caller_skill="pdf-lab",
        agent="build",
        session_id="sess-scope-violation",
        request_payload={"prompt": "patch"},
        directory=str(isolated),
    )
    spec = OpenCodeRunRequest(prompt="patch", agent="build", wait=False, timeout_s=10)
    receipt = SkillViewReceipt((), (), (), None)
    pending_message = {
        "info": {"role": "assistant", "id": "msg-1"},
        "parts": [
            {"type": "reasoning", "text": "I will search for list code."},
            {
                "type": "tool",
                "tool": "glob",
                "state": {
                    "status": "running",
                    "input": {
                        "pattern": "python/**/*list*",
                        "path": str(parent_checkout),
                    },
                },
            },
        ],
    }

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.send_prompt_async = AsyncMock(return_value=None)
    mock_client.list_messages = AsyncMock(return_value=[pending_message])
    mock_client.session_status_map = AsyncMock(return_value={"sess-scope-violation": {"status": "busy"}})
    mock_client.diff = AsyncMock(return_value=[])
    mock_client.abort = AsyncMock(return_value=True)

    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        result = await api_mod._execute_run(run, spec, skill_receipt=receipt)

    assert result["status"] == "timeout"
    blocker = result["terminal_blocker"]
    assert blocker["primary_reason"] == "tool_scope_violation"
    assert blocker["last_tool_scope_violation_count"] == 1
    violation = blocker["last_tool_scope_violations"][0]["scope_violation"]
    assert violation["input_key"] == "path"
    assert violation["resolved_path"] == str(parent_checkout.resolve())
    assert violation["allowed_root"] == str(isolated.resolve())
    assert result["diff_evidence"]["diff_count"] == 0
    mock_client.abort.assert_awaited_once_with("sess-scope-violation", directory=str(isolated))
    events = [
        json.loads(line)["event"]
        for line in run.events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert "assistant_tool_scope_violation" in events
    assert "run_timeout" in events
    assert "run_completed" not in events


@pytest.mark.asyncio
async def test_execute_run_times_out_when_tool_completes_without_terminal_text(tmp_path: Path) -> None:
    from scillm.proxy import opencode_serve_api as api_mod

    run = OpenCodeServeRun(
        run_id="oc-completed-tool-no-text",
        artifact_root=tmp_path,
        caller_skill="pdf-lab",
        agent="build",
        session_id="sess-completed-tool-no-text",
        request_payload={"prompt": "patch"},
        directory=str(tmp_path),
    )
    spec = OpenCodeRunRequest(prompt="patch", agent="build", wait=False, timeout_s=10)
    receipt = SkillViewReceipt((), (), (), None)
    completed_tool_message = {
        "info": {"role": "assistant", "id": "msg-1"},
        "parts": [
            {"type": "reasoning", "text": "I ran the requested command."},
            {
                "type": "tool",
                "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {"command": "sleep 8 && echo ISSUE9_SLEEP_DONE"},
                    "output": "ISSUE9_SLEEP_DONE\n",
                },
            },
        ],
    }

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.send_prompt_async = AsyncMock(return_value=None)
    mock_client.list_messages = AsyncMock(return_value=[completed_tool_message])
    mock_client.session_status_map = AsyncMock(return_value={"sess-completed-tool-no-text": {"status": "idle"}})
    mock_client.diff = AsyncMock(return_value=[])

    clock = {"value": 0.0}

    def _advance_clock() -> float:
        clock["value"] += 0.5
        return clock["value"]

    with (
        patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client),
        patch("scillm.proxy.opencode_serve_api.time.monotonic", side_effect=_advance_clock),
        patch("scillm.proxy.opencode_serve_api.asyncio.sleep", AsyncMock()),
    ):
        result = await api_mod._execute_run(run, spec, skill_receipt=receipt)

    assert result["status"] == "timeout"
    assert result["terminal_blocker"]["primary_reason"] == "tool_completed_without_terminal_text"
    events = [
        json.loads(line)["event"]
        for line in run.events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert "assistant_waiting_for_terminal_text" in events
    assert "run_completed" not in events


@pytest.mark.asyncio
async def test_execute_run_blocks_tool_call_finish_with_commentary_text(tmp_path: Path) -> None:
    from scillm.proxy import opencode_serve_api as api_mod

    run = OpenCodeServeRun(
        run_id="oc-tool-call-finish-commentary",
        artifact_root=tmp_path,
        caller_skill="pdf-lab",
        agent="build",
        session_id="sess-tool-call-finish-commentary",
        request_payload={"prompt": "patch"},
        directory=str(tmp_path),
    )
    spec = OpenCodeRunRequest(prompt="patch", agent="build", wait=False, timeout_s=10)
    receipt = SkillViewReceipt((), (), (), None)
    tool_call_message = {
        "info": {"role": "assistant", "id": "msg-tool-call", "finish": "tool-calls"},
        "parts": [
            {"type": "step-start"},
            {"type": "text", "text": "I will inspect the worktree first."},
            {
                "type": "tool",
                "tool": "todowrite",
                "state": {"status": "completed", "input": {"todos": []}, "output": "[]"},
            },
            {"type": "step-finish", "reason": "tool-calls"},
        ],
    }

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.send_prompt_async = AsyncMock(return_value=None)
    mock_client.list_messages = AsyncMock(return_value=[tool_call_message])
    mock_client.session_status_map = AsyncMock(return_value={"sess-tool-call-finish-commentary": {"status": "idle"}})
    mock_client.diff = AsyncMock(return_value=[])

    clock = {"value": 0.0}

    def _advance_clock() -> float:
        clock["value"] += 0.5
        return clock["value"]

    with (
        patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client),
        patch("scillm.proxy.opencode_serve_api.time.monotonic", side_effect=_advance_clock),
        patch("scillm.proxy.opencode_serve_api.asyncio.sleep", AsyncMock()),
    ):
        result = await api_mod._execute_run(run, spec, skill_receipt=receipt)

    assert result["status"] == "timeout"
    blocker = result["terminal_blocker"]
    assert blocker["primary_reason"] == "tool_call_turn_without_terminal_text"
    assert blocker["last_assistant_tool_call_finish"] is True
    assert blocker["last_assistant_terminal_text_chars"] > 0
    events = [
        json.loads(line)["event"]
        for line in run.events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert "assistant_waiting_for_terminal_text" in events
    assert "run_completed" not in events


@pytest.mark.asyncio
async def test_execute_run_rechecks_tool_call_finish_before_completion(tmp_path: Path) -> None:
    from scillm.proxy import opencode_serve_api as api_mod

    run = OpenCodeServeRun(
        run_id="oc-tool-call-finish-final-snapshot",
        artifact_root=tmp_path,
        caller_skill="pdf-lab",
        agent="build",
        session_id="sess-tool-call-finish-final-snapshot",
        request_payload={"prompt": "patch"},
        directory=str(tmp_path),
    )
    spec = OpenCodeRunRequest(prompt="patch", agent="build", wait=False, timeout_s=10)
    receipt = SkillViewReceipt((), (), (), None)
    first_delta_message = {
        "info": {"role": "assistant", "id": "msg-tool-call"},
        "parts": [
            {"type": "step-start"},
            {"type": "text", "text": "I will inspect the worktree first."},
        ],
    }
    final_tool_call_message = {
        "info": {"role": "assistant", "id": "msg-tool-call", "finish": "tool-calls"},
        "parts": [
            {"type": "step-start"},
            {"type": "text", "text": "I will inspect the worktree first."},
            {
                "type": "tool",
                "tool": "skill",
                "state": {"status": "completed", "input": {"name": "memory"}, "output": "skill content"},
            },
            {"type": "step-finish", "reason": "tool-calls"},
        ],
    }

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.send_prompt_async = AsyncMock(return_value=None)
    mock_client.list_messages = AsyncMock(
        side_effect=[
            [first_delta_message],
            [final_tool_call_message],
            [final_tool_call_message],
        ]
    )
    mock_client.session_status_map = AsyncMock(return_value={"sess-tool-call-finish-final-snapshot": {"status": "idle"}})
    mock_client.diff = AsyncMock(return_value=[])

    with (
        patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client),
        patch("scillm.proxy.opencode_serve_api.asyncio.sleep", AsyncMock()),
    ):
        result = await api_mod._execute_run(run, spec, skill_receipt=receipt)

    assert result["status"] == "timeout"
    blocker = result["terminal_blocker"]
    assert blocker["primary_reason"] == "tool_call_turn_without_terminal_text"
    assert blocker["last_assistant_tool_call_finish"] is True
    assert blocker["last_assistant_terminal_text_chars"] > 0
    events = [
        json.loads(line)["event"]
        for line in run.events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert "assistant_waiting_for_terminal_text" in events
    assert "run_completed" not in events


def test_opencode_run_timeout_returns_terminal_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))

    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.create_session = AsyncMock(return_value={"id": "sess-timeout", "directory": str(tmp_path)})
    mock_client.register_mcp = AsyncMock()
    async def _slow_send(*_a, **_k):
        await asyncio.sleep(20)

    mock_client.send_message = AsyncMock(side_effect=_slow_send)
    mock_client.send_prompt_async = AsyncMock(side_effect=_slow_send)
    mock_client.abort = AsyncMock(return_value=True)
    mock_client.session_status_map = AsyncMock(return_value={})
    mock_client.diff = AsyncMock(return_value=[])
    mock_client.list_messages = AsyncMock(return_value=[])
    mock_client.kill_session = AsyncMock(return_value={"session_id": "sess-timeout", "aborted": True, "deleted": True})

    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        resp = client.post(
            "/v1/scillm/opencode/runs",
            headers={"X-Caller-Skill": "test-opencode-serve"},
            json={
                "prompt": "slow",
                "agent": "build",
                "cwd": str(tmp_path),
                "timeout_s": 10,
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "timeout"
    mock_client.abort.assert_awaited()


def test_pdf_lab_patch_delegate_blocks_when_terminal_diff_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path / "artifacts"))
    cwd = tmp_path / "case"
    cwd.mkdir()

    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    message = {
        "info": {"role": "assistant", "id": "msg-1"},
        "parts": [{"type": "text", "text": "I applied the patch."}],
    }
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.list_agents = AsyncMock(return_value=[{"name": "build"}])
    mock_client.create_session = AsyncMock(return_value={"id": "sess-pdf", "directory": str(cwd)})
    mock_client.send_message = AsyncMock(return_value=message)
    mock_client.list_messages = AsyncMock(return_value=[message])
    mock_client.session_status_map = AsyncMock(return_value={"sess-pdf": {"status": "idle"}})
    mock_client.diff = AsyncMock(return_value=[])
    mock_client.kill_session = AsyncMock(return_value={"session_id": "sess-pdf", "aborted": True, "deleted": True})

    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        resp = client.post(
            "/v1/scillm/opencode/runs",
            headers={"X-Caller-Skill": "pdf-lab"},
            json={
                "prompt": "Run the bounded patch delegate.",
                "agent": "build",
                "cwd": str(cwd),
                "patch_mode": "live",
                "batch_id": "batch-1",
                "case_id": "page_case_0001_p0001",
                "page_number": 1,
                "candidate_id": "cand-1",
                "scillm_metadata": {
                    "batch_id": "batch-1",
                    "item_id": "page_case_0001_p0001",
                    "patch_mode": "live",
                },
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["patch_delegate_status"] == "PATCH_DELEGATE_BLOCKED"
    assert body["patch_delegate_reason"] == "no_patch_delta"
    assert body["patch_delegate"]["substrate_reason"] == "no_patch_delta"
    assert body["patch_delegate"]["diff_count"] == 0
    assert "PATCH_DELEGATE_BLOCKED" in body["project_agent_message"]
    status = json.loads((tmp_path / "artifacts" / body["run_id"] / "status.json").read_text(encoding="utf-8"))
    assert status["patch_delegate_status"] == "PATCH_DELEGATE_BLOCKED"
    assert status["patch_delegate_reason"] == "no_patch_delta"


def test_pdf_lab_patch_delegate_surfaces_concrete_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path / "artifacts"))
    cwd = tmp_path / "case"
    cwd.mkdir()

    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    message = {
        "info": {"role": "assistant", "id": "msg-1"},
        "parts": [{"type": "text", "text": "PATCH_DELEGATE_BLOCKED - permission denied writing src/calc.py"}],
    }
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.list_agents = AsyncMock(return_value=[{"name": "build"}])
    mock_client.create_session = AsyncMock(return_value={"id": "sess-pdf", "directory": str(cwd)})
    mock_client.send_message = AsyncMock(return_value=message)
    mock_client.list_messages = AsyncMock(return_value=[message])
    mock_client.session_status_map = AsyncMock(return_value={"sess-pdf": {"status": "idle"}})
    mock_client.diff = AsyncMock(return_value=[])
    mock_client.kill_session = AsyncMock(return_value={"session_id": "sess-pdf", "aborted": True, "deleted": True})

    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        resp = client.post(
            "/v1/scillm/opencode/runs",
            headers={"X-Caller-Skill": "pdf-lab"},
            json={
                "prompt": "Run the bounded patch delegate.",
                "agent": "build",
                "cwd": str(cwd),
                "patch_mode": "live",
                "batch_id": "batch-1",
                "case_id": "page_case_0001_p0001",
                "page_number": 1,
                "candidate_id": "cand-1",
                "scillm_metadata": {
                    "batch_id": "batch-1",
                    "item_id": "page_case_0001_p0001",
                    "patch_mode": "live",
                },
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["patch_delegate_status"] == "PATCH_DELEGATE_BLOCKED"
    assert body["patch_delegate_reason"] == "permission_denied"
    assert body["patch_delegate"]["substrate_reason"] == "permission_denied"
    assert body["patch_delegate"]["receipt_classifier"]["has_concrete_blocker"] is True
    assert "permission_denied" in body["project_agent_message"]


def test_pdf_lab_patch_delegate_applied_requires_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path / "artifacts"))
    cwd = tmp_path / "case"
    cwd.mkdir()

    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    message = {
        "info": {"role": "assistant", "id": "msg-1"},
        "parts": [{"type": "text", "text": "Patch applied and tests pass."}],
    }
    diff = [{"path": "tests/test_pdf_lab_canary.py", "status": "modified"}]
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.list_agents = AsyncMock(return_value=[{"name": "build"}])
    mock_client.create_session = AsyncMock(return_value={"id": "sess-pdf", "directory": str(cwd)})
    mock_client.send_message = AsyncMock(return_value=message)
    mock_client.list_messages = AsyncMock(return_value=[message])
    mock_client.session_status_map = AsyncMock(return_value={"sess-pdf": {"status": "idle"}})
    mock_client.diff = AsyncMock(return_value=diff)
    mock_client.kill_session = AsyncMock(return_value={"session_id": "sess-pdf", "aborted": True, "deleted": True})

    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        resp = client.post(
            "/v1/scillm/opencode/runs",
            headers={"X-Caller-Skill": "pdf-lab"},
            json={
                "prompt": "Run the bounded patch delegate.",
                "agent": "build",
                "cwd": str(cwd),
                "patch_mode": "live",
                "batch_id": "batch-1",
                "case_id": "page_case_0001_p0001",
                "page_number": 1,
                "candidate_id": "cand-1",
                "scillm_metadata": {
                    "batch_id": "batch-1",
                    "item_id": "page_case_0001_p0001",
                    "patch_mode": "live",
                },
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["patch_delegate_status"] == "PATCH_APPLIED"
    assert body["patch_delegate_reason"] == ""
    assert body["patch_delegate"]["diff_count"] == 1
    assert body["patch_delegate"]["changed_paths"] == ["tests/test_pdf_lab_canary.py"]


@pytest.mark.asyncio
async def test_diff_with_fallback_uses_filesystem_snapshot_when_opencode_diff_empty(tmp_path: Path) -> None:
    from scillm.proxy.opencode_serve_api import (
        OpenCodeServeRun,
        _diff_with_fallback,
        _filesystem_snapshot,
    )

    workspace = tmp_path / "workspace"
    source = workspace / "src" / "calc.py"
    source.parent.mkdir(parents=True)
    source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    before = _filesystem_snapshot(str(workspace))
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    class EmptyDiffClient:
        async def diff(self, *_args, **_kwargs):
            return []

    run = OpenCodeServeRun(
        run_id="oc-fs-diff",
        artifact_root=tmp_path / "artifacts",
        caller_skill="pdf-lab",
        agent="build",
        session_id="ses-fs",
        request_payload={},
        directory=str(workspace),
    )

    diff, evidence = await _diff_with_fallback(EmptyDiffClient(), run, before_snapshot=before)

    assert diff == [{"path": "src/calc.py", "status": "modified", "source": "filesystem_snapshot"}]
    assert evidence["changed_paths"] == ["src/calc.py"]
    assert evidence["diff_source"] == "filesystem_snapshot"
    assert evidence["diff_artifact"]
    assert "return a - b" in Path(evidence["diff_artifact"]).read_text(encoding="utf-8")
    assert "return a + b" in Path(evidence["diff_artifact"]).read_text(encoding="utf-8")


def test_opencode_kill_session_protects_only_active_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stale status.json alone must not block kill; only in-process active runs do."""
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    run_dir = tmp_path / "oc-run1"
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(
        '{"session_id": "sess-stale", "state": "running", "agent": "build"}',
        encoding="utf-8",
    )

    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.kill_session = AsyncMock(return_value={"session_id": "sess-stale", "aborted": True, "deleted": True})

    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        resp = client.post(
            "/v1/scillm/opencode/sessions/sess-stale/kill",
            headers={"X-Caller-Skill": "test-opencode-serve"},
        )
        assert resp.status_code == 200
        mock_client.kill_session.assert_awaited()


def test_opencode_serve_runtime_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    async def _inspect() -> dict:
        return {"schema": "scillm.opencode_serve_runtime.v1", "docker_control_enabled": True}

    async def _restart(**_kwargs: object) -> dict:
        return {"schema": "scillm.opencode_serve_runtime_action.v1", "actions": []}

    with patch(
        "scillm.proxy.opencode_serve_api.inspect_opencode_serve_runtime",
        side_effect=_inspect,
    ), patch(
        "scillm.proxy.opencode_serve_api.restart_opencode_serve_runtime",
        side_effect=_restart,
    ):
        client = TestClient(app)
        status = client.get(
            "/v1/scillm/opencode/serve/runtime",
            headers={"X-Caller-Skill": "test-opencode-serve"},
        )
        restart = client.post(
            "/v1/scillm/opencode/serve/restart",
            headers={"X-Caller-Skill": "test-opencode-serve"},
        )

    assert status.status_code == 200
    assert status.json()["schema"] == "scillm.opencode_serve_runtime.v1"
    assert restart.status_code == 200
    assert restart.json()["schema"] == "scillm.opencode_serve_runtime_action.v1"

def test_serve_run_dialog_for_transport_room(tmp_path, monkeypatch):
    """oc-* runs expose transport-room dialog + run-index for ux-lab."""
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    run_dir = tmp_path / "oc-transport-bridge"
    run_dir.mkdir()
    status = {
        "run_id": "oc-transport-bridge",
        "state": "completed",
        "session_id": "ses_bridge",
        "caller_skill": "pdf-lab",
        "human_monitor": {
            "session_title": "pdf_lab bridge run",
            "scillm_chat_monitor_url": "http://127.0.0.1:4001/v1/scillm/opencode/runs/oc-transport-bridge/monitor?token=t",
        },
    }
    (run_dir / "status.json").write_text(__import__("json").dumps(status), encoding="utf-8")
    (run_dir / "request.json").write_text(
        __import__("json").dumps({"scillm_metadata": {"case_id": "page_case_1"}}),
        encoding="utf-8",
    )
    messages = [
        {
            "info": {"id": "msg-1", "role": "user"},
            "parts": [{"type": "text", "text": "Apply patch to foo.py"}],
        },
        {
            "info": {"id": "msg-2", "role": "assistant", "agent": "build"},
            "parts": [{"type": "text", "text": "PATCH_APPLIED"}],
        },
    ]
    (run_dir / "messages_snapshot.json").write_text(
        __import__("json").dumps({"messages": messages}),
        encoding="utf-8",
    )
    from scillm.proxy.opencode_serve_dialog import (
        build_serve_dialog_response,
        list_serve_run_index,
        load_serve_run,
    )

    index = list_serve_run_index()
    indexed = next(r for r in index if r["transport_run_id"] == "oc-transport-bridge")
    assert indexed["run_id"] == "oc-transport-bridge"
    assert indexed["id"] == "oc-transport-bridge"
    assert indexed["title"] == "pdf_lab bridge run"
    assert indexed["state"] == "completed"
    assert indexed["phase"] is None
    assert indexed["session_id"] == "ses_bridge"
    assert indexed["human_monitor_url"].endswith("/monitor?token=t")
    from scillm.proxy.opencode_transport import list_transport_run_index

    transport_index = list_transport_run_index()
    transport_row = next(r for r in transport_index if r["run_id"] == "oc-transport-bridge")
    assert transport_row["run_kind"] == "opencode_serve"
    assert transport_row["title"] == "pdf_lab bridge run"
    run = load_serve_run("oc-transport-bridge")
    dialog = build_serve_dialog_response(run)
    assert dialog["run_kind"] == "opencode_serve"
    assert dialog["human_can_participate"] is False
    assert len(dialog["turns"]) == 2
    assert dialog["observation"]["browser_worker_url"]

def test_opencode_run_disconnect_returns_terminal_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))

    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.list_agents = AsyncMock(return_value=[{"name": "build"}])
    mock_client.create_session = AsyncMock(return_value={"id": "sess-disc", "directory": str(tmp_path)})
    mock_client.register_mcp = AsyncMock()
    mock_client.send_message = AsyncMock(
        side_effect=httpx.RemoteProtocolError("Server disconnected without sending a response.")
    )
    mock_client.abort = AsyncMock(return_value=True)
    mock_client.session_status_map = AsyncMock(return_value={})
    mock_client.diff = AsyncMock(return_value=[])
    mock_client.list_messages = AsyncMock(return_value=[])
    mock_client.kill_session = AsyncMock(return_value={"session_id": "sess-disc", "aborted": True, "deleted": True})

    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        resp = client.post(
            "/v1/scillm/opencode/runs",
            headers={"X-Caller-Skill": "pdf-lab"},
            json={
                "prompt": "patch",
                "agent": "build",
                "cwd": str(tmp_path),
                "timeout_s": 120,
                "patch_mode": "live",
                "scillm_metadata": {"case_id": "page_case_0001_p0001"},
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "disconnected"
    assert body.get("terminal_blocker")
    assert body.get("disconnect_summary")
    assert body["disconnect_summary"].get("http_error")
    assert body.get("human_monitor", {}).get("scillm_chat_monitor_url")

    run_id = body["run_id"]
    status = json.loads((tmp_path / run_id / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "disconnected"
    assert status.get("terminal_blocker")
    events = [
        json.loads(line)
        for line in (tmp_path / run_id / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(e.get("event") == "run_disconnected" for e in events)

def test_opencode_run_client_disconnect_finalizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate client disconnect (CancelledError) after session is created."""
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))

    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.list_agents = AsyncMock(return_value=[{"name": "build"}])
    mock_client.create_session = AsyncMock(return_value={"id": "sess-cancel", "directory": str(tmp_path)})
    mock_client.register_mcp = AsyncMock()
    mock_client.abort = AsyncMock(return_value=True)
    mock_client.session_status_map = AsyncMock(return_value={})
    mock_client.diff = AsyncMock(return_value=[])
    mock_client.list_messages = AsyncMock(return_value=[])
    mock_client.kill_session = AsyncMock(return_value={"session_id": "sess-cancel", "aborted": True, "deleted": True})

    async def _cancelled_execute(*_a, **_k):
        raise asyncio.CancelledError()

    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        with patch("scillm.proxy.opencode_serve_api._execute_run", side_effect=_cancelled_execute):
            client = TestClient(app)
            resp = client.post(
                "/v1/scillm/opencode/runs",
                headers={"X-Caller-Skill": "pdf-lab"},
                json={
                    "prompt": "patch",
                    "agent": "build",
                    "cwd": str(tmp_path),
                    "timeout_s": 120,
                },
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "disconnected"
    assert body.get("disconnect_summary")
    run_id = body["run_id"]
    status = json.loads((tmp_path / run_id / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "disconnected"
    assert (tmp_path / run_id / "opencode_result.json").is_file()
    events = [
        json.loads(line)
        for line in (tmp_path / run_id / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(e.get("event") == "run_disconnected" for e in events)


def test_opencode_run_blocks_reasoning_only_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))

    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    message = {
        "info": {"role": "assistant", "id": "msg-reasoning", "completed": 1},
        "parts": [
            {"type": "step-start"},
            {"type": "reasoning", "text": "Considering memory and tasks"},
            {"type": "text", "text": ""},
        ],
    }
    messages = [
        {"info": {"role": "user", "id": "msg-user"}, "parts": [{"type": "text", "text": "patch"}]},
        message,
    ]

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.list_agents = AsyncMock(return_value=[{"name": "build"}])
    mock_client.create_session = AsyncMock(return_value={"id": "sess-reasoning", "directory": str(workspace)})
    mock_client.register_mcp = AsyncMock()
    mock_client.send_message = AsyncMock(return_value=message)
    mock_client.list_messages = AsyncMock(return_value=messages)
    mock_client.session_status_map = AsyncMock(return_value={"sess-reasoning": {"status": "idle"}})
    mock_client.diff = AsyncMock(return_value=[])
    mock_client.abort = AsyncMock(return_value=True)
    mock_client.kill_session = AsyncMock(return_value={"session_id": "sess-reasoning", "aborted": True, "deleted": True})

    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        resp = client.post(
            "/v1/scillm/opencode/runs",
            headers={"X-Caller-Skill": "pdf-lab"},
            json={
                "prompt": "patch delegate must start with tools",
                "agent": "build",
                "cwd": str(workspace),
                "timeout_s": 30,
                "patch_mode": "live",
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "timeout"
    assert body["terminal_blocker"]["primary_reason"] == "reasoning_only_no_terminal_text"
    assert body["timeout_summary"]["last_assistant_terminal_text_chars"] == 0
    assert body["timeout_summary"]["last_assistant_reasoning_chars"] > 0
    assert body["patch_delegate_status"] == "PATCH_DELEGATE_BLOCKED"
    events = [
        json.loads(line)
        for line in (tmp_path / body["run_id"] / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(e.get("event") == "assistant_waiting_for_terminal_text" for e in events)
    assert not any(e.get("event") == "run_completed" for e in events)


def test_serve_dialog_post_labels_active_child_as_side_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    run_dir = tmp_path / "oc-dialog-active"
    run_dir.mkdir()
    (run_dir / "status.json").write_text(
        json.dumps(
            {
                "run_id": "oc-dialog-active",
                "state": "running",
                "phase": "prompting",
                "session_id": "sess-dialog-active",
                "caller_skill": "pdf-lab",
                "agent": "build",
                "cwd": str(tmp_path),
            }
        ),
        encoding="utf-8",
    )

    app = FastAPI()
    app.add_exception_handler(ProxyError, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.session_status_map = AsyncMock(return_value={"sess-dialog-active": {"status": "busy"}})
    mock_client.send_message = AsyncMock(return_value={"info": {"id": "msg-note"}})

    with patch("scillm.proxy.opencode_serve.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        resp = client.post(
            "/v1/scillm/opencode/runs/oc-dialog-active/dialog",
            headers={"X-Caller-Skill": "pdf-lab"},
            json={"speaker": "Project agent", "body": "Use the preset path."},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["delivery_mode"] == "queued_for_next_turn"
    assert body["steering_supported"] is False
    assert body["active_turn_interrupt_supported"] is False
    assert "does not interrupt" in body["project_agent_message"]
    assert "replayed as a real prompt" in body["project_agent_message"]
    assert mock_client.send_message.await_args.kwargs["no_reply"] is True
    # The turn is queued for replay when the session goes idle (issue #13)
    queued_lines = (run_dir / "pending_dialog.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(queued_lines) == 1
    assert "Use the preset path." in queued_lines[0]
    events = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(e.get("event") == "dialog.delivery" and e.get("delivery_mode") == "queued_for_next_turn" for e in events)
    assert any(e.get("event") == "dialog.queued_for_next_turn" for e in events)


def test_serve_dialog_queue_drain_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scillm.proxy.opencode_serve_api import OpenCodeServeRun
    from scillm.proxy.opencode_serve_dialog import drain_pending_dialog, queue_pending_dialog_turn

    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    run = OpenCodeServeRun(
        run_id="oc-queue-roundtrip",
        artifact_root=tmp_path,
        caller_skill="pdf-lab",
        agent="build",
        session_id="sess-q",
        request_payload={},
    )
    assert drain_pending_dialog(run) == []
    queue_pending_dialog_turn(run, {"speaker": "Project agent", "text": "nudge one"})
    queue_pending_dialog_turn(run, {"speaker": "Project agent", "text": "nudge two"})
    drained = drain_pending_dialog(run)
    assert [t["text"] for t in drained] == ["nudge one", "nudge two"]
    # drain consumes atomically — second drain is empty
    assert drain_pending_dialog(run) == []


def test_serve_run_index_includes_custom_run_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #10: caller-supplied run_ids without the oc- prefix must be indexed."""
    from scillm.proxy.opencode_serve_dialog import list_serve_run_index

    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    for name, phase in (("pdf-lab-p45-custom", "prompting"), ("oc-standard", "created")):
        d = tmp_path / name
        d.mkdir()
        (d / "status.json").write_text(
            json.dumps({"run_id": name, "state": "running", "phase": phase}), encoding="utf-8"
        )
    rows = {r["run_id"] for r in list_serve_run_index()}
    assert {"pdf-lab-p45-custom", "oc-standard"} <= rows


@pytest.mark.asyncio
async def test_execute_run_delivers_queued_dialog_before_terminalizing(tmp_path: Path) -> None:
    """Issue #13: a nudge queued while the child turn was active must be
    replayed as a real prompt before the run accepts terminal text."""
    from scillm.proxy import opencode_serve_api as api_mod
    from scillm.proxy.opencode_serve_dialog import queue_pending_dialog_turn

    run = OpenCodeServeRun(
        run_id="oc-queued-nudge",
        artifact_root=tmp_path,
        caller_skill="pdf-lab",
        agent="build",
        session_id="sess-queued-nudge",
        request_payload={"prompt": "patch"},
        directory=str(tmp_path),
    )
    spec = OpenCodeRunRequest(prompt="patch", agent="build", wait=False, timeout_s=10)
    receipt = SkillViewReceipt((), (), (), None)
    queue_pending_dialog_turn(run, {"speaker": "Project agent", "text": "Stop re-litigating; use the preset path."})

    pre_nudge_message = {
        "info": {"role": "assistant", "id": "msg-1"},
        "parts": [{"type": "text", "text": "I think we should debate Rust vs preset further."}],
    }
    post_nudge_message = {
        "info": {"role": "assistant", "id": "msg-2"},
        "parts": [{"type": "text", "text": "Understood — implementing the preset/applier path now."}],
    }

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.send_prompt_async = AsyncMock(return_value=None)
    mock_client.send_message = AsyncMock(return_value={"info": {"id": "msg-nudge"}})
    mock_client.list_messages = AsyncMock(side_effect=[[pre_nudge_message], [post_nudge_message], [post_nudge_message]])
    mock_client.session_status_map = AsyncMock(return_value={"sess-queued-nudge": {"status": "idle"}})
    mock_client.diff = AsyncMock(return_value=[])

    with patch("scillm.proxy.opencode_serve_api.OpenCodeServeClient", return_value=mock_client):
        result = await api_mod._execute_run(run, spec, skill_receipt=receipt)

    assert result["status"] == "completed"
    assert "preset/applier path" in result["assistant_text"]
    # the queued nudge was sent as a REAL prompt (no_reply=False)
    nudge_call = mock_client.send_message.await_args
    assert nudge_call.kwargs["no_reply"] is False
    assert "Stop re-litigating" in str(nudge_call.kwargs.get("parts") or nudge_call.args)
    events = [
        json.loads(line)["event"]
        for line in run.events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert "dialog.queued_turn_delivered" in events
    assert events.index("dialog.queued_turn_delivered") < events.index("run_completed")
    # queue is consumed
    assert not (run.run_dir / "pending_dialog.jsonl").exists()
