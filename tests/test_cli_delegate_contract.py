"""Deterministic tests for the scillm agent delegate contract (issue #19)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from scillm.cli import _run_project_agent


def make_args(tmp_path: Path, require_artifact=None):
    return argparse.Namespace(
        profile="build",
        model=None,
        base_url="http://test",
        api_key="k",
        caller="test",
        json=True,
        timeout=30.0,
        require_artifact=require_artifact or [],
    )


def run_with_response(args, payload, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.content = json.dumps(payload).encode()
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=None)
    client.post.return_value = resp
    with patch("scillm.cli.httpx.Client", return_value=client):
        return _run_project_agent(args, prompt="do the thing")


def test_disconnected_status_exits_nonzero(tmp_path, capsys):
    rc = run_with_response(make_args(tmp_path), {"status": "disconnected", "run_id": "r1", "assistant_text": ""})
    assert rc == 2
    err = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert err["ok"] is False
    assert "disconnected" in err["reason"]


def test_timeout_status_exits_nonzero(tmp_path):
    assert run_with_response(make_args(tmp_path), {"status": "timeout", "run_id": "r2"}) == 2


def test_completed_without_required_artifact_fails_closed(tmp_path, capsys):
    args = make_args(tmp_path, require_artifact=[str(tmp_path / "receipt.json")])
    rc = run_with_response(args, {"status": "completed", "run_id": "r3", "assistant_text": "done"})
    assert rc == 3
    err = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert err["missing_artifacts"] == [str(tmp_path / "receipt.json")]


def test_completed_with_artifact_present_succeeds(tmp_path):
    artifact = tmp_path / "receipt.json"
    artifact.write_text('{"status":"COMPLETED"}')
    args = make_args(tmp_path, require_artifact=[str(artifact)])
    assert run_with_response(args, {"status": "completed", "run_id": "r4", "assistant_text": "done"}) == 0


def test_plain_completed_still_succeeds(tmp_path):
    assert run_with_response(make_args(tmp_path), {"status": "completed", "run_id": "r5", "assistant_text": "ok"}) == 0
