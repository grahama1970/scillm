"""YAML config loader for scillm proxy.

Parses proxy_server_config.yaml into typed dataclasses.
Resolves ``os.environ/VAR_NAME`` syntax for environment variable injection.
Also supports fallback lookup via ``os.environ/PRIMARY|SECONDARY``.
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

_ENV_RE = re.compile(r"^os\.environ/([\w|]+)$")


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


@dataclass(frozen=True)
class CallerProfile:
    """Per-caller capability policy.

    Profiles are keyed by the ``X-Caller-Skill`` header.  Empty/default fields
    are permissive to keep unprofiled callers OpenAI-compatible.
    """

    allowed_models: list[str] = field(default_factory=list)
    deny_model_patterns: list[str] = field(default_factory=list)
    require_scillm_metadata: list[str] = field(default_factory=list)
    allow_tools: bool = True
    allow_files: bool = True
    allow_images: bool = True
    allow_pdfs: bool = True
    allow_streaming: bool = True
    max_timeout_s: float | None = None


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
    ollama_api_base: str | None = None
    chutes_api_base: str | None = None
    chutes_api_key: str | None = None
    gemini_api_base: str | None = None
    gemini_api_key: str | None = None
    opencode_go_api_base: str | None = None
    opencode_go_api_key: str | None = None
    caller_profiles: dict[str, CallerProfile] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Environment resolution
# ---------------------------------------------------------------------------


def _resolve_env(value: Any) -> Any:
    """Resolve ``os.environ/VAR_NAME`` strings to environment values.

    Supports fallbacks via ``os.environ/PRIMARY|SECONDARY`` and returns the first
    non-empty environment value found.
    """
    if not isinstance(value, str):
        return value
    m = _ENV_RE.match(value)
    if not m:
        return value
    var_names = [part for part in m.group(1).split("|") if part]
    for var_name in var_names:
        resolved = os.environ.get(var_name)
        if resolved:
            return resolved
    logger.warning("env vars {} not set, using empty string", ", ".join(var_names))
    return ""


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
    """Parse a scillm_params block into a Deployment."""
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
        params = entry.get("scillm_params", entry.get("litellm_params", entry.get("params", {})))
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


def _as_str_list(value: Any) -> list[str]:
    """Normalize YAML scalar/list values to a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _as_bool(value: Any, default: bool = True) -> bool:
    """Parse permissive YAML/env boolean values."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _parse_caller_profiles(raw: dict[str, Any] | None) -> dict[str, CallerProfile]:
    """Parse caller_profiles config keyed by X-Caller-Skill."""
    if not raw:
        return {}
    out: dict[str, CallerProfile] = {}
    for caller, profile in raw.items():
        if not isinstance(profile, dict):
            logger.warning("caller_profiles.{} must be a mapping, skipping", caller)
            continue
        max_timeout_raw = profile.get("max_timeout_s")
        max_timeout_s = float(max_timeout_raw) if max_timeout_raw is not None else None
        out[str(caller)] = CallerProfile(
            allowed_models=_as_str_list(profile.get("allowed_models")),
            deny_model_patterns=_as_str_list(profile.get("deny_model_patterns")),
            require_scillm_metadata=_as_str_list(profile.get("require_scillm_metadata")),
            allow_tools=_as_bool(profile.get("allow_tools"), True),
            allow_files=_as_bool(profile.get("allow_files"), True),
            allow_images=_as_bool(profile.get("allow_images"), True),
            allow_pdfs=_as_bool(profile.get("allow_pdfs"), True),
            allow_streaming=_as_bool(profile.get("allow_streaming"), True),
            max_timeout_s=max_timeout_s,
        )
    return out


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
    caller_profiles = _parse_caller_profiles(resolved.get("caller_profiles"))

    settings = resolved.get("scillm_settings", resolved.get("litellm_settings", {}))
    aliases = settings.get("model_alias_map", {})
    callbacks = settings.get("success_callback", [])
    post_call_rules = settings.get("post_call_rules")

    # Auto-detect Ollama base URL: explicit config > env var > sniff from local-text group
    ollama_base = resolved.get("ollama_api_base") or os.environ.get("OLLAMA_API_BASE")
    if not ollama_base and "local-text" in model_groups:
        for dep in model_groups["local-text"].deployments:
            if dep.api_base and "11434" in dep.api_base:
                ollama_base = dep.api_base
                break
    if ollama_base:
        # Ensure /v1 suffix — the openai SDK needs it for correct path construction
        if not ollama_base.rstrip("/").endswith("/v1"):
            ollama_base = ollama_base.rstrip("/") + "/v1"
        logger.info("Ollama auto-routing enabled (base={})", ollama_base)

    # Auto-detect Chutes base URL and API key from env. Chutes chat aliases are
    # intentionally not inferred from configured groups; callers must pass exact
    # live provider/model IDs selected from current inventory.
    chutes_base = os.environ.get("CHUTES_API_BASE")
    chutes_key = os.environ.get("CHUTES_API_KEY")
    if chutes_base:
        # Ensure /v1 suffix
        if not chutes_base.rstrip("/").endswith("/v1"):
            chutes_base = chutes_base.rstrip("/") + "/v1"
        logger.info("Chutes auto-routing enabled (base={})", chutes_base)

    # Auto-detect Gemini base URL and API key from env or sniff from Gemini groups
    gemini_base = os.environ.get("GEMINI_API_BASE")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_base:
        for group_name in ("gemini-flash", "gemini-flash-high", "vlm"):
            group = model_groups.get(group_name)
            if not group:
                continue
            for dep in group.deployments:
                if dep.api_base and "generativelanguage.googleapis.com" in dep.api_base:
                    gemini_base = dep.api_base
                    gemini_key = gemini_key or dep.api_key
                    break
            if gemini_base:
                break
    if gemini_base:
        logger.info("Gemini auto-routing enabled (base={})", gemini_base)

    opencode_go_base = (
        os.environ.get("OPENCODE_GO_API_BASE")
        or resolved.get("opencode_go_api_base")
        or "https://opencode.ai/zen/go/v1"
    )
    opencode_go_key = os.environ.get("OPENCODE_GO_API_KEY") or resolved.get("opencode_go_api_key")
    if opencode_go_base and opencode_go_key:
        logger.info("OpenCode Go auto-routing enabled (base={})", opencode_go_base)

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
        ollama_api_base=ollama_base,
        chutes_api_base=chutes_base,
        chutes_api_key=chutes_key,
        gemini_api_base=gemini_base,
        gemini_api_key=gemini_key,
        opencode_go_api_base=opencode_go_base,
        opencode_go_api_key=opencode_go_key,
        caller_profiles=caller_profiles,
    )
