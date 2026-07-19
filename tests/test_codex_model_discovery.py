from __future__ import annotations

import json
from pathlib import Path

from scillm.proxy.config import ProxyConfig
from scillm.proxy.providers.codex import _apply_codex_reasoning
from scillm.proxy.providers.codex_models import discover_codex_models, resolve_codex_model
from scillm.proxy.router import Router


def _catalog(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-5.6-sol",
                        "display_name": "GPT-5.6-Sol",
                        "supported_reasoning_levels": [
                            {"effort": "high"},
                            {"effort": "xhigh"},
                        ],
                        "default_reasoning_level": "low",
                    },
                    {"slug": "gpt-5.5", "supported_reasoning_levels": [{"effort": "high"}]},
                ]
            }
        ),
        encoding="utf-8",
    )


def test_discovers_models_and_reasoning_efforts(tmp_path: Path) -> None:
    path = tmp_path / "models_cache.json"
    _catalog(path)

    models = discover_codex_models(path)

    assert [model.slug for model in models] == ["gpt-5.6-sol", "gpt-5.5"]
    assert models[0].reasoning_efforts == ("high", "xhigh")


def test_resolves_family_selector_to_first_discovered_variant(tmp_path: Path) -> None:
    path = tmp_path / "models_cache.json"
    _catalog(path)

    resolved = resolve_codex_model("gpt-5.6", discover_codex_models(path))

    assert resolved is not None
    assert resolved.slug == "gpt-5.6-sol"


def test_absent_codex_model_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "models_cache.json"
    _catalog(path)

    assert resolve_codex_model("gpt-9.9", discover_codex_models(path)) is None


def test_router_resolves_family_to_discovered_model(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "models_cache.json"
    _catalog(path)
    monkeypatch.setenv("SCILLM_CODEX_MODELS_CACHE", str(path))
    monkeypatch.setattr("scillm.proxy.router.is_codex_available", lambda: True)

    group = Router(ProxyConfig())._get_group("gpt-5.6")

    assert group is not None
    assert group.deployments[0].model == "gpt-5.6-sol"


def test_codex_request_forwards_discovered_xhigh(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "models_cache.json"
    _catalog(path)
    monkeypatch.setenv("SCILLM_CODEX_MODELS_CACHE", str(path))
    body: dict[str, object] = {}

    _apply_codex_reasoning(body, "gpt-5.6-sol", "xhigh")

    assert body == {"reasoning": {"effort": "xhigh"}}
