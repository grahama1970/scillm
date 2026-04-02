"""Tests for Claude and Codex OAuth providers through the scillm proxy.

Prerequisites:
- Fresh OAuth tokens in ~/.pi/agent/auth.json (run Pi CLI /login)
- scillm proxy running on localhost:4001
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

PROXY_BASE = "http://localhost:4001"
AUTH_HEADERS = {"Authorization": "Bearer sk-dev-proxy-123"}
AUTH_FILE = Path.home() / ".pi" / "agent" / "auth.json"


def _chat(client: httpx.Client, model: str, prompt: str, **kwargs) -> httpx.Response:
    return client.post(
        f"{PROXY_BASE}/v1/chat/completions",
        headers={**AUTH_HEADERS, "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": kwargs.get("max_tokens", 256),
            **{k: v for k, v in kwargs.items() if k != "max_tokens"},
        },
        timeout=kwargs.get("timeout", 60.0),
    )


def _proxy_reachable() -> bool:
    try:
        r = httpx.get(f"{PROXY_BASE}/health/liveliness", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def _anthropic_token_valid() -> bool:
    # Check Claude Code credentials first (primary source)
    cc_file = Path.home() / ".claude" / ".credentials.json"
    if cc_file.exists():
        try:
            data = json.loads(cc_file.read_text())
            oauth = data.get("claudeAiOauth", {})
            if oauth.get("accessToken") and int(time.time() * 1000) < oauth.get("expiresAt", 0):
                return True
        except Exception:
            pass
    # Fallback: Pi auth.json
    if not AUTH_FILE.exists():
        return False
    try:
        data = json.loads(AUTH_FILE.read_text())
        cred = data.get("anthropic", {})
        if cred.get("type") != "oauth":
            return False
        return int(time.time() * 1000) < cred.get("expires", 0)
    except Exception:
        return False


def _codex_token_valid() -> bool:
    # Check Codex CLI credentials first
    codex_file = Path.home() / ".codex" / "auth.json"
    if codex_file.exists():
        try:
            data = json.loads(codex_file.read_text())
            tokens = data.get("tokens", {})
            if tokens.get("access_token"):
                return True
        except Exception:
            pass
    # Fallback: Pi auth.json
    if not AUTH_FILE.exists():
        return False
    try:
        data = json.loads(AUTH_FILE.read_text())
        cred = data.get("openai-codex", {})
        return cred.get("type") == "oauth" and int(time.time() * 1000) < cred.get("expires", 0)
    except Exception:
        return False


_PROXY_OK = _proxy_reachable()


class TestClaudeOAuth:
    """Test Claude models via Anthropic OAuth."""

    @pytest.mark.skipif(not _PROXY_OK, reason="Proxy not reachable")
    @pytest.mark.skipif(not _anthropic_token_valid(), reason="No valid Anthropic OAuth token")
    def test_claude_sonnet_basic(self):
        """Basic text completion through Claude Sonnet."""
        with httpx.Client() as client:
            r = _chat(client, "claude-sonnet-4-6", "What is 2+2? Answer with just the number.")
            assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text[:200]}"
            data = r.json()
            assert "choices" in data
            content = data["choices"][0]["message"]["content"]
            assert "4" in content, f"Expected '4' in response: {content}"

    @pytest.mark.skipif(not _PROXY_OK, reason="Proxy not reachable")
    @pytest.mark.skipif(not _anthropic_token_valid(), reason="No valid Anthropic OAuth token")
    def test_claude_haiku_basic(self):
        """Basic text completion through Claude Haiku."""
        with httpx.Client() as client:
            r = _chat(client, "claude-haiku-4-5-20251001", "Say hello", max_tokens=32)
            assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text[:200]}"
            data = r.json()
            assert data["choices"][0]["message"]["content"]

    @pytest.mark.skipif(not _PROXY_OK, reason="Proxy not reachable")
    @pytest.mark.skipif(not _anthropic_token_valid(), reason="No valid Anthropic OAuth token")
    def test_claude_with_system_prompt(self):
        """System prompt should not crash — injected as user message for OAuth.

        Note: Claude Code OAuth scope locks the system prompt to the Claude Code
        prefix. Custom system prompts are injected as user messages instead.
        The model may not follow them strictly but should not error.
        """
        with httpx.Client() as client:
            r = client.post(
                f"{PROXY_BASE}/v1/chat/completions",
                headers={**AUTH_HEADERS, "Content-Type": "application/json"},
                json={
                    "model": "claude-sonnet-4-6",
                    "messages": [
                        {"role": "system", "content": "You are a helpful math tutor."},
                        {"role": "user", "content": "What is 2+2?"},
                    ],
                    "max_tokens": 32,
                },
                timeout=30.0,
            )
            assert r.status_code == 200, f"System prompt caused error: {r.text[:200]}"
            content = r.json()["choices"][0]["message"]["content"]
            assert content, "Response should not be empty"

    @pytest.mark.skipif(not _PROXY_OK, reason="Proxy not reachable")
    @pytest.mark.skipif(not _anthropic_token_valid(), reason="No valid Anthropic OAuth token")
    def test_claude_response_format(self):
        """Response should have OpenAI-compatible format."""
        with httpx.Client() as client:
            r = _chat(client, "claude-sonnet-4-6", "Say hi", max_tokens=32)
            assert r.status_code == 200
            data = r.json()
            # Verify OpenAI format
            assert "id" in data
            assert "choices" in data
            assert "usage" in data
            assert data["object"] == "chat.completion"
            assert data["choices"][0]["message"]["role"] == "assistant"
            assert isinstance(data["usage"]["prompt_tokens"], int)
            assert isinstance(data["usage"]["completion_tokens"], int)


    @pytest.mark.skipif(not _PROXY_OK, reason="Proxy not reachable")
    @pytest.mark.skipif(not _anthropic_token_valid(), reason="No valid Anthropic OAuth token")
    def test_claude_streaming(self):
        """Claude streaming should return SSE data lines with OpenAI delta format."""
        with httpx.Client() as client:
            with client.stream(
                "POST",
                f"{PROXY_BASE}/v1/chat/completions",
                headers={**AUTH_HEADERS, "Content-Type": "application/json"},
                json={
                    "model": "claude-sonnet-4-6",
                    "messages": [{"role": "user", "content": "Count to 3"}],
                    "stream": True,
                    "max_tokens": 32,
                },
                timeout=30.0,
            ) as resp:
                assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
                assert "text/event-stream" in resp.headers.get("content-type", "")

                lines = []
                for line in resp.iter_lines():
                    if line:
                        lines.append(line)

                # Must have at least one data chunk and [DONE]
                data_lines = [l for l in lines if l.startswith("data: ")]
                assert len(data_lines) >= 2, f"Expected >=2 data lines, got {len(data_lines)}"

                # Verify first content chunk is OpenAI delta format
                first_data = json.loads(data_lines[0][6:])
                assert first_data["object"] == "chat.completion.chunk"
                assert "delta" in first_data["choices"][0]

                # Verify finish_reason in penultimate chunk
                pre_done = [l for l in data_lines if l != "data: [DONE]"]
                last_chunk = json.loads(pre_done[-1][6:])
                assert last_chunk["choices"][0]["finish_reason"] is not None

                # Verify [DONE] sentinel
                assert data_lines[-1] == "data: [DONE]"


class TestCodexOAuth:
    """Test Codex/GPT models via OpenAI OAuth."""

    @pytest.mark.skipif(not _PROXY_OK, reason="Proxy not reachable")
    @pytest.mark.skipif(not _codex_token_valid(), reason="No valid Codex OAuth token")
    def test_gpt_basic(self):
        """Basic text completion through GPT via Codex OAuth."""
        with httpx.Client() as client:
            r = _chat(client, "gpt-5.3-codex", "What is 2+2? Answer with just the number.", max_tokens=32, timeout=60.0)
            assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text[:200]}"
            data = r.json()
            assert "choices" in data
            content = data["choices"][0]["message"]["content"]
            assert "4" in content

    @pytest.mark.skipif(not _PROXY_OK, reason="Proxy not reachable")
    @pytest.mark.skipif(not _codex_token_valid(), reason="No valid Codex OAuth token")
    def test_codex_streaming(self):
        """Codex streaming should return SSE data lines with OpenAI delta format."""
        with httpx.Client() as client:
            with client.stream(
                "POST",
                f"{PROXY_BASE}/v1/chat/completions",
                headers={**AUTH_HEADERS, "Content-Type": "application/json"},
                json={
                    "model": "gpt-5.3-codex",
                    "messages": [{"role": "user", "content": "Say hello"}],
                    "stream": True,
                    "max_tokens": 32,
                },
                timeout=60.0,
            ) as resp:
                assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
                assert "text/event-stream" in resp.headers.get("content-type", "")

                lines = []
                for line in resp.iter_lines():
                    if line:
                        lines.append(line)

                data_lines = [l for l in lines if l.startswith("data: ")]
                assert len(data_lines) >= 2, f"Expected >=2 data lines, got {len(data_lines)}"

                # Verify delta format
                first_data = json.loads(data_lines[0][6:])
                assert first_data["object"] == "chat.completion.chunk"
                assert "delta" in first_data["choices"][0]

                # Verify finish_reason before [DONE]
                pre_done = [l for l in data_lines if l != "data: [DONE]"]
                last_chunk = json.loads(pre_done[-1][6:])
                assert last_chunk["choices"][0]["finish_reason"] is not None

                # Verify [DONE] sentinel
                assert data_lines[-1] == "data: [DONE]"

    @pytest.mark.skipif(not _PROXY_OK, reason="Proxy not reachable")
    @pytest.mark.skipif(not _codex_token_valid(), reason="No valid Codex OAuth token")
    def test_codex_response_format(self):
        """Response should have OpenAI-compatible format."""
        with httpx.Client() as client:
            r = _chat(client, "gpt-5.3-codex", "Say hi", max_tokens=32, timeout=60.0)
            assert r.status_code == 200
            data = r.json()
            assert "id" in data
            assert "choices" in data
            assert "usage" in data
            assert data["object"] == "chat.completion"


class TestTokenManagement:
    """Test OAuth token reading and validation."""

    def test_auth_file_exists(self):
        """auth.json should exist."""
        assert AUTH_FILE.exists(), f"Auth file not found: {AUTH_FILE}"

    def test_auth_file_readable(self):
        """auth.json should be valid JSON."""
        data = json.loads(AUTH_FILE.read_text())
        assert isinstance(data, dict)

    def test_anthropic_credentials_present(self):
        """Anthropic OAuth credentials should be in auth.json."""
        data = json.loads(AUTH_FILE.read_text())
        cred = data.get("anthropic")
        assert cred is not None, "No anthropic entry in auth.json"
        assert cred.get("type") == "oauth", f"Expected oauth type, got {cred.get('type')}"
        assert "access" in cred, "Missing access token"
        assert "refresh" in cred, "Missing refresh token"
        assert "expires" in cred, "Missing expires timestamp"

    @pytest.mark.skipif(not _PROXY_OK, reason="Proxy not reachable")
    def test_expired_token_returns_clear_error_or_success(self):
        """Expired tokens should return a clear error; valid tokens should succeed."""
        with httpx.Client() as client:
            r = _chat(client, "claude-sonnet-4-6", "hi", max_tokens=16, timeout=15.0)
            if _anthropic_token_valid():
                assert r.status_code == 200, f"Valid token should succeed: {r.status_code}"
            else:
                assert r.status_code in (401, 502, 500), f"Unexpected status: {r.status_code}"
