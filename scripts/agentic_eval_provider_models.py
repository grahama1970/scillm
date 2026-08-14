#!/usr/bin/env python3
"""Agentic eval probes for dynamic provider model catalogs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = "http://127.0.0.1:4001"


def _dotenv_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _config_value(names: tuple[str, ...], default: str) -> str:
    dotenv = _dotenv_values(REPO_ROOT / ".env")
    for name in names:
        value = os.environ.get(name) or dotenv.get(name)
        if value:
            return value
    return default


def _proxy_base() -> str:
    return _config_value(("SCILLM_API_BASE",), DEFAULT_BASE).rstrip("/")


def _proxy_key() -> str:
    return _config_value(
        ("SCILLM_MASTER_KEY", "LITELLM_MASTER_KEY", "SCILLM_PROXY_KEY"),
        "sk-dev-proxy-123",
    )


def _request_json(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{_proxy_base()}{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {_proxy_key()}",
            "X-Caller-Skill": "scillm-agentic-evals",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return exc.code, json.loads(text)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def unit_catalog_contract() -> None:
    _run_pytest(
        [
            "tests/test_claude_model_aliases.py",
            "tests/test_codex_model_discovery.py",
            "tests/test_codex_routing.py",
            "tests/test_opencode_go.py",
        ],
        timeout=120,
    )
    print("unit_catalog_contract_ok")


def _run_pytest(paths: list[str], *, timeout: int) -> None:
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(REPO_ROOT / "src")
        if not existing_pythonpath
        else f"{REPO_ROOT / 'src'}{os.pathsep}{existing_pythonpath}"
    )
    result = subprocess.run(
        [
            "/usr/bin/python3",
            "-m",
            "pytest",
            "-q",
            *paths,
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
        env=env,
    )
    if result.returncode != 0:
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)


def unit_reasoning_contract() -> None:
    _run_pytest(
        [
            "tests/test_codex_routing.py::test_validation_allows_dynamic_codex_xhigh_reasoning",
            "tests/test_codex_routing.py::test_validation_rejects_invalid_codex_reasoning_effort",
            "tests/test_codex_routing.py::test_validation_rejects_stale_codex_reasoning_effort_with_dynamic_help",
            "tests/test_opencode_go.py::test_app_validation_rejects_opencode_go_reasoning_with_help",
        ],
        timeout=120,
    )
    print("unit_reasoning_contract_ok")


def live_catalog_endpoint() -> None:
    status, health = _request_json("/health/liveliness")
    _require(status == 200 and health.get("status") == "ok", "proxy liveliness did not return ok")

    status, payload = _request_json("/v1/scillm/models?refresh_provider_models=true")
    _require(status == 200, f"models endpoint returned {status}")
    catalogs = payload.get("provider_catalogs")
    _require(isinstance(catalogs, dict), "provider_catalogs missing")

    for provider in ("claude", "codex", "opencode-go"):
        catalog = catalogs.get(provider)
        _require(isinstance(catalog, dict), f"{provider} catalog missing")
        models = catalog.get("models")
        _require(isinstance(models, list) and models, f"{provider} catalog has no models")
        for row in models:
            _require(isinstance(row, dict) and row.get("id"), f"{provider} model row missing id")

    claude_ids = {row["id"] for row in catalogs["claude"]["models"]}
    _require("claude-opus-5" in claude_ids, "claude-opus-5 missing from Claude catalog")

    codex_rows = catalogs["codex"]["models"]
    _require(
        any(row.get("reasoning_efforts") for row in codex_rows),
        "Codex catalog did not expose reasoning efforts",
    )
    for row in catalogs["opencode-go"]["models"]:
        _require("reasoning_efforts" in row, "OpenCode Go row missing reasoning_efforts field")
        _require(row.get("reasoning_source"), "OpenCode Go row missing reasoning_source")

    print(
        "live_provider_catalogs_ok "
        f"claude={len(catalogs['claude']['models'])} "
        f"codex={len(catalogs['codex']['models'])} "
        f"opencode-go={len(catalogs['opencode-go']['models'])}"
    )


def live_invalid_selectors() -> None:
    cases = {
        "claude-not-real-999": "claude",
        "gpt-not-real-999": "codex",
        "opencode-go/not-real-999": "opencode-go",
    }
    for model, provider in cases.items():
        status, payload = _request_json(
            "/v1/chat/completions",
            method="POST",
            payload={"model": model, "messages": [{"role": "user", "content": "ping"}]},
        )
        error = payload.get("error") or {}
        details = error.get("details") or {}
        _require(status == 400, f"{model} returned status {status}")
        _require(error.get("type") == "model_not_available", f"{model} missing model_not_available")
        _require(details.get("provider") == provider, f"{model} provider detail mismatch")
        _require(details.get("requested_model") == model, f"{model} requested_model mismatch")
        _require(details.get("available_models"), f"{model} available_models missing")
        _require(details.get("refresh_hint"), f"{model} refresh_hint missing")
        _require(details.get("catalog_source"), f"{model} catalog_source missing")
        for row in details.get("available_models") or []:
            _require("reasoning_efforts" in row, f"{model} row missing reasoning_efforts")
        if provider in {"claude", "codex"}:
            _require(
                any(row.get("reasoning_efforts") for row in details.get("available_models") or []),
                f"{provider} error payload missing reasoning effort values",
            )
    print("live_invalid_selectors_ok claude,codex,opencode-go")


COMMANDS = {
    "unit-catalog-contract": unit_catalog_contract,
    "unit-reasoning-contract": unit_reasoning_contract,
    "live-catalog-endpoint": live_catalog_endpoint,
    "live-invalid-selectors": live_invalid_selectors,
}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print("usage: agentic_eval_provider_models.py [" + "|".join(sorted(COMMANDS)) + "]", file=sys.stderr)
        return 2
    try:
        COMMANDS[sys.argv[1]]()
    except Exception as exc:
        print(f"agentic_eval_provider_models_failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
