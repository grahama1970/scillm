"""Config resolution for curate pipeline.

Resolution order: defaults → file → env → CLI

Config file location: {code_path}/.graph-memory-operator.yaml
"""

import os
from pathlib import Path
from typing import Any, Optional

import yaml

from graph_memory.codebase.pipeline.types import CurateConfig

CONFIG_FILENAME = ".graph-memory-operator.yaml"

# Environment variable prefix
ENV_PREFIX = "CURATE_"


def load_config_file(code_path: Path) -> dict[str, Any]:
    """Load config from file if it exists."""
    config_path = code_path / CONFIG_FILENAME
    if config_path.exists():
        try:
            with open(config_path) as f:
                data = yaml.safe_load(f) or {}
                return data.get("curate", {})
        except (yaml.YAMLError, OSError):
            return {}
    return {}


def load_env_overrides() -> dict[str, Any]:
    """Load config overrides from environment variables."""
    overrides = {}

    # PDF settings
    if os.getenv(f"{ENV_PREFIX}PDF_REMOTE"):
        overrides.setdefault("pdf", {})["remote"] = (
            os.getenv(f"{ENV_PREFIX}PDF_REMOTE", "").lower() == "true"
        )

    if os.getenv(f"{ENV_PREFIX}PDF_ENABLED"):
        overrides.setdefault("pdf", {})["enabled"] = (
            os.getenv(f"{ENV_PREFIX}PDF_ENABLED", "").lower() == "true"
        )

    # Lean settings
    if os.getenv(f"{ENV_PREFIX}LEAN_ENABLED"):
        overrides.setdefault("lean", {})["enabled"] = (
            os.getenv(f"{ENV_PREFIX}LEAN_ENABLED", "").lower() == "true"
        )

    # Debug
    if os.getenv(f"{ENV_PREFIX}DEBUG"):
        overrides["debug"] = {"enabled": os.getenv(f"{ENV_PREFIX}DEBUG") == "1"}

    return overrides


def merge_configs(*configs: dict[str, Any]) -> dict[str, Any]:
    """Deep merge multiple config dictionaries."""
    result: dict[str, Any] = {}
    for config in configs:
        for key, value in config.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = merge_configs(result[key], value)
            else:
                result[key] = value
    return result


def resolve_config(
    code_path: Path,
    debug: bool = False,
    cli_overrides: Optional[dict[str, Any]] = None,
) -> CurateConfig:
    """Resolve config from all sources.

    Order: defaults → file → env → CLI
    """
    # Start with defaults (from CurateConfig dataclass)
    config = CurateConfig()

    # Load file config
    file_config = load_config_file(code_path)

    # Load env overrides
    env_config = load_env_overrides()

    # CLI overrides
    cli_config = cli_overrides or {}
    if debug:
        cli_config.setdefault("debug", {})["enabled"] = True

    # Merge all configs
    merged = merge_configs(file_config, env_config, cli_config)

    # Apply merged config to CurateConfig
    pdf = merged.get("pdf", {})
    if "enabled" in pdf:
        config.pdf_enabled = pdf["enabled"]
    if "remote" in pdf:
        config.pdf_remote = pdf["remote"]
    if "remote_domains" in pdf:
        config.pdf_remote_domains = pdf["remote_domains"]
    if "allow_any_domain" in pdf:
        config.pdf_allow_any_domain = pdf["allow_any_domain"]
    if "max_remote" in pdf:
        config.pdf_max_remote = pdf["max_remote"]
    if "max_bytes" in pdf:
        config.pdf_max_bytes = pdf["max_bytes"]
    if "timeout_s" in pdf:
        config.pdf_timeout_s = pdf["timeout_s"]
    if "marker_llm_mode" in pdf:
        config.pdf_marker_llm_mode = pdf["marker_llm_mode"]

    lean = merged.get("lean", {})
    if "enabled" in lean:
        config.lean_enabled = lean["enabled"]
    if "max_theorems" in lean:
        config.lean_max_theorems = lean["max_theorems"]
    if "time_budget_s" in lean:
        config.lean_time_budget_s = lean["time_budget_s"]
    if "candidate_max" in lean:
        config.lean_candidate_max = lean["candidate_max"]
    if "tactics" in lean:
        config.lean_tactics = lean["tactics"]

    treesitter = merged.get("treesitter", {})
    if "enabled" in treesitter:
        config.treesitter_enabled = treesitter["enabled"]

    debug_config = merged.get("debug", {})
    if "enabled" in debug_config:
        config.debug = debug_config["enabled"]
    if "max_files" in debug_config:
        config.debug_max_files = debug_config["max_files"]
    if "verbose_artifacts" in debug_config:
        config.debug_verbose_artifacts = debug_config["verbose_artifacts"]

    # Apply debug defaults if debug is enabled
    if config.debug:
        config.debug_max_files = debug_config.get("max_files", 50)
        config.pdf_max_remote = pdf.get("max_remote", 3)
        config.pdf_max_bytes = pdf.get("max_bytes", 10485760)  # 10MB
        config.pdf_timeout_s = pdf.get("timeout_s", 30)
        config.lean_candidate_max = lean.get("candidate_max", 200)
        config.lean_max_theorems = lean.get("max_theorems", 10)
        config.lean_time_budget_s = lean.get("time_budget_s", 180)
        config.debug_verbose_artifacts = True

    return config
