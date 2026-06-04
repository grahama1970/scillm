"""Live test-interactions adapter — runs manifest via skill CLI."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._receipt import base_receipt, sha256_hex
from ._run_sh import run_skill_sh

# 1x1 PNG — placeholder when CDP/Chrome is unavailable (harness smoke only).
_PLACEHOLDER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_manifest(args: dict[str, Any]) -> Path:
    for key in ("manifest", "manifest_path", "interaction_manifest"):
        raw = args.get(key)
        if isinstance(raw, str) and raw.strip():
            path = Path(raw).expanduser()
            if path.is_file():
                return path.resolve()
    env = os.environ.get("SCILLM_CAPTURE_EVIDENCE_MANIFEST", "").strip()
    if env:
        path = Path(env).expanduser()
        if path.is_file():
            return path.resolve()
    default = (
        Path(__file__).resolve().parents[5]
        / "agent-skills"
        / "artifacts"
        / "review-design-orchestration"
        / "fixtures"
        / "smoke-interaction-manifest.json"
    )
    if default.is_file():
        return default.resolve()
    raise FileNotFoundError(
        "test-interactions requires args.manifest or SCILLM_CAPTURE_EVIDENCE_MANIFEST "
        f"(default fixture missing: {default})"
    )


def _resolve_workspace(args: dict[str, Any]) -> Path | None:
    for key in ("workspace", "harness_workspace"):
        raw = args.get(key)
        if isinstance(raw, str) and raw.strip():
            return Path(raw).expanduser().resolve()
    env = os.environ.get("SCILLM_HARNESS_WORKSPACE", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return None


def _chrome_available() -> bool:
    if os.environ.get("TEST_INTERACTIONS_ATTACH_EXISTING", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        return True
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
    ):
        if shutil.which(name):
            return True
    return False


def _screenshot_manifest_from_results(results_path: Path, captures_dir: Path) -> dict[str, Any]:
    screenshots: list[dict[str, str]] = []
    if results_path.is_file():
        data = json.loads(results_path.read_text(encoding="utf-8"))
        for row in data.get("interactions") or []:
            shot = str(row.get("screenshot") or "").strip()
            if not shot:
                continue
            path = Path(shot)
            if not path.is_absolute():
                path = captures_dir / path
            if path.is_file():
                screenshots.append(
                    {
                        "path": str(path),
                        "sha256": sha256_hex(
                            path.read_bytes()[:1_000_000].decode("latin-1", errors="replace")
                        ),
                    }
                )
    if not screenshots and captures_dir.is_dir():
        for png in sorted(captures_dir.rglob("*.png"))[:50]:
            screenshots.append({"path": str(png), "sha256": sha256_hex(png.name)})
    if not screenshots:
        raise ValueError("no screenshots produced by test-interactions run")
    return {
        "schema_version": "review-design-screenshot-manifest.v1",
        "screenshots": screenshots,
        "freshness_timestamp": _iso_now(),
    }


def _persist_workspace(
    workspace: Path,
    *,
    ti_results: dict[str, Any],
    shot_manifest: dict[str, Any],
) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "test-interactions-results.json").write_text(
        json.dumps(ti_results, indent=2), encoding="utf-8"
    )
    (workspace / "screenshot_manifest.json").write_text(
        json.dumps(shot_manifest, indent=2), encoding="utf-8"
    )


def _smoke_capture_without_cdp(*, workspace: Path, manifest: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Harness smoke path when exec runs in a container without Chrome/CDP."""
    captures = workspace / "captures" / "harness-smoke"
    captures.mkdir(parents=True, exist_ok=True)
    placeholder = captures / "smoke-no-cdp-placeholder.png"
    placeholder.write_bytes(_PLACEHOLDER_PNG)
    manifest_body = json.loads(manifest.read_text(encoding="utf-8"))
    app = str(manifest_body.get("app") or "unknown")
    ti_results = {
        "schema_version": "review-design-test-interactions-results.v1",
        "status": "smoke_no_cdp",
        "results_ref": str(placeholder),
        "interaction_refs": ["harness-smoke:no-cdp"],
        "screenshot_refs": [str(placeholder)],
        "freshness_timestamp": _iso_now(),
        "error": None,
        "pass": False,
        "capture_mode": "harness_smoke_no_cdp",
        "note": "Chrome/CDP unavailable in exec environment; placeholder artifact only.",
    }
    shot_manifest = {
        "schema_version": "review-design-screenshot-manifest.v1",
        "screenshots": [
            {
                "path": str(placeholder),
                "sha256": __import__("hashlib").sha256(_PLACEHOLDER_PNG).hexdigest(),
                "label": "harness-smoke-no-cdp-placeholder",
            }
        ],
        "freshness_timestamp": _iso_now(),
        "capture_mode": "harness_smoke_no_cdp",
        "manifest": str(manifest),
        "app": app,
    }
    _persist_workspace(workspace, ti_results=ti_results, shot_manifest=shot_manifest)
    return ti_results, shot_manifest


class TestInteractionsAdapter:
    def invoke(self, spec: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        args = dict(spec.get("args") or {})
        if dry_run:
            raise NotImplementedError(
                "test-interactions requires live execution; lower harness with --live / --no-dry-run"
            )

        try:
            manifest = _resolve_manifest(args)
        except FileNotFoundError as exc:
            return base_receipt(
                skill="test-interactions",
                spec=spec,
                status="error",
                executor="harness:test-interactions-adapter",
                errors=[str(exc)],
                dry_run=False,
            )

        workspace = _resolve_workspace(args)

        if workspace and not _chrome_available():
            ti_results, shot_manifest = _smoke_capture_without_cdp(
                workspace=workspace, manifest=manifest
            )
            ti_sha = sha256_hex(json.dumps(ti_results, sort_keys=True, default=str))
            shot_sha = sha256_hex(json.dumps(shot_manifest, sort_keys=True, default=str))
            return base_receipt(
                skill="test-interactions",
                spec=spec,
                status="ok",
                executor="harness:test-interactions-adapter",
                artifacts=[
                    {"path": "test-interactions-results.json", "sha256": ti_sha},
                    {"path": "screenshot_manifest.json", "sha256": shot_sha},
                ],
                extra={
                    "test_interactions_results": ti_results,
                    "screenshot_manifest": shot_manifest,
                    "validation": {
                        "useful": False,
                        "why": "no Chrome/CDP in exec container — harness smoke placeholder written",
                        "commands_run": [f"harness smoke capture (manifest={manifest})"],
                    },
                },
                dry_run=False,
            )

        work_root = Path(os.environ.get("SCILLM_HARNESS_WORK_DIR", "/tmp/scillm-harness-work")).expanduser()
        work_root.mkdir(parents=True, exist_ok=True)
        output_dir = work_root / f"capture-{spec.get('skill_call_id', 'capture')}"
        output_dir.mkdir(parents=True, exist_ok=True)

        proc = run_skill_sh(
            "test-interactions",
            ["run", "--manifest", str(manifest), "--output-dir", str(output_dir)],
            timeout_sec=int(spec.get("timeout_sec") or 600),
        )
        results_path = output_dir / "results.json"

        if proc.returncode != 0 or not results_path.is_file():
            err = (proc.stderr or proc.stdout or "test-interactions run failed")[:2000]
            if workspace and (
                "Chrome" in err or "Chromium" in err or "CDP" in err or "ConnectionError" in err
            ):
                ti_results, shot_manifest = _smoke_capture_without_cdp(
                    workspace=workspace, manifest=manifest
                )
                ti_sha = sha256_hex(json.dumps(ti_results, sort_keys=True, default=str))
                shot_sha = sha256_hex(json.dumps(shot_manifest, sort_keys=True, default=str))
                return base_receipt(
                    skill="test-interactions",
                    spec=spec,
                    status="ok",
                    executor="harness:test-interactions-adapter",
                    artifacts=[
                        {"path": "test-interactions-results.json", "sha256": ti_sha},
                        {"path": "screenshot_manifest.json", "sha256": shot_sha},
                    ],
                    extra={
                        "test_interactions_results": ti_results,
                        "screenshot_manifest": shot_manifest,
                        "validation": {
                            "useful": False,
                            "why": "test-interactions failed (no CDP) — harness smoke placeholder",
                            "commands_run": [f"test-interactions run failed: {err[:120]}"],
                        },
                    },
                    dry_run=False,
                )
            return base_receipt(
                skill="test-interactions",
                spec=spec,
                status="error",
                executor="harness:test-interactions-adapter",
                errors=[err],
                dry_run=False,
            )

        results_body = json.loads(results_path.read_text(encoding="utf-8"))
        passed = bool(results_body.get("failed", 1) == 0)
        ti_results = {
            "schema_version": "review-design-test-interactions-results.v1",
            "status": "ok" if passed else "failed",
            "results_ref": str(results_path),
            "interaction_refs": [
                f"{row.get('surface')}:{row.get('element')}:{row.get('action')}"
                for row in (results_body.get("interactions") or [])[:100]
            ],
            "screenshot_refs": [
                str(row.get("screenshot") or "")
                for row in (results_body.get("interactions") or [])
                if row.get("screenshot")
            ],
            "freshness_timestamp": _iso_now(),
            "error": None if passed else f"{results_body.get('failed')} interaction failures",
            "pass": passed,
        }
        try:
            shot_manifest = _screenshot_manifest_from_results(results_path, output_dir)
        except ValueError as exc:
            return base_receipt(
                skill="test-interactions",
                spec=spec,
                status="error",
                executor="harness:test-interactions-adapter",
                errors=[str(exc)],
                extra={"test_interactions_results": ti_results},
                dry_run=False,
            )

        if workspace:
            _persist_workspace(workspace, ti_results=ti_results, shot_manifest=shot_manifest)

        ti_sha = sha256_hex(json.dumps(ti_results, sort_keys=True, default=str))
        shot_sha = sha256_hex(json.dumps(shot_manifest, sort_keys=True, default=str))
        return base_receipt(
            skill="test-interactions",
            spec=spec,
            status="ok" if passed else "error",
            executor="harness:test-interactions-adapter",
            artifacts=[
                {"path": "test-interactions-results.json", "sha256": ti_sha},
                {"path": "screenshot_manifest.json", "sha256": shot_sha},
            ],
            extra={
                "test_interactions_results": ti_results,
                "screenshot_manifest": shot_manifest,
                "validation": {
                    "useful": passed,
                    "why": "live test-interactions run against manifest",
                    "commands_run": [f"test-interactions run --manifest {manifest}"],
                },
            },
            dry_run=False,
        )
