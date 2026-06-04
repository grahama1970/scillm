"""Invoke Embry skill run.sh entrypoints from harness adapters."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _canonical_skills_roots() -> list[Path]:
    """Real skill trees (agent-skills/skills). IDE paths are usually symlinks into here."""
    roots: list[Path] = []
    for key in (
        "SCILLM_CANONICAL_SKILLS_ROOT",
        "EMBRY_SKILLS_DIR",
        "SKILLS_DIR",
        "PI_SKILLS_DIR",
    ):
        raw = os.environ.get(key, "").strip()
        if raw:
            roots.append(Path(raw).expanduser())
    catalog = os.environ.get("SCILLM_REVIEW_CATALOG_ROOT", "").strip()
    if catalog:
        roots.append(Path(catalog) / "skills")
    project_root = os.environ.get("SCILLM_PROJECT_ROOT", "").strip()
    if project_root:
        roots.append(Path(project_root) / "skills")
    roots.append(Path("/catalog/agent-skills/skills"))
    return roots


def _symlink_skills_roots() -> list[Path]:
    """Broadcast targets (~/.claude/skills, etc.). May be absent in exec containers."""
    roots: list[Path] = []
    for key in ("SCILLM_OPENCODE_SKILL_ROOTS", "SCILLM_SKILL_ROOTS"):
        raw = os.environ.get(key, "").strip()
        if raw:
            for part in raw.split(":"):
                part = part.strip()
                if part:
                    roots.append(Path(part).expanduser())
    home_skills = Path.home() / ".claude" / "skills"
    roots.append(home_skills)
    if home_skills != Path("/home/graham/.claude/skills"):
        roots.append(Path("/home/graham/.claude/skills"))
    return roots


def _skills_root_candidates() -> list[Path]:
    """Canonical trees first; symlink/IDE roots only as fallback."""
    candidates = _canonical_skills_roots() + _symlink_skills_roots()
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in candidates:
        if not path.is_dir():
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def skills_root() -> Path:
    roots = _skills_root_candidates()
    if roots:
        return roots[0]
    return Path("/catalog/agent-skills/skills")


def skill_run_sh(skill_dir_name: str) -> Path:
    last: Path | None = None
    for root in _skills_root_candidates():
        path = root / skill_dir_name / "run.sh"
        last = path
        if path.is_file():
            return path.resolve()
    raise FileNotFoundError(
        f"skill run.sh not found for {skill_dir_name!r} (last tried {last}); "
        "canonical tree is agent-skills/skills (container: /catalog/agent-skills/skills)"
    )


def run_skill_sh(
    skill_dir_name: str,
    argv: list[str],
    *,
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
    timeout_sec: int = 600,
) -> subprocess.CompletedProcess[str]:
    run_sh = skill_run_sh(skill_dir_name)
    env = os.environ.copy()
    env.setdefault("UV_PROJECT_ENVIRONMENT", f"/tmp/scillm-harness-venvs/{skill_dir_name}")
    env.setdefault("UV_CACHE_DIR", "/tmp/scillm-uv-cache")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(run_sh), *argv],
        cwd=str(cwd or run_sh.parent),
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        env=env,
        check=False,
    )
