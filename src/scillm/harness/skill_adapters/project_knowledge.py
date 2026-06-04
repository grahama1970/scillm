"""Live project-knowledge adapter — reads PROJECT_KNOWLEDGE.md via skill CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ._receipt import base_receipt, sha256_hex
from ._run_sh import run_skill_sh


def _project_cwd(args: dict[str, Any]) -> Path:
    for key in ("project_root", "cwd", "workspace"):
        raw = args.get(key)
        if isinstance(raw, str) and raw.strip():
            return Path(raw).expanduser().resolve()
    env = os.environ.get("SCILLM_PROJECT_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[5] / "agent-skills"





def _resolve_workspace(args: dict[str, Any]) -> Path | None:
    for key in ("workspace", "harness_workspace"):
        raw = args.get(key)
        if isinstance(raw, str) and raw.strip():
            return Path(raw).expanduser().resolve()
    return None


def _persist_workspace(workspace: Path, payload: dict[str, Any]) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "project_knowledge.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n",
        encoding="utf-8",
    )

def _read_project_knowledge_md(cwd: Path) -> dict[str, Any]:
    """Read PROJECT_KNOWLEDGE.md directly when skill run.sh is unavailable (e.g. exec container)."""
    for name in ("PROJECT_KNOWLEDGE.md", "project_knowledge.md"):
        path = cwd / name
        if path.is_file():
            body = path.read_text(encoding="utf-8", errors="replace")
            return {"PROJECT_KNOWLEDGE.md": body}
    raise FileNotFoundError(f"PROJECT_KNOWLEDGE.md not found under {cwd}")


def _build_artifact(sections: dict[str, Any], *, artifact_path: str) -> dict[str, Any]:
    known_failures: list[str] = []
    non_goals: list[str] = []
    context_refs: list[str] = []
    for name, body in sections.items():
        if not isinstance(body, str):
            continue
        low = name.lower()
        blob = body.strip()
        if not blob:
            continue
        context_refs.append(f"section:{name}")
        if "failure" in low or "blocker" in low:
            known_failures.extend(blob.splitlines()[:20])
        if "non-goal" in low or "out of scope" in low:
            non_goals.extend(blob.splitlines()[:20])
    return {
        "schema": "review-design-project-knowledge.v1",
        "status": "ok",
        "source": "project-knowledge",
        "context_refs": context_refs,
        "known_failures": [line for line in known_failures if line.strip()][:30],
        "non_goals": [line for line in non_goals if line.strip()][:30],
        "artifact_path": artifact_path,
        "error": None,
    }


class ProjectKnowledgeAdapter:
    def invoke(self, spec: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        args = dict(spec.get("args") or {})
        cwd = _project_cwd(args)
        if dry_run:
            raise NotImplementedError(
                "project-knowledge requires live execution; lower harness with --live / --no-dry-run"
            )

        sections: dict[str, Any]
        commands_run: list[str]
        try:
            proc = run_skill_sh(
                "project-knowledge",
                ["read", "--json"],
                cwd=cwd,
                extra_env={"PROJECT_KNOWLEDGE_CWD": str(cwd)},
                timeout_sec=int(spec.get("timeout_sec") or 120),
            )
            commands_run = [f"project-knowledge read --json (cwd={cwd})"]
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or proc.stdout or "project-knowledge read failed")[:2000])
            try:
                sections = json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"invalid JSON from project-knowledge read: {exc}") from exc
            if not isinstance(sections, dict):
                sections = {"body": sections}
        except (FileNotFoundError, RuntimeError) as exc:
            try:
                sections = _read_project_knowledge_md(cwd)
                commands_run = [f"read PROJECT_KNOWLEDGE.md (cwd={cwd})"]
            except FileNotFoundError:
                return base_receipt(
                    skill="project-knowledge",
                    spec=spec,
                    status="error",
                    executor="harness:project-knowledge-adapter",
                    errors=[str(exc)],
                    extra={"project_knowledge": {"schema": "review-design-project-knowledge.v1", "status": "failed", "source": "project-knowledge", "error": str(exc)[:500]}},
                    dry_run=False,
                )

        artifact_path = "project_knowledge.json"
        payload = _build_artifact(sections, artifact_path=artifact_path)
        workspace = _resolve_workspace(dict(spec.get("args") or {}))
        if workspace:
            _persist_workspace(workspace, payload)
        artifact_sha = sha256_hex(json.dumps(payload, sort_keys=True, default=str))
        return base_receipt(
            skill="project-knowledge",
            spec=spec,
            status="ok",
            executor="harness:project-knowledge-adapter",
            artifacts=[{"path": artifact_path, "sha256": artifact_sha}],
            extra={
                "project_knowledge": payload,
                "validation": {
                    "useful": True,
                    "why": "live project-knowledge read --json",
                    "commands_run": commands_run,
                },
            },
            dry_run=False,
        )
