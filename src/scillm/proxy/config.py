"""YAML config loader for scillm proxy.

Parses litellm-format proxy_server_config.yaml into typed dataclasses.
Resolves ``os.environ/VAR_NAME`` syntax for environment variable injection.
Supports CHUTES_RESEARCH toggle for research/standard endpoint switching.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

_ENV_RE = re.compile(r"^os\.environ/(\w+)$")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Deployment:
    """Single model deployment (one provider endpoint)."""

    model: str
    api_base: str | None = None
    api_key: str | None = None
    timeout: int = 45
    custom_llm_provider: str | None = None


@dataclass(frozen=True)
class ModelGroup:
    """Named group of deployments (e.g. 'text', 'vlm')."""

    name: str
    deployments: list[Deployment] = field(default_factory=list)


@dataclass(frozen=True)
class RetryPolicy:
    """Per-exception retry counts."""

    internal_server_error: int = 8
    rate_limit_error: int = 6
    timeout_error: int = 6
    authentication_error: int = 0
    bad_request_error: int = 0
    content_policy_violation_error: int = 0


@dataclass(frozen=True)
class GeneralSettings:
    """Top-level proxy settings."""

    master_key: str = ""
    environment: str = "dev"
    health_check_concurrency: int = 5


@dataclass
class ProxyConfig:
    """Complete proxy configuration."""

    general: GeneralSettings = field(default_factory=GeneralSettings)
    model_groups: dict[str, ModelGroup] = field(default_factory=dict)
    fallbacks: dict[str, list[str]] = field(default_factory=dict)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    aliases: dict[str, str] = field(default_factory=dict)
    callbacks: list[str] = field(default_factory=list)
    post_call_rules: str | None = None
    routing_strategy: str = "simple-shuffle"
    num_retries: int = 8
    retry_after: int = 2
    allowed_fails: int = 3
    cooldown_time: int = 20


# ---------------------------------------------------------------------------
# Environment resolution
# ---------------------------------------------------------------------------


def _resolve_env(value: Any) -> Any:
    """Resolve ``os.environ/VAR_NAME`` strings to environment values."""
    if not isinstance(value, str):
        return value
    m = _ENV_RE.match(value)
    if not m:
        return value
    var_name = m.group(1)
    resolved = os.environ.get(var_name)
    if resolved is None:
        logger.warning("env var {} not set, using empty string", var_name)
        return ""
    return resolved


def _resolve_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Recursively resolve env vars in a dict."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = _resolve_dict(v)
        elif isinstance(v, list):
            out[k] = [_resolve_dict(i) if isinstance(i, dict) else _resolve_env(i) for i in v]
        else:
            out[k] = _resolve_env(v)
    return out


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _parse_deployment(params: dict[str, Any]) -> Deployment:
    """Parse a litellm_params block into a Deployment."""
    return Deployment(
        model=params.get("model", ""),
        api_base=params.get("api_base"),
        api_key=params.get("api_key"),
        timeout=int(params.get("timeout", 45)),
        custom_llm_provider=params.get("custom_llm_provider"),
    )


def _parse_model_groups(model_list: list[dict[str, Any]]) -> dict[str, ModelGroup]:
    """Parse model_list into named ModelGroups."""
    groups: dict[str, list[Deployment]] = {}
    for entry in model_list:
        name = entry.get("model_name", "")
        params = entry.get("litellm_params", {})
        dep = _parse_deployment(params)
        groups.setdefault(name, []).append(dep)
    return {name: ModelGroup(name=name, deployments=deps) for name, deps in groups.items()}


def _parse_fallbacks(fallback_list: list[dict[str, Any]] | None) -> dict[str, list[str]]:
    """Parse fallbacks list-of-single-key-dicts into a flat dict."""
    if not fallback_list:
        return {}
    out: dict[str, list[str]] = {}
    for item in fallback_list:
        if isinstance(item, dict):
            for k, v in item.items():
                out[k] = v if isinstance(v, list) else [v]
    return out


def _parse_retry_policy(rp: dict[str, Any] | None) -> RetryPolicy:
    """Parse retry_policy from router_settings."""
    if not rp:
        return RetryPolicy()
    return RetryPolicy(
        internal_server_error=int(rp.get("InternalServerErrorRetries", 8)),
        rate_limit_error=int(rp.get("RateLimitErrorRetries", 6)),
        timeout_error=int(rp.get("TimeoutErrorRetries", 6)),
        authentication_error=int(rp.get("AuthenticationErrorRetries", 0)),
        bad_request_error=int(rp.get("BadRequestErrorRetries", 0)),
        content_policy_violation_error=int(rp.get("ContentPolicyViolationErrorRetries", 0)),
    )


def _parse_general(gs: dict[str, Any] | None) -> GeneralSettings:
    """Parse general_settings."""
    if not gs:
        return GeneralSettings()
    return GeneralSettings(
        master_key=gs.get("master_key", ""),
        environment=gs.get("environment", "dev"),
        health_check_concurrency=int(gs.get("health_check_concurrency", 5)),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(path: str | Path) -> ProxyConfig:
    """Load and parse a proxy config YAML file.

    Args:
        path: Path to proxy_server_config.yaml.

    Returns:
        Fully resolved ProxyConfig with env vars expanded.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")

    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"Config must be a YAML mapping, got {type(raw).__name__}")

    resolved = _resolve_dict(raw)

    general = _parse_general(resolved.get("general_settings"))
    model_groups = _parse_model_groups(resolved.get("model_list", []))

    router = resolved.get("router_settings", {})
    fallbacks = _parse_fallbacks(router.get("fallbacks"))
    retry_policy = _parse_retry_policy(router.get("retry_policy"))

    litellm_settings = resolved.get("litellm_settings", {})
    aliases = litellm_settings.get("model_alias_map", {})
    callbacks = litellm_settings.get("success_callback", [])
    post_call_rules = litellm_settings.get("post_call_rules")

    return ProxyConfig(
        general=general,
        model_groups=model_groups,
        fallbacks=fallbacks,
        retry_policy=retry_policy,
        aliases=aliases,
        callbacks=callbacks,
        post_call_rules=post_call_rules,
        routing_strategy=router.get("routing_strategy", "simple-shuffle"),
        num_retries=int(router.get("num_retries", 8)),
        retry_after=int(router.get("retry_after", 2)),
        allowed_fails=int(router.get("allowed_fails", 3)),
        cooldown_time=int(router.get("cooldown_time", 20)),
    )
