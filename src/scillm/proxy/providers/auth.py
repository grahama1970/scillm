"""OAuth credential management for Claude and Codex.

Reads tokens from ~/.pi/agent/auth.json (shared with Pi CLI).
Auto-refreshes expired tokens using the provider's refresh endpoint.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

# These directories are bind-mounted as directories in compose. Keeping the
# file constants derived from their parent directories makes the atomic-rename
# requirement explicit while preserving the standard CLI paths.
PI_AUTH_DIR = Path.home() / ".pi" / "agent"
CLAUDE_CODE_AUTH_DIR = Path.home() / ".claude"
CODEX_AUTH_DIR = Path.home() / ".codex"
OPENCODE_AUTH_DIR = Path.home() / ".local" / "share" / "opencode"

AUTH_FILE = PI_AUTH_DIR / "auth.json"
CLAUDE_CODE_CREDENTIALS = CLAUDE_CODE_AUTH_DIR / ".credentials.json"
CODEX_AUTH_FILE = CODEX_AUTH_DIR / "auth.json"
OPENCODE_AUTH_FILE = OPENCODE_AUTH_DIR / "auth.json"

AUTH_STALE_FILE_THRESHOLD_S = float(os.environ.get("SCILLM_AUTH_STALE_FILE_THRESHOLD_S", "21600"))
_LAST_REFRESH_OUTCOMES: dict[str, dict[str, Any]] = {}

# Anthropic OAuth
ANTHROPIC_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ANTHROPIC_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
ANTHROPIC_EXPIRY_BUFFER_MS = 5 * 60 * 1000  # 5-minute safety margin

# OpenAI Codex OAuth
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_EXPIRY_BUFFER_S = 5 * 60  # refresh before JWT exp when possible

_ANTHROPIC_REFRESH_CACHE: dict[str, Any] | None = None


@dataclass(frozen=True)
class _RefreshFailure:
    reason: str
    context: str
    http_status: int | None = None
    provider_error_code: str | None = None
    provider_error_body_classification: str = "ambiguous_error"

    @property
    def requires_human_login(self) -> bool:
        return self.provider_error_body_classification.startswith("login_required_")


def _http_failure(exc: httpx.HTTPStatusError) -> _RefreshFailure:
    """Preserve provider error context without exposing an unbounded body."""
    response = exc.response
    status = response.status_code
    provider_error_code: str | None = None
    provider_message: str | None = None

    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        payload = None

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            provider_error_code = error.get("code") or error.get("type")
            provider_message = error.get("message")
        elif isinstance(error, str):
            provider_error_code = error
        provider_error_code = provider_error_code or payload.get("code")
        provider_message = (
            provider_message
            or payload.get("error_description")
            or payload.get("message")
        )

    body_text = response.text[:500]
    classification_text = " ".join(
        part
        for part in (
            provider_error_code,
            provider_message,
            body_text,
        )
        if isinstance(part, str)
    ).lower()

    if "invalid_grant" in classification_text:
        body_classification = "login_required_invalid_grant"
    elif "revoked" in classification_text:
        body_classification = "login_required_revoked"
    elif any(
        marker in classification_text
        for marker in (
            "expired session",
            "session expired",
            "invalid refresh token",
            "refresh token is invalid",
            "refresh token has expired",
            "expired refresh token",
        )
    ):
        body_classification = "login_required_expired_or_invalid_session"
    elif status in {401, 403} and any(
        marker in classification_text
        for marker in (
            "auth",
            "unauthorized",
            "forbidden",
            "token",
            "credential",
            "login",
            "session",
        )
    ):
        body_classification = "login_required_http_auth_error"
    elif status >= 500:
        body_classification = "ambiguous_server_error"
    else:
        body_classification = "ambiguous_http_error"

    context = provider_message or provider_error_code or str(exc)
    return _RefreshFailure(
        reason="http_error",
        context=str(context)[:500],
        http_status=status,
        provider_error_code=provider_error_code,
        provider_error_body_classification=body_classification,
    )


def _refresh_exception_failure(exc: Exception) -> _RefreshFailure:
    if isinstance(exc, httpx.HTTPStatusError):
        return _http_failure(exc)
    if isinstance(exc, httpx.RequestError):
        return _RefreshFailure(
            reason="network_error",
            context=str(exc)[:500],
            provider_error_body_classification="ambiguous_network_error",
        )
    return _RefreshFailure(
        reason="refresh_error",
        context=f"{type(exc).__name__}: {exc}"[:500],
        provider_error_body_classification="ambiguous_refresh_error",
    )


def _credential_file_mtime_ms(path: Path) -> int | None:
    """Return the credential mtime visible to this process/container."""
    try:
        return int(path.stat().st_mtime * 1000)
    except OSError:
        return None


def _jwt_expiry_ms(access_token: str) -> int | None:
    """Extract an expiry from a JWT without treating unverified data as auth."""
    import base64

    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        expires_s = payload.get("exp")
        return int(expires_s * 1000) if expires_s is not None else None
    except Exception:
        return None


def _refresh_failure_details(
    provider: str,
    credential_file: Path,
    failure: _RefreshFailure | None,
) -> dict[str, Any]:
    """Classify a failed refresh, prioritizing definitive provider auth errors."""
    now_ms = int(time.time() * 1000)
    mtime_ms = _credential_file_mtime_ms(credential_file)
    age_s = max(0.0, (now_ms - mtime_ms) / 1000) if mtime_ms is not None else None
    stale = age_s is not None and age_s >= AUTH_STALE_FILE_THRESHOLD_S
    login_required = failure is not None and failure.requires_human_login

    if login_required:
        status = "needs_human_login"
        message = (
            f"{provider} OAuth refresh was rejected by the provider "
            f"({failure.provider_error_body_classification}). Run the provider CLI login on the "
            "host, then retry."
        )
    elif stale:
        status = "stale_bind_mount_suspected"
        message = (
            f"{provider} OAuth refresh failed and {credential_file} is {int(age_s)}s old in the "
            "container. Recreate/redeploy the scillm-proxy so its directory credential bind mount "
            "is applied, then retry. If refresh still fails with a current mtime, run the provider "
            "CLI login; do not assume login is required before remounting."
        )
    else:
        status = "needs_human_login"
        message = (
            f"{provider} OAuth refresh failed with a current or unavailable credential-file mtime. "
            "Run the provider CLI login on the host, then retry."
        )

    return {
        "outcome": "failed",
        "attempted_at_ms": now_ms,
        "credential_file": str(credential_file),
        "credential_file_mtime_ms": mtime_ms,
        "provider_auth_status": status,
        "project_agent_message": message,
        "failure_reason": failure.reason if failure is not None else "unknown_refresh_failure",
        "failure_context": failure.context if failure is not None else None,
        "http_status": failure.http_status if failure is not None else None,
        "provider_error_code": failure.provider_error_code if failure is not None else None,
        "provider_error_body_classification": (
            failure.provider_error_body_classification if failure is not None else "ambiguous_error"
        ),
    }


def _record_refresh_failure(
    provider: str,
    credential_file: Path,
    failure: _RefreshFailure | None,
) -> None:
    details = _refresh_failure_details(provider, credential_file, failure)
    _LAST_REFRESH_OUTCOMES[provider] = details
    logger.bind(provider=provider, **details).error("OAuth credential refresh failed")


def _record_refresh_success(provider: str, credential_file: Path) -> None:
    _LAST_REFRESH_OUTCOMES[provider] = {
        "outcome": "succeeded",
        "attempted_at_ms": int(time.time() * 1000),
        "credential_file": str(credential_file),
        "credential_file_mtime_ms": _credential_file_mtime_ms(credential_file),
        "provider_auth_status": "valid",
        "project_agent_message": None,
    }


def _missing_refresh_token_failure() -> _RefreshFailure:
    return _RefreshFailure(
        reason="missing_refresh_token",
        context="OAuth credential has no refresh token",
        provider_error_body_classification="ambiguous_missing_refresh_token",
    )


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


def _refresh_anthropic(cred: dict[str, Any]) -> dict[str, Any] | _RefreshFailure:
    """Refresh Anthropic OAuth token, preserving structured failure context."""
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
        failure = _refresh_exception_failure(exc)
        logger.error("Anthropic token refresh failed: {}", failure)
        return failure


def _refresh_codex(cred: dict[str, Any]) -> dict[str, Any] | _RefreshFailure:
    """Refresh OpenAI Codex OAuth token, preserving structured failure context."""
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
        failure = _refresh_exception_failure(exc)
        logger.error("Codex token refresh failed: {}", failure)
        return failure


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
        _record_refresh_failure("claude", CLAUDE_CODE_CREDENTIALS, _missing_refresh_token_failure())
        return None
    new_cred = _refresh_anthropic({
        "refresh": refresh_token,
        "access": access_token,
        "expires": cc_creds.get("expiresAt", 0),
    })
    if not isinstance(new_cred, dict):
        _record_refresh_failure(
            "claude",
            CLAUDE_CODE_CREDENTIALS,
            new_cred if isinstance(new_cred, _RefreshFailure) else None,
        )
        return None
    _cache_anthropic_refresh(new_cred)
    _write_claude_code_credentials(new_cred)
    _record_refresh_success("claude", CLAUDE_CODE_CREDENTIALS)
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
        _record_refresh_failure("codex", CODEX_AUTH_FILE, _missing_refresh_token_failure())
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
        _record_refresh_success("codex", CODEX_AUTH_FILE)
        return new_tokens
    except httpx.HTTPStatusError as exc:
        logger.error("Codex CLI token refresh failed: {}", exc)
        _record_refresh_failure("codex", CODEX_AUTH_FILE, _http_failure(exc))
        return None
    except Exception as exc:
        logger.error("Codex CLI token refresh failed: {}", exc)
        _record_refresh_failure("codex", CODEX_AUTH_FILE, _refresh_exception_failure(exc))
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
        if not cred.get("refresh"):
            _record_refresh_failure("claude", AUTH_FILE, _missing_refresh_token_failure())
            return None
        logger.info("Pi Anthropic token {}refreshing...", "force-" if force_refresh else "expired, ")
        new_cred = _refresh_anthropic(cred)
        if isinstance(new_cred, dict):
            _cache_anthropic_refresh(new_cred)
            auth_data["anthropic"] = new_cred
            _write_auth(auth_data)
            _record_refresh_success("claude", AUTH_FILE)
            return new_cred["access"]
        _record_refresh_failure("claude", AUTH_FILE, new_cred)
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
        if not cred.get("refresh"):
            _record_refresh_failure("codex", AUTH_FILE, _missing_refresh_token_failure())
            return None
        logger.info("Pi Codex token expired, refreshing...")
        new_cred = _refresh_codex(cred)
        if isinstance(new_cred, dict):
            auth_data["openai-codex"] = new_cred
            _write_auth(auth_data)
            _record_refresh_success("codex", AUTH_FILE)
            return new_cred["access"], new_cred.get("accountId", "")
        _record_refresh_failure("codex", AUTH_FILE, new_cred)
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


def _auth_file_report(path: Path, now_ms: int) -> dict[str, Any]:
    mtime_ms = _credential_file_mtime_ms(path)
    return {
        "credential_file": str(path),
        "credential_file_mtime_ms": mtime_ms,
        "credential_file_age_s": (
            max(0, (now_ms - mtime_ms) // 1000) if mtime_ms is not None else None
        ),
    }


def _provider_report(
    provider: str,
    credential_file: Path,
    token_expires_at_ms: int | None,
    now_ms: int,
    *,
    configured: bool,
) -> dict[str, Any]:
    if not configured:
        provider_status = "not_configured"
    elif token_expires_at_ms is None:
        provider_status = "configured"
    elif now_ms < token_expires_at_ms:
        provider_status = "valid"
    else:
        provider_status = "expired"

    report = {
        "status": provider_status,
        "provider_auth_status": provider_status,
        **_auth_file_report(credential_file, now_ms),
        "source": str(credential_file),
        "token_expires_at_ms": token_expires_at_ms,
        "expires_in_s": (
            max(0, (token_expires_at_ms - now_ms) // 1000)
            if token_expires_at_ms is not None
            else None
        ),
        "last_refresh_outcome": "not_attempted_since_process_start",
        "last_refresh_at_ms": None,
        "last_refresh_failure_reason": None,
        "last_refresh_failure_context": None,
        "last_refresh_http_status": None,
        "last_refresh_provider_error_code": None,
        "last_refresh_error_body_classification": None,
    }

    last_refresh = _LAST_REFRESH_OUTCOMES.get(provider)
    if not last_refresh:
        return report

    report["last_refresh_outcome"] = last_refresh["outcome"]
    report["last_refresh_at_ms"] = last_refresh["attempted_at_ms"]
    report["last_refresh_failure_reason"] = last_refresh.get("failure_reason")
    report["last_refresh_failure_context"] = last_refresh.get("failure_context")
    report["last_refresh_http_status"] = last_refresh.get("http_status")
    report["last_refresh_provider_error_code"] = last_refresh.get("provider_error_code")
    report["last_refresh_error_body_classification"] = last_refresh.get(
        "provider_error_body_classification"
    )
    # A prior failure is current only while the provider is still unusable and
    # the file has the same container-view mtime. A host-side rotation therefore
    # clears the warning instead of masking a newly valid credential.
    same_file_version = (
        str(credential_file) == last_refresh.get("credential_file")
        and report["credential_file_mtime_ms"] == last_refresh.get("credential_file_mtime_ms")
    )
    if provider_status in {"expired", "not_configured"} and same_file_version:
        report["provider_auth_status"] = last_refresh["provider_auth_status"]
        report["project_agent_message"] = last_refresh["project_agent_message"]
    return report


def get_auth_status_snapshot(now_ms: int | None = None) -> dict[str, Any]:
    """Return per-provider token, file, and refresh diagnostics."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    pi_data = _read_auth()

    claude = _read_claude_code_credentials()
    claude_file = CLAUDE_CODE_CREDENTIALS
    claude_expiry = claude.get("expiresAt") if claude else None
    if not claude:
        pi_claude = pi_data.get("anthropic", {})
        if pi_claude.get("type") == "oauth":
            claude = pi_claude
            claude_file = AUTH_FILE
            claude_expiry = pi_claude.get("expires")
    claude_report = _provider_report(
        "claude", claude_file, claude_expiry, now_ms, configured=bool(claude)
    )
    if claude and claude_file == CLAUDE_CODE_CREDENTIALS:
        claude_report["subscription"] = claude.get("subscriptionType", "unknown")
        claude_report["rate_tier"] = claude.get("rateLimitTier", "unknown")

    codex = _read_codex_auth()
    codex_file = CODEX_AUTH_FILE
    codex_expiry = _jwt_expiry_ms(codex["access_token"]) if codex else None
    if not codex:
        pi_codex = pi_data.get("openai-codex", {})
        if pi_codex.get("type") == "oauth":
            codex = pi_codex
            codex_file = AUTH_FILE
            codex_expiry = pi_codex.get("expires")
    codex_report = _provider_report(
        "codex", codex_file, codex_expiry, now_ms, configured=bool(codex)
    )
    if codex and codex_file == CODEX_AUTH_FILE:
        codex_report["account_id"] = (codex.get("account_id") or "")[:12] + "..."

    opencode_report = _provider_report(
        "opencode",
        OPENCODE_AUTH_FILE,
        None,
        now_ms,
        configured=OPENCODE_AUTH_FILE.is_file(),
    )
    opencode_report["last_refresh_outcome"] = "managed_by_opencode_cli"

    return {
        "timestamp": now_ms,
        "staleness_threshold_s": AUTH_STALE_FILE_THRESHOLD_S,
        "claude": claude_report,
        "codex": codex_report,
        "opencode": opencode_report,
    }
