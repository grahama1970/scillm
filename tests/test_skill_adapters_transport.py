"""Harness adapters for memory, scillm, debugger + transport skill_call helpers."""

from __future__ import annotations

import pytest

from scillm.harness.skill_adapters import run_skill_call
from scillm.harness.skill_adapters.debugger import DebuggerAdapter
from scillm.harness.skill_adapters.memory import MemoryAdapter
from scillm.harness.skill_adapters.scillm import ScillmAdapter
from scillm.proxy.opencode_transport import (
    build_skill_call_spec,
    dialog_turn_from_message,
    extract_skill_slugs,
    routing_hint_for_turn,
    strip_skill_slugs,
    DialogTurn,
)


def _spec(skill: str, **args: object) -> dict:
    base = {
        "schema": "scillm.skill_call.v1",
        "action": "skill_call",
        "skill_call_id": f"skill-call-{skill}",
        "idempotency_key": f"sha256:{skill}",
        "skill": skill,
        "args": {"query": f"test {skill} query", **args},
        "requested_by": "test",
        "allowed_tools": [f"{skill}.run_sh"],
        "timeout_sec": 30,
        "turn_id": "harness_turns/turn-test",
    }
    return base


@pytest.mark.parametrize(
    "adapter_cls",
    [MemoryAdapter, ScillmAdapter, DebuggerAdapter],
)
def test_registered_adapters_dry_run(adapter_cls) -> None:
    skill = {
        MemoryAdapter: "memory",
        ScillmAdapter: "scillm",
        DebuggerAdapter: "debugger",
    }[adapter_cls]
    receipt = run_skill_call(_spec(skill), dry_run=True)
    assert receipt["schema"] == "memory.skill_invocation.v1"
    assert receipt["skill"] == skill
    assert receipt["status"] == "ok"
    assert receipt["dry_run"] is True


def test_extract_and_strip_skill_slugs() -> None:
    text = "Please /dogpile this issue and /memory prior lessons"
    assert extract_skill_slugs(text) == ["dogpile", "memory"]
    assert strip_skill_slugs(text) == "Please  this issue and  prior lessons"


def test_dialog_turn_includes_routing_hint_and_created_at() -> None:
    turn = dialog_turn_from_message(
        {
            "id": "msg-1",
            "created": "2026-05-27T12:00:00Z",
            "info": {"role": "user"},
            "parts": [{"type": "text", "text": "Human: hello"}],
        }
    )
    assert turn is not None
    assert turn.created_at.startswith("2026-05-27")
    assert turn.routing_hint["tone"] == "to-reviewer"
    assert turn.audience == "project_agent"


def test_routing_hint_worker_completion() -> None:
    turn = DialogTurn(
        message_id="m2",
        collaborator="worker",
        speaker="Worker (reviewer)",
        text="**Worker (reviewer)**\n\nDone.",
    )
    hint = routing_hint_for_turn(turn)
    assert hint["tone"] == "to-human"
    assert hint["inferred"] is False


def test_build_skill_call_spec_allowed_tools() -> None:
    spec = build_skill_call_spec(
        skill="memory",
        args={"query": "recall transport"},
        transport_run_id="otr-1",
        turn_id="transport_dialog/otr-1/t1",
        requested_by="Project agent",
    )
    assert spec["allowed_tools"] == ["memory.run_sh"]
    assert spec["skill"] == "memory"


@pytest.mark.asyncio
async def test_transport_post_dialog_executes_skill_call(tmp_path, monkeypatch) -> None:
    from unittest.mock import AsyncMock, patch

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from scillm.proxy.errors import proxy_error_handler
    from scillm.proxy.opencode_serve_api import create_opencode_serve_router
    from scillm.proxy.opencode_transport import OpenCodeTransport, TransportState

    monkeypatch.setenv("SCILLM_OPENCODE_SERVE_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("SCILLM_TRANSPORT_SKILL_DRY_RUN", "1")

    app = FastAPI()
    app.add_exception_handler(Exception, proxy_error_handler)
    app.include_router(create_opencode_serve_router(lambda _request: None), prefix="/v1/scillm")

    transport = OpenCodeTransport()
    state = TransportState(
        transport_run_id="otr-skill",
        dag_node_id="n1",
        parent_session_id="ses-parent",
        workspace=str(tmp_path),
    )
    transport.store.save(state)

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.settings.base_url = "http://127.0.0.1:4098"
    mock_client.list_messages.return_value = []

    with patch("scillm.proxy.opencode_transport_api.OpenCodeServeClient", return_value=mock_client):
        client = TestClient(app)
        resp = client.post(
            "/v1/scillm/opencode/transport/runs/otr-skill/dialog",
            headers={"X-Caller-Skill": "test-transport"},
            json={
                "speaker": "Project agent",
                "body": "Run /memory recall lessons for transport UX",
                "execute_skills": True,
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skill_call"]["status"] == "ok"
    assert body["skill_call"]["skill_invocation"]["skill"] == "memory"
    mock_client.send_message.assert_awaited()
