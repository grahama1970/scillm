"""Generate bifrost.json from proxy_server_config.yaml at container startup.

Single source of truth: proxy_server_config.yaml defines models and providers.
This script reads it and produces the Bifrost config with resolved env vars
and explicit model lists per provider.
"""

import json
import os
import re
import sys

import yaml


# Map scillm api_base patterns to Bifrost provider names
_PROVIDER_MAP = [
    ("generativelanguage.googleapis.com", "gemini", None),
    ("api.deepseek.com", "deepseek", "openai"),
    ("llm.chutes.ai", "chutes", "openai"),
    ("api.z.ai", "glm", "openai"),
    ("api.moonshot.ai", "moonshot", "openai"),
    ("openrouter.ai", "openrouter", "openai"),
    ("11434", "ollama", None),
    ("127.0.0.1:11434", "ollama", None),
]

# OAuth providers that need special handling.
# Claude OAuth: route through Bifrost's native anthropic provider with token injection.
# Codex OAuth: skip — Responses API not supported by Bifrost.
_SKIP_PROVIDERS = {"codex-oauth"}

# Bifrost shell config (static parts)
_BIFROST_BASE = {
    "$schema": "https://www.getbifrost.ai/schema",
    "client": {
        "allow_direct_keys": True,
        "allowed_origins": ["*"],
        "drop_excess_requests": False,
        "enable_logging": True,
        "enforce_auth_on_inference": False,
        "initial_pool_size": 100,
        "max_request_body_size_mb": 50,
    },
    "config_store": {
        "enabled": True,
        "type": "sqlite",
        "config": {"path": "/app/bifrost/data/config.db"},
    },
    "logs_store": {
        "enabled": True,
        "type": "sqlite",
        "config": {"path": "/app/bifrost/data/logs.db"},
    },
    "auth_config": {"is_enabled": False, "disable_auth_on_inference": True},
    "plugins": [],
    "framework": {"pricing": {"pricing_sync_interval": 86400}},
}


_CLAUDE_CRED_PATHS = [
    "/root/.claude/.credentials.json",       # Docker mount
    os.path.expanduser("~/.claude/.credentials.json"),  # Host
]


def _read_claude_oauth_token() -> str:
    """Read Claude OAuth access token from credential file."""
    for path in _CLAUDE_CRED_PATHS:
        try:
            with open(path) as f:
                creds = json.load(f)
            token = creds.get("claudeAiOauth", {}).get("accessToken", "")
            if token:
                print(f"  Claude OAuth token from {path}")
                return token
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return ""


def _resolve_env(val: str) -> str:
    """Resolve 'os.environ/VAR' or 'env.VAR' to actual env var value."""
    if val.startswith("os.environ/"):
        var = val[len("os.environ/"):]
        return os.environ.get(var, "")
    if val.startswith("env."):
        var = val[4:]
        return os.environ.get(var, "")
    return val


def _detect_provider(api_base: str) -> tuple[str, str | None]:
    """Return (bifrost_provider_name, custom_base_type_or_None)."""
    for pattern, provider, base_type in _PROVIDER_MAP:
        if pattern in api_base:
            return provider, base_type
    return "", None


def generate(scillm_config_path: str, output_path: str) -> None:
    with open(scillm_config_path) as f:
        cfg = yaml.safe_load(f)

    model_list = cfg.get("model_list", [])

    # Collect per-provider: {provider: {key_value: {name, models set, network_config}}}
    providers: dict[str, dict] = {}

    for entry in model_list:
        params = entry.get("scillm_params", {})
        model = params.get("model", "")
        api_base = params.get("api_base", "")
        api_key_raw = params.get("api_key", "")
        custom_llm = params.get("custom_llm_provider", "")

        # Codex OAuth: skip — Bifrost doesn't support Responses API
        if custom_llm in _SKIP_PROVIDERS:
            continue

        # Claude OAuth: add to Bifrost's native anthropic provider with token from cred file
        if custom_llm == "anthropic-oauth" or (api_key_raw == "oauth" and "anthropic" in api_base):
            token = _read_claude_oauth_token()
            if token:
                if "anthropic" not in providers:
                    providers["anthropic"] = {}
                if token not in providers["anthropic"]:
                    providers["anthropic"][token] = {
                        "name": "claude-oauth",
                        "value": token,
                        "models": set(),
                        "weight": 1.0,
                        "base_type": None,
                        "network_config": {},
                    }
                providers["anthropic"][token]["models"].add(model)
            else:
                print(f"  SKIP: no Claude OAuth token for {model}")
            continue

        # Skip other oauth providers we don't handle
        if api_key_raw == "oauth":
            continue

        # Resolve env var references in api_base before provider detection
        api_base = _resolve_env(api_base)

        provider, base_type = _detect_provider(api_base)
        if not provider:
            print(f"  SKIP: no Bifrost provider for api_base={api_base} model={model}")
            continue

        api_key = _resolve_env(api_key_raw)
        if not api_key:
            print(f"  SKIP: empty key for {provider}/{model}")
            continue

        if provider not in providers:
            providers[provider] = {}

        # Group by resolved key value (same key = same Bifrost key entry)
        if api_key not in providers[provider]:
            # Determine key name from the raw env var reference
            if api_key_raw.startswith("os.environ/"):
                key_name = api_key_raw[len("os.environ/"):].lower()
            else:
                key_name = f"{provider}-key"

            providers[provider][api_key] = {
                "name": key_name,
                "value": api_key,
                "models": set(),
                "weight": 1.0,
                "base_type": base_type,
                "network_config": {},
            }

            # Set network_config for custom providers
            if base_type == "openai" and api_base:
                # Strip /v1 suffix if present — Bifrost adds it
                base = api_base.rstrip("/")
                if base.endswith("/v1"):
                    base = base[:-3]
                # For Gemini, the base_url is different — Bifrost handles natively
                if provider != "gemini":
                    providers[provider][api_key]["network_config"] = {
                        "base_url": base,
                        "default_request_timeout_in_seconds": int(params.get("timeout", 60)),
                        "max_retries": 0,
                    }

        providers[provider][api_key]["models"].add(model)

    # Also add alias models so Bifrost recognizes them
    aliases = cfg.get("scillm_settings", {}).get("model_alias_map", {})
    for alias_model, group_name in aliases.items():
        # Find which provider serves this group's models
        for entry in model_list:
            params = entry.get("scillm_params", {})
            if entry.get("model_name") == group_name:
                api_base = params.get("api_base", "")
                api_key_raw = params.get("api_key", "")
                if api_key_raw == "oauth":
                    break
                provider, _ = _detect_provider(api_base)
                api_key = _resolve_env(api_key_raw)
                if provider and api_key and provider in providers and api_key in providers[provider]:
                    providers[provider][api_key]["models"].add(alias_model)
                break

    # Build Bifrost config
    bifrost = dict(_BIFROST_BASE)
    bifrost["providers"] = {}

    for provider, keys in providers.items():
        key_list = []
        network_config = {}
        custom_config = None

        for key_val, info in keys.items():
            key_list.append({
                "name": info["name"],
                "value": info["value"],
                "models": sorted(info["models"]),
                "weight": info["weight"],
            })
            if info["network_config"]:
                network_config = info["network_config"]
            if info["base_type"] == "openai":
                custom_config = {
                    "base_provider_type": "openai",
                    "is_key_less": False,
                }

        entry: dict = {"keys": key_list}
        if network_config:
            entry["network_config"] = network_config
        if custom_config:
            entry["custom_provider_config"] = custom_config

        bifrost["providers"][provider] = entry

    with open(output_path, "w") as f:
        json.dump(bifrost, f, indent=2)

    total_models = sum(
        len(info["models"])
        for keys in providers.values()
        for info in keys.values()
    )
    print(f"  Generated {output_path}: {len(providers)} providers, {total_models} models")


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "/app/config.yaml"
    output_path = sys.argv[2] if len(sys.argv) > 2 else "/app/bifrost/data/config.json"
    generate(config_path, output_path)
