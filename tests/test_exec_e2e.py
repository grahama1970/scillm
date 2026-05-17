"""Live e2e sanity checks for scillm exec endpoints.

These tests hit the running proxy, not mocks. They exercise deterministic
local_command nodes so they do not depend on LLM provider availability.
"""
from __future__ import annotations

import threading
import time
import uuid

import httpx
import pytest


def _run_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _py_json_command(payload: dict) -> list[str]:
    # repr(payload) is safe here because payload is test-owned deterministic data.
    return ["python", "-c", "import json; print(json.dumps(" + repr(payload) + "))"]


def _skip_if_unreachable(proxy_reachable: bool) -> None:
    if not proxy_reachable:
        pytest.skip("scillm proxy is not reachable")


class TestExecEndpoints:
    def test_exec_local_command_writes_status_and_events(self, client: httpx.Client, proxy_reachable: bool):
        _skip_if_unreachable(proxy_reachable)
        run_id = _run_id("e2e-exec-local")

        response = client.post(
            "/v1/scillm/exec",
            json={
                "run_id": run_id,
                "id": "local_ok",
                "type": "local_command",
                "node_goal": "Return deterministic JSON for exec endpoint sanity.",
                "command": _py_json_command({"status": "ok", "endpoint": "exec"}),
            },
            timeout=60.0,
        )

        assert response.status_code == 200, response.text[:500]
        body = response.json()
        assert body["status"] == "completed"
        assert body["result"]["ok"] is True
        assert body["result"]["result"]["status"] == "ok"

        status = client.get(f"/v1/scillm/exec/{run_id}/status", timeout=10.0)
        assert status.status_code == 200, status.text[:500]
        assert status.json()["state"] == "completed"

        events = client.get(f"/v1/scillm/exec/{run_id}/events?tail=50", timeout=10.0)
        assert events.status_code == 200, events.text[:500]
        event_types = [event["type"] for event in events.json()["events"]]
        assert "exec_started" in event_types
        assert "exec_finished" in event_types

    def test_exec_batch_runs_independent_workers(self, client: httpx.Client, proxy_reachable: bool):
        _skip_if_unreachable(proxy_reachable)
        batch_id = _run_id("e2e-exec-batch")

        response = client.post(
            "/v1/scillm/exec/batch",
            json={
                "batch_id": batch_id,
                "graph_goal": "Run independent deterministic exec workers.",
                "max_concurrency": 2,
                "items": [
                    {
                        "id": "worker_a",
                        "type": "local_command",
                        "node_goal": "Return worker A JSON.",
                        "command": _py_json_command({"worker": "a"}),
                    },
                    {
                        "id": "worker_b",
                        "type": "local_command",
                        "node_goal": "Return worker B JSON.",
                        "command": _py_json_command({"worker": "b"}),
                    },
                ],
            },
            timeout=60.0,
        )

        assert response.status_code == 200, response.text[:500]
        body = response.json()
        assert body["status"] == "completed"
        assert sorted(body["completed"]) == ["worker_a", "worker_b"]
        assert body["failed"] == []

    def test_exec_graph_respects_dependencies(self, client: httpx.Client, proxy_reachable: bool, tmp_path):
        _skip_if_unreachable(proxy_reachable)
        graph_id = _run_id("e2e-exec-graph")
        cwd = tmp_path / "graph-cwd"
        cwd.mkdir()

        response = client.post(
            "/v1/scillm/exec/graph",
            json={
                "graph_id": graph_id,
                "graph_goal": "Run one deterministic node before a dependent node.",
                "cwd": str(cwd),
                "max_concurrency": 2,
                "nodes": [
                    {
                        "id": "write_marker",
                        "type": "local_command",
                        "node_goal": "Write a marker file.",
                        "command": ["python", "-c", "from pathlib import Path; Path('marker.txt').write_text('ok')"],
                    },
                    {
                        "id": "read_marker",
                        "type": "local_command",
                        "node_goal": "Read marker file after dependency completes.",
                        "depends_on": ["write_marker"],
                        "command": [
                            "python",
                            "-c",
                            "import json; from pathlib import Path; print(json.dumps({'marker': Path('marker.txt').read_text()}))",
                        ],
                    },
                ],
            },
            timeout=60.0,
        )

        assert response.status_code == 200, response.text[:500]
        body = response.json()
        assert body["status"] == "completed"
        assert body["node_results"]["read_marker"]["result"]["marker"] == "ok"

    def test_exec_graph_skips_dependents_after_failure(self, client: httpx.Client, proxy_reachable: bool):
        _skip_if_unreachable(proxy_reachable)
        graph_id = _run_id("e2e-exec-failure")

        response = client.post(
            "/v1/scillm/exec/graph",
            json={
                "graph_id": graph_id,
                "graph_goal": "Verify failed dependencies skip dependent nodes.",
                "max_concurrency": 2,
                "nodes": [
                    {
                        "id": "fail_first",
                        "type": "local_command",
                        "node_goal": "Fail intentionally.",
                        "command": ["python", "-c", "raise SystemExit(3)"],
                    },
                    {
                        "id": "dependent",
                        "type": "local_command",
                        "node_goal": "Should be skipped.",
                        "depends_on": ["fail_first"],
                        "command": _py_json_command({"should_not_run": True}),
                    },
                ],
            },
            timeout=60.0,
        )

        assert response.status_code == 200, response.text[:500]
        body = response.json()
        assert body["status"] == "failed"
        assert body["node_results"]["dependent"]["status"] == "skipped"
        assert body["node_results"]["dependent"]["failure_type"] == "dependency_failed"

    def test_exec_cancel_long_running_node(self, client: httpx.Client, proxy_reachable: bool):
        _skip_if_unreachable(proxy_reachable)
        run_id = _run_id("e2e-exec-cancel")
        result_holder: dict = {}

        def run_request() -> None:
            result_holder["response"] = client.post(
                "/v1/scillm/exec",
                json={
                    "run_id": run_id,
                    "id": "long_sleep",
                    "type": "local_command",
                    "node_goal": "Sleep long enough to be cancelled.",
                    "timeout_s": 60,
                    "command": ["python", "-c", "import time; time.sleep(30)"],
                },
                timeout=90.0,
            )

        thread = threading.Thread(target=run_request, daemon=True)
        thread.start()
        time.sleep(2.0)

        cancel = client.post(f"/v1/scillm/exec/{run_id}/cancel", timeout=10.0)
        assert cancel.status_code == 200, cancel.text[:500]
        assert cancel.json()["cancel_requested"] is True

        thread.join(timeout=30.0)
        assert "response" in result_holder, "exec request did not return after cancel"
        assert result_holder["response"].status_code == 200, result_holder["response"].text[:500]
        assert result_holder["response"].json()["status"] in {"failed", "cancelled"}
