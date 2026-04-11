#!/usr/bin/env python3
"""Refresh OAuth tokens in Bifrost via HTTP API.

Runs in a loop, refreshing Claude and Codex OAuth tokens every 55 minutes.
Uses Bifrost's PUT /api/providers/{provider} endpoint for zero-downtime updates.

Designed to run as a supervisord program alongside Bifrost and scillm.
"""

import json
import os
import sys
import time
from pathlib import Path

import httpx

BIFROST_URL = "http://127.0.0.1:4002"
REFRESH_INTERVAL_S = 55 * 60  # 55 minutes (tokens typically expire in 60)
STARTUP_DELAY_S = 60  # Wait for Bifrost to be ready


def read_claude_oauth_token() -> str:
    """Read Claude OAuth access token from credential file."""
    paths = [
        Path("/root/.claude/.credentials.json"),
        Path.home() / ".claude" / ".credentials.json",
    ]
    for path in paths:
        try:
            creds = json.loads(path.read_text())
            token = creds.get("claudeAiOauth", {}).get("accessToken", "")
            if token:
                print(f"  Claude OAuth: loaded from {path}")
                return token
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return ""


def read_codex_oauth_token() -> tuple[str, str]:
    """Read Codex OAuth (access_token, account_id) from credential file."""
    paths = [
        Path("/root/.codex/auth.json"),
        Path.home() / ".codex" / "auth.json",
    ]
    for path in paths:
        try:
            creds = json.loads(path.read_text())
            tokens = creds.get("tokens", {})
            token = tokens.get("access_token", creds.get("access_token", creds.get("token", "")))
            account_id = creds.get("account_id", creds.get("chatgpt_account_id", ""))
            if token:
                print(f"  Codex OAuth: loaded from {path}")
                return token, account_id
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return "", ""


def update_provider_key(client: httpx.Client, provider: str, key_name: str, key_value: str) -> bool:
    """Update a provider key via Bifrost API.

    Returns True on success, False on failure.
    """
    try:
        # Get list of all providers to find the one we want
        resp = client.get(f"{BIFROST_URL}/api/providers")
        if resp.status_code != 200:
            print(f"  {provider}: GET providers failed: {resp.status_code}")
            return False

        providers_data = resp.json()
        providers_list = providers_data.get("providers", [])

        # Find the provider by name
        target_provider = None
        for p in providers_list:
            if p.get("name") == provider:
                target_provider = p
                break

        if not target_provider:
            print(f"  {provider}: provider not configured in Bifrost, skipping")
            return True  # Not an error, just not configured

        # Update the key in the provider config
        keys = target_provider.get("keys", [])
        updated = False
        for key in keys:
            if key.get("name") == key_name:
                # Bifrost stores value as nested object: {"value": "...", "env_var": "", "from_env": false}
                if isinstance(key.get("value"), dict):
                    key["value"]["value"] = key_value
                else:
                    key["value"] = {"value": key_value, "env_var": "", "from_env": False}
                updated = True
                break

        if not updated:
            print(f"  {provider}/{key_name}: key not found in provider config")
            return True  # Not an error if key doesn't exist

        # PUT updated config
        resp = client.put(f"{BIFROST_URL}/api/providers/{provider}", json=target_provider)
        if resp.status_code != 200:
            print(f"  {provider}: PUT failed: {resp.status_code} {resp.text[:200]}")
            return False

        print(f"  {provider}/{key_name}: updated successfully")
        return True

    except httpx.RequestError as e:
        print(f"  {provider}: request error: {e}")
        return False


def refresh_tokens() -> dict:
    """Refresh all OAuth tokens in Bifrost.

    Returns dict of {provider: success_bool}.
    """
    results = {}

    with httpx.Client(timeout=10.0) as client:
        # Check Bifrost is up
        try:
            resp = client.get(f"{BIFROST_URL}/api/version")
            if resp.status_code != 200:
                print(f"Bifrost not ready: {resp.status_code}")
                return {"bifrost": False}
        except httpx.RequestError as e:
            print(f"Bifrost not reachable: {e}")
            return {"bifrost": False}

        # Refresh Claude OAuth
        claude_token = read_claude_oauth_token()
        if claude_token:
            results["anthropic"] = update_provider_key(
                client, "anthropic", "claude-oauth", claude_token
            )
        else:
            print("  Claude OAuth: no token found")
            results["anthropic"] = True  # Not an error if not configured

        # Refresh Codex OAuth
        codex_token, _ = read_codex_oauth_token()
        if codex_token:
            results["openai"] = update_provider_key(
                client, "openai", "codex-oauth", codex_token
            )
        else:
            print("  Codex OAuth: no token found")
            results["openai"] = True  # Not an error if not configured

    return results


def main():
    print(f"Token refresh service starting (interval: {REFRESH_INTERVAL_S}s)")
    print(f"Waiting {STARTUP_DELAY_S}s for Bifrost startup...")
    time.sleep(STARTUP_DELAY_S)

    while True:
        print(f"\n=== Token refresh: {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
        results = refresh_tokens()

        success = all(results.values())
        if success:
            print("All tokens refreshed successfully")
        else:
            failed = [k for k, v in results.items() if not v]
            print(f"Token refresh failed for: {failed}")

        print(f"Next refresh in {REFRESH_INTERVAL_S // 60} minutes")
        time.sleep(REFRESH_INTERVAL_S)


if __name__ == "__main__":
    main()
