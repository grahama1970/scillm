"""Compile per-run OpenCode skill views and system prompt overlays for serve workers."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class SkillViewReceipt:
    """What scillm exposed to OpenCode for one serve run."""

    skills_requested: tuple[str, ...]
    skills_materialized: tuple[str, ...]
    skills_missing: tuple[str, ...]
    skill_view_dir: str | None
    search_roots: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "skills_requested": list(self.skills_requested),
            "skills_materialized": list(self.skills_materialized),
            "skills_missing": list(self.skills_missing),
            "skill_view_dir": self.skill_view_dir,
            # search_roots omitted from API receipt (internal only)
        }


def _normalize_skill_names(skills: list[str] | None) -> list[str]:
    if not skills:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in skills:
        name = str(raw).strip().lower()
        if not name or name in seen:
            continue
        if not _SKILL_NAME_RE.match(name):
            continue
        seen.add(name)
        out.append(name)
    return out


def default_skill_search_roots() -> list[Path]:
    roots: list[Path] = []
    for env_key in (
        "SCILLM_OPENCODE_SKILL_ROOTS",
        "OPENCODE_SKILL_ROOTS",
    ):
        raw = os.environ.get(env_key, "").strip()
        if raw:
            for part in raw.split(":"):
                part = part.strip()
                if part:
                    roots.append(Path(part).expanduser())
    for candidate in (
        Path.home() / ".config" / "opencode" / "skills",
        Path.home() / ".claude" / "skills",
        Path.home() / ".codex" / "skills",
        Path.home() / ".pi" / "skills",
    ):
        if candidate.is_dir() and candidate not in roots:
            roots.append(candidate)
    return roots


def _find_skill_dir(name: str, roots: list[Path]) -> Path | None:
    for root in roots:
        try:
            root_resolved = root.expanduser().resolve()
        except OSError:
            continue
        if not root_resolved.is_dir():
            continue
        direct = root_resolved / name
        if (direct / "SKILL.md").is_file():
            try:
                if root_resolved in direct.resolve().parents or direct.resolve() == root_resolved:
                    return direct
            except OSError:
                continue
    return None


def materialize_skill_view(
    *,
    run_id: str,
    skills: list[str] | None,
    base_dir: Path | None = None,
    search_roots: list[Path] | None = None,
) -> SkillViewReceipt:
    """Symlink allowed skills into ``<base>/<run_id>/.opencode/skills/<name>``."""
    requested = _normalize_skill_names(skills)
    if not requested:
        return SkillViewReceipt(
            skills_requested=tuple(),
            skills_materialized=tuple(),
            skills_missing=tuple(),
            skill_view_dir=None,
            search_roots=tuple(str(p) for p in (search_roots or default_skill_search_roots())),
        )

    roots = search_roots or default_skill_search_roots()
    safe_run_id = re.sub(r"[^a-zA-Z0-9._-]+", "-", run_id).strip("-") or "run"
    root = (base_dir or Path(os.environ.get("SCILLM_OPENCODE_SKILL_VIEW_DIR", "/tmp/scillm-opencode-skill-views"))).expanduser()
    view_dir = root / safe_run_id / ".opencode" / "skills"
    view_dir.mkdir(parents=True, exist_ok=True)

    materialized: list[str] = []
    missing: list[str] = []
    for name in requested:
        src = _find_skill_dir(name, roots)
        dest = view_dir / name
        if src is None:
            missing.append(name)
            continue
        if dest.exists() or dest.is_symlink():
            if dest.is_dir() and not dest.is_symlink():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        dest.symlink_to(src.resolve(), target_is_directory=True)
        materialized.append(name)

    return SkillViewReceipt(
        skills_requested=tuple(requested),
        skills_materialized=tuple(materialized),
        skills_missing=tuple(missing),
        skill_view_dir=str(view_dir.parent.parent),
        search_roots=tuple(str(p) for p in roots),
    )


def build_skills_system_overlay(receipt: SkillViewReceipt) -> str:
    if not receipt.skills_materialized:
        return ""
    lines = [
        "## scillm skill allowlist",
        "Load skills only via the OpenCode `skill` tool when directly relevant.",
        "Allowed skill names for this run:",
    ]
    lines.extend(f"- `{name}`" for name in receipt.skills_materialized)
    if receipt.skills_missing:
        lines.append("Requested but not found on disk (deny use):")
        lines.extend(f"- `{name}`" for name in receipt.skills_missing)
    lines.append(
        "Do not call skills outside this list. For LLM HTTP calls prefer the `scillm` skill "
        "(localhost:4001, Bearer token, X-Caller-Skill header)."
    )
    return "\n".join(lines)


def merge_system_prompt(base: str | None, overlay: str) -> str | None:
    parts = [p.strip() for p in (base, overlay) if isinstance(p, str) and p.strip()]
    if not parts:
        return None
    return "\n\n".join(parts)


def cleanup_skill_view(receipt: SkillViewReceipt) -> None:
    if not receipt.skill_view_dir:
        return
    path = Path(receipt.skill_view_dir)
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
