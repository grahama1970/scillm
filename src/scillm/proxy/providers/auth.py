"""OAuth credential management for Claude and Codex.

Reads tokens from ~/.pi/agent/auth.json (shared with Pi CLI).
Auto-refreshes expired tokens using the provider's refresh endpoint.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
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
CODEX_EXPIRY_BUFFER_S = 5 * 60  # refresh before JWT exp when possible

_ANTHROPIC_REFRESH_CACHE: dict[str, Any] | None = None


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


def _cache_anthropic_refresh(new_cred: dict[str, Any]) -> None:
    global _ANTHROPIC_REFRESH_CACHE
    _ANTHROPIC_REFRESH_CACHE = dict(new_cred)


def _anthropic_cached_token_valid(now_ms: int) -> str | None:
    if not _ANTHROPIC_REFRESH_CACHE:
        return None
    access = _ANTHROPIC_REFRESH_CACHE.get("access")
    expires = int(_ANTHROPIC_REFRESH_CACHE.get("expires") or 0)
    if access and now_ms < expires:
        logger.debug(
            "Using cached Anthropic OAuth token refreshed by SciLLM (expires in {}s)",
            (expires - now_ms) // 1000,
        )
        return str(access)
    return None


def _write_claude_code_credentials(new_cred: dict[str, Any]) -> None:
    try:
        data = json.loads(CLAUDE_CODE_CREDENTIALS.read_text())
        data["claudeAiOauth"]["accessToken"] = new_cred["access"]
        data["claudeAiOauth"]["refreshToken"] = new_cred["refresh"]
        data["claudeAiOauth"]["expiresAt"] = new_cred["expires"]
        CLAUDE_CODE_CREDENTIALS.write_text(json.dumps(data, indent=2))
        logger.info("Updated Claude Code credentials after refresh")
    except (json.JSONDecodeError, OSError, KeyError) as exc:
        logger.warning(
            "Could not update Claude Code credentials after Anthropic refresh; "
            "using in-process refreshed token until restart: {}",
            exc,
        )


def _refresh_claude_code_token(cc_creds: dict[str, Any]) -> str | None:
    refresh_token = cc_creds.get("refreshToken")
    access_token = cc_creds.get("accessToken")
    if not refresh_token:
        logger.error("Claude Code credentials missing refreshToken; run `claude auth login --claudeai`")
        return None
    new_cred = _refresh_anthropic({
        "refresh": refresh_token,
        "access": access_token,
        "expires": cc_creds.get("expiresAt", 0),
    })
    if not new_cred:
        return None
    _cache_anthropic_refresh(new_cred)
    _write_claude_code_credentials(new_cred)
    return str(new_cred["access"])


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




def _jwt_exp_unix(access_token: str) -> int | None:
    """Return JWT ``exp`` claim as unix seconds, if present."""
    import base64

    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        return int(exp) if exp is not None else None
    except Exception:
        return None


def _write_codex_auth_file(data: dict[str, Any]) -> None:
    """Persist Codex CLI auth.json (requires a writable bind mount in Docker)."""
    try:
        CODEX_AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        CODEX_AUTH_FILE.write_text(json.dumps(data, indent=2))
    except OSError as exc:
        logger.warning("Could not write Codex auth.json: {}", exc)


def _refresh_codex_cli_tokens(tokens: dict[str, Any]) -> dict[str, Any] | None:
    """Refresh Codex CLI ``~/.codex/auth.json`` tokens using ``refresh_token``."""
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        logger.error("Codex auth.json missing refresh_token; run `codex login` on the host")
        return None
    try:
        resp = httpx.post(
            CODEX_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CODEX_CLIENT_ID,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        access_token = data["access_token"]
        account_id = (
            tokens.get("account_id")
            or _extract_codex_account_id(access_token)
            or ""
        )
        new_tokens = {
            "access_token": access_token,
            "refresh_token": data.get("refresh_token", refresh_token),
            "account_id": account_id,
        }
        if data.get("id_token"):
            new_tokens["id_token"] = data["id_token"]
        elif tokens.get("id_token"):
            new_tokens["id_token"] = tokens["id_token"]

        auth_doc = json.loads(CODEX_AUTH_FILE.read_text()) if CODEX_AUTH_FILE.exists() else {}
        auth_doc["tokens"] = new_tokens
        auth_doc["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _write_codex_auth_file(auth_doc)
        logger.info("Refreshed Codex CLI OAuth token (account={}...)", account_id[:8] if account_id else "?")
        return new_tokens
    except Exception as exc:
        logger.error("Codex CLI token refresh failed: {}", exc)
        return None


def _ensure_codex_cli_tokens_fresh(tokens: dict[str, Any], *, force: bool = False) -> dict[str, Any] | None:
    """Refresh Codex CLI tokens when JWT is near expiry or ``force`` is set."""
    access = tokens.get("access_token")
    if not access:
        return None
    exp = _jwt_exp_unix(access)
    now = int(time.time())
    if not force and exp is not None and now + CODEX_EXPIRY_BUFFER_S < exp:
        return tokens
    return _refresh_codex_cli_tokens(tokens)


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


def get_anthropic_token(*, force_refresh: bool = False) -> str | None:
    """Get a valid Anthropic OAuth access token.

    Priority: Claude Code credentials (~/.claude) > Pi auth (~/.pi/agent).
    Auto-refreshes expired tokens. Use ``force_refresh`` after a provider 401
    because Anthropic can revoke an otherwise unexpired access token.
    """
    now_ms = int(time.time() * 1000)
    if not force_refresh:
        cached = _anthropic_cached_token_valid(now_ms)
        if cached:
            return cached

    # Try Claude Code credentials first (always freshest — managed by the running CLI)
    cc_creds = _read_claude_code_credentials()
    if cc_creds:
        expires = cc_creds.get("expiresAt", 0)
        if not force_refresh and now_ms < expires:
            logger.debug("Using Claude Code OAuth token (expires in {}s)", (expires - now_ms) // 1000)
            return cc_creds["accessToken"]
        logger.info("Claude Code token {}refreshing...", "force-" if force_refresh else "expired, ")
        refreshed = _refresh_claude_code_token(cc_creds)
        if refreshed:
            return refreshed
        if force_refresh:
            return None

    # Fall back to Pi auth.json
    auth_data = _read_auth()
    cred = auth_data.get("anthropic")
    if not cred or cred.get("type") != "oauth":
        return None

    if force_refresh or now_ms >= cred.get("expires", 0):
        logger.info("Pi Anthropic token {}refreshing...", "force-" if force_refresh else "expired, ")
        new_cred = _refresh_anthropic(cred)
        if new_cred:
            _cache_anthropic_refresh(new_cred)
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


def get_codex_credentials(*, force_refresh: bool = False) -> tuple[str, str] | None:
    """Get (access_token, account_id) for Codex.

    Priority: Codex CLI auth (~/.codex) > Pi auth (~/.pi/agent).
    Auto-refreshes expired or near-expiry tokens; use ``force_refresh`` after 401.
    """
    codex_tokens = _read_codex_auth()
    if codex_tokens:
        fresh = _ensure_codex_cli_tokens_fresh(codex_tokens, force=force_refresh)
        if not fresh:
            return None
        access = fresh["access_token"]
        account_id = fresh.get("account_id") or _extract_codex_account_id(access) or ""
        logger.debug("Using Codex CLI OAuth token (account={}...)", account_id[:8] if account_id else "?")
        return access, account_id

    auth_data = _read_auth()
    cred = auth_data.get("openai-codex")
    if not cred or cred.get("type") != "oauth":
        return None

    now_ms = int(time.time() * 1000)
    if force_refresh or now_ms >= cred.get("expires", 0):
        logger.info("Pi Codex token expired, refreshing...")
        new_cred = _refresh_codex(cred)
        if new_cred:
            auth_data["openai-codex"] = new_cred
            _write_auth(auth_data)
            return new_cred["access"], new_cred.get("accountId", "")
        return None

    account_id = cred.get("accountId") or _extract_codex_account_id(cred["access"]) or ""
    return cred["access"], account_id


def inspect_codex_auth() -> dict[str, Any]:
    """Summarize Codex OAuth health for /v1/scillm/auth (no secrets)."""
    now = int(time.time())
    codex_tokens = _read_codex_auth()
    if codex_tokens:
        access = codex_tokens.get("access_token", "")
        exp = _jwt_exp_unix(access) if access else None
        remaining_s = max(0, (exp - now)) if exp is not None else None
        auth_doc: dict[str, Any] = {}
        if CODEX_AUTH_FILE.exists():
            try:
                auth_doc = json.loads(CODEX_AUTH_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                auth_doc = {}
        status = "valid"
        if not access:
            status = "not_configured"
        elif exp is not None and now >= exp:
            status = "expired"
        return {
            "status": status,
            "source": str(CODEX_AUTH_FILE),
            "account_id": (codex_tokens.get("account_id") or "")[:12] + "...",
            "expires_in_s": remaining_s,
            "last_refresh": auth_doc.get("last_refresh"),
            "refresh_available": bool(codex_tokens.get("refresh_token")),
        }

    auth_data = _read_auth()
    cred = auth_data.get("openai-codex", {})
    if cred.get("type") == "oauth":
        expires_ms = cred.get("expires", 0)
        remaining_s = max(0, (expires_ms - int(time.time() * 1000)) // 1000)
        return {
            "status": "valid" if int(time.time() * 1000) < expires_ms else "expired",
            "source": str(AUTH_FILE),
            "expires_in_s": remaining_s,
            "refresh_available": bool(cred.get("refresh")),
        }
    return {"status": "not_configured"}


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
