"""Discover Codex OAuth models from the catalog maintained by Codex CLI."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger


@dataclass(frozen=True)
class CodexModel:
    slug: str
    display_name: str
    reasoning_efforts: tuple[str, ...]
    default_reasoning_effort: str | None


def codex_models_cache_path() -> Path:
    configured = os.environ.get("SCILLM_CODEX_MODELS_CACHE", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".codex" / "models_cache.json"


def discover_codex_models(path: Path | None = None) -> tuple[CodexModel, ...]:
    """Return the current Codex CLI model catalog, or an empty tuple fail-closed."""
    source = path or codex_models_cache_path()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Codex model discovery unavailable at {}: {}", source, exc)
        return ()

    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        logger.warning("Codex model discovery catalog has no models list: {}", source)
        return ()

    models: list[CodexModel] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        slug = row.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        levels = row.get("supported_reasoning_levels")
        efforts: list[str] = []
        if isinstance(levels, list):
            for level in levels:
                effort = level.get("effort") if isinstance(level, dict) else None
                if isinstance(effort, str) and effort.strip():
                    efforts.append(effort.strip().lower())
        default = row.get("default_reasoning_level")
        models.append(
            CodexModel(
                slug=slug.strip(),
                display_name=str(row.get("display_name") or slug).strip(),
                reasoning_efforts=tuple(dict.fromkeys(efforts)),
                default_reasoning_effort=(default.strip().lower() if isinstance(default, str) and default.strip() else None),
            )
        )
    return tuple(models)


def resolve_codex_model(requested: str, models: tuple[CodexModel, ...] | None = None) -> CodexModel | None:
    """Resolve an exact slug or a family selector such as ``gpt-5.6``."""
    available = models if models is not None else discover_codex_models()
    normalized = requested.strip().lower()
    for model in available:
        if model.slug.lower() == normalized:
            return model
    for model in available:
        if model.slug.lower().startswith(f"{normalized}-"):
            return model
    return None


def codex_catalog_payload(path: Path | None = None) -> dict[str, Any]:
    source = path or codex_models_cache_path()
    models = discover_codex_models(source)
    return {
        "source": str(source),
        "available": bool(models),
        "models": [
            {
                "id": model.slug,
                "display_name": model.display_name,
                "reasoning_efforts": list(model.reasoning_efforts),
                "default_reasoning_effort": model.default_reasoning_effort,
            }
            for model in models
        ],
    }
