"""Discover Claude OAuth models from Anthropic's Models API."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

from scillm.proxy.providers.auth import get_anthropic_token, is_anthropic_available
from scillm.proxy.providers.claude import ANTHROPIC_VERSION, CLAUDE_MODEL_MAP

ANTHROPIC_MODELS_API_URL = os.environ.get("ANTHROPIC_MODELS_API_URL", "https://api.anthropic.com/v1/models")
CLAUDE_MODELS_CACHE_TTL_S = float(os.environ.get("CLAUDE_MODELS_CACHE_TTL_S", "900"))
CLAUDE_REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")

_STATIC_CLAUDE_MODEL_IDS = tuple(dict.fromkeys((
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-5",
    *CLAUDE_MODEL_MAP.keys(),
    *CLAUDE_MODEL_MAP.values(),
)))


@dataclass(frozen=True)
class ClaudeModel:
    id: str
    display_name: str
    created_at: str | None = None
    reasoning_efforts: tuple[str, ...] = CLAUDE_REASONING_EFFORTS


@dataclass(frozen=True)
class ClaudeCatalog:
    source: str
    available: bool
    live: bool
    models: tuple[ClaudeModel, ...]
    error: str | None = None


_CLAUDE_MODELS_CACHE: ClaudeCatalog | None = None
_CLAUDE_MODELS_CACHE_TS = 0.0


def _display_name(model_id: str) -> str:
    return model_id.removeprefix("claude-").replace("-", " ").title()


def _static_catalog(error: str | None = None) -> ClaudeCatalog:
    return ClaudeCatalog(
        source="static_fallback",
        available=is_anthropic_available(),
        live=False,
        error=error,
        models=tuple(
            ClaudeModel(id=model_id, display_name=_display_name(model_id))
            for model_id in _STATIC_CLAUDE_MODEL_IDS
        ),
    )


def _parse_catalog(payload: dict[str, Any]) -> tuple[ClaudeModel, ...]:
    rows = payload.get("data")
    if not isinstance(rows, list):
        return ()
    models: list[ClaudeModel] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id.startswith("claude"):
            continue
        models.append(
            ClaudeModel(
                id=model_id,
                display_name=str(row.get("display_name") or _display_name(model_id)),
                created_at=str(row["created_at"]) if row.get("created_at") else None,
            )
        )
    return tuple(models)


def fetch_claude_catalog(*, refresh: bool = False) -> ClaudeCatalog:
    """Return Claude model catalog with static fallback when live discovery is unavailable."""
    global _CLAUDE_MODELS_CACHE, _CLAUDE_MODELS_CACHE_TS
    now = time.monotonic()
    if (
        not refresh
        and _CLAUDE_MODELS_CACHE is not None
        and now - _CLAUDE_MODELS_CACHE_TS < CLAUDE_MODELS_CACHE_TTL_S
    ):
        return _CLAUDE_MODELS_CACHE

    token = get_anthropic_token()
    if not token:
        fallback = _static_catalog("anthropic_oauth_unavailable")
        _CLAUDE_MODELS_CACHE = fallback
        _CLAUDE_MODELS_CACHE_TS = now
        return fallback

    try:
        resp = httpx.get(
            ANTHROPIC_MODELS_API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-version": ANTHROPIC_VERSION,
            },
            timeout=5.0,
        )
        resp.raise_for_status()
        models = _parse_catalog(resp.json())
        if not models:
            raise ValueError("Anthropic Models API response did not contain Claude models")
        catalog = ClaudeCatalog(
            source=ANTHROPIC_MODELS_API_URL,
            available=True,
            live=True,
            models=models,
        )
        _CLAUDE_MODELS_CACHE = catalog
        _CLAUDE_MODELS_CACHE_TS = now
        return catalog
    except Exception as exc:
        logger.warning("Claude model discovery unavailable: {}", exc)
        if _CLAUDE_MODELS_CACHE is not None:
            return ClaudeCatalog(
                source=_CLAUDE_MODELS_CACHE.source,
                available=_CLAUDE_MODELS_CACHE.available,
                live=_CLAUDE_MODELS_CACHE.live,
                models=_CLAUDE_MODELS_CACHE.models,
                error=str(exc)[:500],
            )
        return _static_catalog(str(exc)[:500])


def claude_catalog_payload(*, refresh: bool = False) -> dict[str, Any]:
    catalog = fetch_claude_catalog(refresh=refresh)
    return {
        "source": catalog.source,
        "available": catalog.available,
        "live": catalog.live,
        "error": catalog.error,
        "models": [
            {
                "id": model.id,
                "display_name": model.display_name,
                "created_at": model.created_at,
                "reasoning_efforts": list(model.reasoning_efforts),
            }
            for model in catalog.models
        ],
    }


def static_claude_model_ids() -> tuple[str, ...]:
    """Return built-in Claude ids used for cheap request-path validation."""
    return _STATIC_CLAUDE_MODEL_IDS


def resolve_claude_model(requested: str, catalog: ClaudeCatalog | None = None) -> ClaudeModel | None:
    available = catalog or fetch_claude_catalog()
    normalized = requested.strip().lower()
    for model in available.models:
        if model.id.lower() == normalized:
            return model
    return None
