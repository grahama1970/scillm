from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from scillm.proxy.providers import auth


NOW_S = 2_000_000_000.0
NOW_MS = int(NOW_S * 1000)


@pytest.fixture(autouse=True)
def _isolated_auth_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(auth, "AUTH_FILE", tmp_path / "pi" / "auth.json")
    monkeypatch.setattr(auth, "CLAUDE_CODE_CREDENTIALS", tmp_path / "claude" / ".credentials.json")
    monkeypatch.setattr(auth, "CODEX_AUTH_FILE", tmp_path / "codex" / "auth.json")
    monkeypatch.setattr(auth, "OPENCODE_AUTH_FILE", tmp_path / "opencode" / "auth.json")
    monkeypatch.setattr(auth, "AUTH_STALE_FILE_THRESHOLD_S", 3600.0)
    monkeypatch.setattr(auth.time, "time", lambda: NOW_S)
    auth._LAST_REFRESH_OUTCOMES.clear()
    yield
    auth._LAST_REFRESH_OUTCOMES.clear()


def _write_claude_credentials(*, mtime_s: float, refresh_token: str | None = "refresh") -> Path:
    path = auth.CLAUDE_CODE_CREDENTIALS
    path.parent.mkdir(parents=True)
    oauth = {
        "accessToken": "expired-access",
        "expiresAt": NOW_MS - 1,
    }
    if refresh_token is not None:
        oauth["refreshToken"] = refresh_token
    path.write_text(json.dumps({"claudeAiOauth": oauth}))
    os.utime(path, (mtime_s, mtime_s))
    return path


def _jwt(exp_s: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp_s}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def test_old_file_plus_network_error_classifies_as_stale_bind(
    monkeypatch: pytest.MonkeyPatch,
):
    credential_file = _write_claude_credentials(mtime_s=NOW_S - 7200)

    def raise_network_error(*_args, **_kwargs):
        raise auth.httpx.ConnectError("connection refused")

    monkeypatch.setattr(auth.httpx, "post", raise_network_error)

    assert auth.get_anthropic_token() is None

    report = auth.get_auth_status_snapshot(NOW_MS)["claude"]
    assert report["provider_auth_status"] == "stale_bind_mount_suspected"
    assert report["credential_file"] == str(credential_file)
    assert report["credential_file_mtime_ms"] == int((NOW_S - 7200) * 1000)
    assert report["token_expires_at_ms"] == NOW_MS - 1
    assert report["last_refresh_outcome"] == "failed"
    assert report["last_refresh_failure_reason"] == "network_error"
    assert report["last_refresh_http_status"] is None
    assert report["last_refresh_error_body_classification"] == "ambiguous_network_error"
    assert "directory credential bind mount" in report["project_agent_message"]


def test_old_file_plus_invalid_grant_classifies_as_needs_human_login(
    monkeypatch: pytest.MonkeyPatch,
):
    _write_claude_credentials(mtime_s=NOW_S - 7200)

    def invalid_grant_response(*_args, **_kwargs):
        request = auth.httpx.Request("POST", auth.ANTHROPIC_TOKEN_URL)
        return auth.httpx.Response(
            400,
            request=request,
            json={
                "error": "invalid_grant",
                "error_description": "The refresh token has been revoked",
            },
        )

    monkeypatch.setattr(auth.httpx, "post", invalid_grant_response)

    assert auth.get_anthropic_token() is None

    report = auth.get_auth_status_snapshot(NOW_MS)["claude"]
    assert report["provider_auth_status"] == "needs_human_login"
    assert report["last_refresh_outcome"] == "failed"
    assert report["last_refresh_failure_reason"] == "http_error"
    assert report["last_refresh_http_status"] == 400
    assert report["last_refresh_provider_error_code"] == "invalid_grant"
    assert report["last_refresh_error_body_classification"] == "login_required_invalid_grant"
    assert "CLI login" in report["project_agent_message"]
    assert "directory credential bind mount" not in report["project_agent_message"]


def test_current_mtime_keeps_failed_refresh_as_needs_human_login(
    monkeypatch: pytest.MonkeyPatch,
):
    _write_claude_credentials(mtime_s=NOW_S - 30)
    monkeypatch.setattr(auth, "_refresh_anthropic", lambda _cred: None)

    assert auth.get_anthropic_token() is None

    report = auth.get_auth_status_snapshot(NOW_MS)["claude"]
    assert report["provider_auth_status"] == "needs_human_login"
    assert report["last_refresh_outcome"] == "failed"
    assert "CLI login" in report["project_agent_message"]
    assert "directory credential bind mount" not in report["project_agent_message"]


def test_missing_refresh_token_in_old_file_is_ambiguous_stale_bind():
    _write_claude_credentials(mtime_s=NOW_S - 7200, refresh_token=None)

    assert auth.get_anthropic_token() is None

    report = auth.get_auth_status_snapshot(NOW_MS)["claude"]
    assert report["provider_auth_status"] == "stale_bind_mount_suspected"
    assert report["last_refresh_outcome"] == "failed"
    assert report["last_refresh_failure_reason"] == "missing_refresh_token"
    assert (
        report["last_refresh_error_body_classification"]
        == "ambiguous_missing_refresh_token"
    )


def test_auth_snapshot_reports_codex_expiry_mtime_and_refresh_outcome():
    path = auth.CODEX_AUTH_FILE
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"tokens": {"access_token": _jwt(int(NOW_S + 900))}}))
    os.utime(path, (NOW_S - 45, NOW_S - 45))

    report = auth.get_auth_status_snapshot(NOW_MS)["codex"]

    assert report["provider_auth_status"] == "valid"
    assert report["token_expires_at_ms"] == int((NOW_S + 900) * 1000)
    assert report["expires_in_s"] == 900
    assert report["credential_file_mtime_ms"] == int((NOW_S - 45) * 1000)
    assert report["last_refresh_outcome"] == "not_attempted_since_process_start"


def test_codex_failed_refresh_uses_the_same_mtime_staleness_rule(
    monkeypatch: pytest.MonkeyPatch,
):
    path = auth.CODEX_AUTH_FILE
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": _jwt(int(NOW_S - 60)),
                    "refresh_token": "refresh",
                }
            }
        )
    )
    os.utime(path, (NOW_S - 7200, NOW_S - 7200))

    def raise_network_error(*_args, **_kwargs):
        raise auth.httpx.ConnectError("connection refused")

    monkeypatch.setattr(auth.httpx, "post", raise_network_error)

    assert auth.get_codex_credentials() is None

    report = auth.get_auth_status_snapshot(NOW_MS)["codex"]
    assert report["provider_auth_status"] == "stale_bind_mount_suspected"
    assert report["last_refresh_outcome"] == "failed"
    assert report["last_refresh_failure_reason"] == "network_error"
    assert report["last_refresh_error_body_classification"] == "ambiguous_network_error"


def test_successful_claude_refresh_is_written_back_and_reported(
    monkeypatch: pytest.MonkeyPatch,
):
    path = _write_claude_credentials(mtime_s=NOW_S - 7200)
    monkeypatch.setattr(
        auth,
        "_refresh_anthropic",
        lambda _cred: {
            "access": "new-access",
            "refresh": "new-refresh",
            "expires": NOW_MS + 3600 * 1000,
        },
    )

    assert auth.get_anthropic_token() == "new-access"

    written = json.loads(path.read_text())["claudeAiOauth"]
    assert written["accessToken"] == "new-access"
    assert written["refreshToken"] == "new-refresh"
    assert written["expiresAt"] == NOW_MS + 3600 * 1000
    report = auth.get_auth_status_snapshot(NOW_MS)["claude"]
    assert report["provider_auth_status"] == "valid"
    assert report["last_refresh_outcome"] == "succeeded"


def test_core_compose_uses_writable_directory_mounts_and_durable_config():
    compose = Path("deploy/docker/compose.scillm.core.yml").read_text()

    assert "${HOME}/.pi/agent:/root/.pi/agent\"" in compose
    assert "${HOME}/.claude:/root/.claude\"" in compose
    assert "${HOME}/.codex:/root/.codex\"" in compose
    assert "${HOME}/.local/share/opencode:/root/.local/share/opencode\"" in compose
    assert "${HOME}/.claude/.credentials.json" not in compose
    assert "${HOME}/.codex/auth.json" not in compose
    assert "${HOME}/.pi/agent/auth.json" not in compose
    assert "${HOME}/.local/share/opencode/auth.json" not in compose
    assert "${SCILLM_REPO_ROOT:-/home/graham/workspace/experiments/scillm}" in compose
    assert "../../local/proxy_server_config.yaml:/app/config.yaml" not in compose
