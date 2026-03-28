"""OAuth credential management for Claude and Codex.

Reads tokens from ~/.pi/agent/auth.json (shared with Pi CLI).
Auto-refreshes expired tokens using the provider's refresh endpoint.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

AUTH_FILE = Path.home() / ".pi" / "agent" / "auth.json"
CLAUDE_CODE_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"
CODEX_AUTH_FILE = Path.home() / ".codex" / "auth.json"

# Anthropic OAuth
ANTHROPIC_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ANTHROPIC_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
ANTHROPIC_EXPIRY_BUFFER_MS = 5 * 60 * 1000  # 5-minute safety margin

# OpenAI Codex OAuth
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"


def _read_auth() -> dict[str, Any]:
    """Read auth.json, returning empty dict if missing."""
    if not AUTH_FILE.exists():
        return {}
    try:
        return json.loads(AUTH_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read auth.json: {}", exc)
        return {}


def _write_auth(data: dict[str, Any]) -> None:
    """Write updated auth data back to auth.json."""
    try:
        AUTH_FILE.write_text(json.dumps(data, indent=2))
    except OSError as exc:
        logger.warning("Failed to write auth.json: {}", exc)


def _refresh_anthropic(cred: dict[str, Any]) -> dict[str, Any] | None:
    """Refresh Anthropic OAuth token. Returns updated credential or None."""
    try:
        resp = httpx.post(
            ANTHROPIC_TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "client_id": ANTHROPIC_CLIENT_ID,
                "refresh_token": cred["refresh"],
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        expires_in = data.get("expires_in", 3600)
        new_cred = {
            "type": "oauth",
            "refresh": data.get("refresh_token", cred["refresh"]),
            "access": data["access_token"],
            "expires": int(time.time() * 1000) + expires_in * 1000 - ANTHROPIC_EXPIRY_BUFFER_MS,
        }
        logger.info("Refreshed Anthropic OAuth token (expires in {}s)", expires_in)
        return new_cred
    except Exception as exc:
        logger.error("Anthropic token refresh failed: {}", exc)
        return None


def _refresh_codex(cred: dict[str, Any]) -> dict[str, Any] | None:
    """Refresh OpenAI Codex OAuth token. Returns updated credential or None."""
    try:
        resp = httpx.post(
            CODEX_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": cred["refresh"],
                "client_id": CODEX_CLIENT_ID,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        expires_in = data.get("expires_in", 3600)
        access_token = data["access_token"]

        # Extract account ID from JWT
        account_id = _extract_codex_account_id(access_token)

        new_cred = {
            "type": "oauth",
            "refresh": data.get("refresh_token", cred["refresh"]),
            "access": access_token,
            "expires": int(time.time() * 1000) + expires_in * 1000,
            "accountId": account_id or cred.get("accountId"),
        }
        logger.info("Refreshed Codex OAuth token (expires in {}s)", expires_in)
        return new_cred
    except Exception as exc:
        logger.error("Codex token refresh failed: {}", exc)
        return None


def _extract_codex_account_id(access_token: str) -> str | None:
    """Extract chatgpt_account_id from JWT payload."""
    import base64

    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return None
        # JWT payload is base64url-encoded
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
    except Exception:
        return None


def _read_claude_code_credentials() -> dict[str, Any] | None:
    """Read Claude Code's own OAuth credentials from ~/.claude/.credentials.json."""
    if not CLAUDE_CODE_CREDENTIALS.exists():
        return None
    try:
        data = json.loads(CLAUDE_CODE_CREDENTIALS.read_text())
        oauth = data.get("claudeAiOauth")
        if not oauth or not oauth.get("accessToken"):
            return None
        return oauth
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read Claude Code credentials: {}", exc)
        return None


def get_anthropic_token() -> str | None:
    """Get a valid Anthropic OAuth access token.

    Priority: Claude Code credentials (~/.claude) > Pi auth (~/.pi/agent).
    Auto-refreshes expired tokens.
    """
    # Try Claude Code credentials first (always freshest — managed by the running CLI)
    cc_creds = _read_claude_code_credentials()
    if cc_creds:
        now_ms = int(time.time() * 1000)
        expires = cc_creds.get("expiresAt", 0)
        if now_ms < expires:
            logger.debug("Using Claude Code OAuth token (expires in {}s)", (expires - now_ms) // 1000)
            return cc_creds["accessToken"]
        # Claude Code token expired — try to refresh it
        logger.info("Claude Code token expired, refreshing...")
        new_cred = _refresh_anthropic({
            "refresh": cc_creds["refreshToken"],
            "access": cc_creds["accessToken"],
            "expires": expires,
        })
        if new_cred:
            # Write back to Claude Code credentials format
            try:
                data = json.loads(CLAUDE_CODE_CREDENTIALS.read_text())
                data["claudeAiOauth"]["accessToken"] = new_cred["access"]
                data["claudeAiOauth"]["refreshToken"] = new_cred["refresh"]
                data["claudeAiOauth"]["expiresAt"] = new_cred["expires"]
                CLAUDE_CODE_CREDENTIALS.write_text(json.dumps(data, indent=2))
                logger.info("Updated Claude Code credentials after refresh")
            except OSError as exc:
                logger.warning("Could not update Claude Code credentials: {}", exc)
            return new_cred["access"]

    # Fall back to Pi auth.json
    auth_data = _read_auth()
    cred = auth_data.get("anthropic")
    if not cred or cred.get("type") != "oauth":
        return None

    now_ms = int(time.time() * 1000)
    if now_ms >= cred.get("expires", 0):
        logger.info("Pi Anthropic token expired, refreshing...")
        new_cred = _refresh_anthropic(cred)
        if new_cred:
            auth_data["anthropic"] = new_cred
            _write_auth(auth_data)
            return new_cred["access"]
        return None

    return cred["access"]


def _read_codex_auth() -> dict[str, Any] | None:
    """Read Codex CLI's own auth from ~/.codex/auth.json."""
    if not CODEX_AUTH_FILE.exists():
        return None
    try:
        data = json.loads(CODEX_AUTH_FILE.read_text())
        tokens = data.get("tokens")
        if not tokens or not tokens.get("access_token"):
            return None
        return tokens
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read Codex auth.json: {}", exc)
        return None


def get_codex_credentials() -> tuple[str, str] | None:
    """Get (access_token, account_id) for Codex.

    Priority: Codex CLI auth (~/.codex) > Pi auth (~/.pi/agent).
    Auto-refreshes expired tokens.
    """
    # Try Codex CLI credentials first (always freshest)
    codex_tokens = _read_codex_auth()
    if codex_tokens:
        access = codex_tokens["access_token"]
        account_id = codex_tokens.get("account_id") or _extract_codex_account_id(access) or ""
        # Codex CLI manages its own refresh — we can try the token and let it fail
        # if expired, then refresh ourselves
        logger.debug("Using Codex CLI OAuth token (account={}...)", account_id[:8] if account_id else "?")
        return access, account_id

    # Fall back to Pi auth.json
    auth_data = _read_auth()
    cred = auth_data.get("openai-codex")
    if not cred or cred.get("type") != "oauth":
        return None

    now_ms = int(time.time() * 1000)
    if now_ms >= cred.get("expires", 0):
        logger.info("Pi Codex token expired, refreshing...")
        new_cred = _refresh_codex(cred)
        if new_cred:
            auth_data["openai-codex"] = new_cred
            _write_auth(auth_data)
            return new_cred["access"], new_cred.get("accountId", "")
        return None

    account_id = cred.get("accountId") or _extract_codex_account_id(cred["access"]) or ""
    return cred["access"], account_id


def is_anthropic_available() -> bool:
    """Check if Anthropic OAuth credentials exist (Claude Code or Pi)."""
    # Claude Code credentials
    cc_creds = _read_claude_code_credentials()
    if cc_creds and cc_creds.get("accessToken"):
        return True
    # Pi auth
    auth_data = _read_auth()
    cred = auth_data.get("anthropic")
    return cred is not None and cred.get("type") == "oauth"


def is_codex_available() -> bool:
    """Check if Codex OAuth credentials exist (Codex CLI or Pi)."""
    codex_tokens = _read_codex_auth()
    if codex_tokens and codex_tokens.get("access_token"):
        return True
    auth_data = _read_auth()
    cred = auth_data.get("openai-codex")
    return cred is not None and cred.get("type") == "oauth"
