# SPDX-License-Identifier: MIT
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Optional, Dict, Any


def _codex_home() -> Path:
    env = os.getenv("CODEX_HOME")
    if env and env.strip():
        return Path(env).expanduser().resolve()
    return Path.home() / ".codex"


def load_auth_json() -> Dict[str, Any]:
    """
    Reads CODEX_HOME/auth.json (default ~/.codex/auth.json).
    Supports tokens.access_token/account_id as well as legacy shapes.
    """
    auth_path = _codex_home() / "auth.json"
    if not auth_path.exists():
        return {}
    try:
        return json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _decode_chatgpt_account_id(jwt_token: str) -> Optional[str]:
    """
    Parse a JWT and extract https://api.openai.com/auth.chatgpt_account_id if present.
    Robust base64url padding and tolerant claim path.
    """
    try:
        parts = jwt_token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1]
        pad = (-len(payload_b64)) % 4
        payload = base64.urlsafe_b64decode(payload_b64 + ("=" * pad))
        obj = json.loads(payload.decode("utf-8", errors="ignore"))
        auth = obj.get("https://api.openai.com/auth") or obj.get("auth") or {}
        val = auth.get("chatgpt_account_id") or auth.get("chatgptAccountId")
        return val.strip() if isinstance(val, str) and val.strip() else None
    except Exception:
        return None


def build_auth_headers(user_agent: str = "scillm-codex-sdk/0.1") -> Dict[str, str]:
    """
    Construct headers for Codex Cloud:
      - Authorization: Bearer <token>
      - ChatGPT-Account-Id: <id> (if available or derivable from JWT)
      - User-Agent: <user_agent>
    Priority for token:
      1) OPENAI_API_KEY
      2) tokens.access_token in auth.json
    """
    headers: Dict[str, str] = {
        "User-Agent": user_agent,
    }

    # Accept multiple env names; also allow CODEX_CLOUD_TOKEN (generic)
    token = (
        os.getenv("CODEX_CLOUD_TOKEN")
        or os.getenv("CODEX_CLOUD_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    ).strip()
    account_id: Optional[str] = None

    if not token:
        auth = load_auth_json()
        tokens = auth.get("tokens") or {}
        token = (tokens.get("access_token") or tokens.get("accessToken") or "").strip()
        account_id = (tokens.get("account_id") or tokens.get("accountId") or None)
        if isinstance(account_id, str):
            account_id = account_id.strip() or None

    if token:
        headers["Authorization"] = f"Bearer {token}"
        if not account_id:
            account_id = _decode_chatgpt_account_id(token)

    if account_id:
        headers["ChatGPT-Account-Id"] = account_id

    return headers


def get_bearer_token(explicit: Optional[str] = None) -> Optional[str]:
    """Compatibility helper: return a bearer token or None.
    Priority: explicit > CODEX_CLOUD_TOKEN > OPENAI_API_KEY > auth.json
    """
    if explicit and explicit.strip():
        return explicit.strip()
    env = (
        os.getenv("CODEX_CLOUD_TOKEN")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("CODEX_CLOUD_API_KEY")
    )
    if env and env.strip():
        return env.strip()
    auth = load_auth_json()
    tokens = auth.get("tokens") or auth
    for k in ("access_token", "token", "bearer", "api_key", "accessToken"):
        v = tokens.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def ensure_token_or_warn(token: Optional[str]) -> None:
    echo = os.getenv("CODEX_CLOUD_ECHO")
    if not token and not (echo and echo not in ("0", "false", "False", "")):
        try:
            print(
                "WARNING: No Codex Cloud token found (CODEX_CLOUD_TOKEN/OPENAI_API_KEY or ~/.codex/auth.json).",
                flush=True,
            )
        except Exception:
            pass
