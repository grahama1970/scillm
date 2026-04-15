"""P1: init phase for curate pipeline.

Purpose: Setup run context, validate inputs, resolve config.

Assertions (from 02_SPEC.md):
- run_id created (UUID v4)
- runs record created (status=running)
- code_path exists (Directory)
- Config resolved (Merged: defaults → file → env → CLI)
- Worktree created (if git repo)
"""

import hashlib
import json
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from graph_memory.codebase.pipeline.config import resolve_config
from graph_memory.codebase.pipeline.types import (
    CurateConfig,
    PhaseResult,
    PhaseStatus,
    RunContext,
)


def is_git_repo(path: Path) -> bool:
    """Check if path is inside a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def is_git_repo_root(path: Path) -> bool:
    """Check if path is the root of a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            toplevel = Path(result.stdout.strip()).resolve()
            return toplevel == path.resolve()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return False


def get_repo_fingerprint(path: Path) -> Optional[str]:
    """Get SHA of HEAD commit for repo fingerprint."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def create_worktree(code_path: Path, run_id: str) -> Optional[Path]:
    """Create a detached worktree for the run.

    Returns the worktree path or None if creation fails.
    """
    worktree_dir = Path(tempfile.mkdtemp(prefix=f"curate-{run_id[:8]}-"))
    try:
        # Create detached worktree at HEAD
        result = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree_dir)],
            cwd=code_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return worktree_dir
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Cleanup on failure
    if worktree_dir.exists():
        import shutil
        shutil.rmtree(worktree_dir, ignore_errors=True)
    return None


def run_p1_init(
    code_path: Path,
    scope: str = "code",
    debug: bool = False,
    dry_run: bool = False,
    cli_overrides: Optional[dict[str, Any]] = None,
    artifacts_base: Optional[Path] = None,
) -> tuple[RunContext, PhaseResult]:
    """Execute P1: init phase.

    Args:
        code_path: Path to codebase to ingest
        scope: Scope for ingested items
        debug: Enable debug mode
        dry_run: Validate without writing to DB
        cli_overrides: Additional config overrides from CLI
        artifacts_base: Base path for artifacts (default: run/artifacts)

    Returns:
        (RunContext, PhaseResult) tuple
    """
    errors: list[dict[str, Any]] = []
    warnings: list[str] = []

    # Generate run_id
    run_id = str(uuid.uuid4())

    # Validate code_path
    code_path = code_path.resolve()
    if not code_path.is_dir():
        errors.append({
            "code": "INVALID_CODE_PATH",
            "message": f"code_path '{code_path}' is not a directory",
            "phase": "init",
        })
        # Fatal error - return early
        return _create_failed_context(
            run_id, code_path, scope, debug, dry_run, errors
        )

    # Resolve config
    config = resolve_config(code_path, debug=debug, cli_overrides=cli_overrides)

    # Check if git repo
    git_repo = is_git_repo(code_path)
    git_repo_root = is_git_repo_root(code_path)
    repo_fingerprint = None
    worktree_path = None

    if git_repo:
        repo_fingerprint = get_repo_fingerprint(code_path)
        if not repo_fingerprint:
            warnings.append("Could not get repo fingerprint (HEAD)")

        # Only create worktree if code_path is the git repo root
        # (not a subdirectory of a larger repo)
        if git_repo_root:
            worktree_path = create_worktree(code_path, run_id)
            if not worktree_path:
                warnings.append("Could not create worktree, using code_path directly")
        else:
            warnings.append("code_path is inside a git repo but not at root, skipping worktree")

    # Setup artifacts directory
    if artifacts_base is None:
        artifacts_base = Path("run/artifacts")
    artifacts_path = artifacts_base / run_id
    artifacts_path.mkdir(parents=True, exist_ok=True)

    # Create P1 artifacts directory
    p1_artifacts = artifacts_path / "p1_init"
    p1_artifacts.mkdir(parents=True, exist_ok=True)

    # Create RunContext
    started_at = datetime.now(timezone.utc)
    context = RunContext(
        run_id=run_id,
        code_path=code_path,
        scope=scope,
        config=config,
        artifacts_path=artifacts_path,
        started_at=started_at,
        is_git_repo=git_repo,
        worktree_path=worktree_path,
        repo_fingerprint=repo_fingerprint,
        dry_run=dry_run,
    )

    # Write init_summary.json (for gate validation)
    summary = {
        "run_id": run_id,
        "code_path": str(code_path),
        "scope": scope,
        "started_at": started_at.isoformat(),
        "is_git_repo": git_repo,
        "repo_fingerprint": repo_fingerprint,
        "worktree_path": str(worktree_path) if worktree_path else None,
        "config": config.to_dict(),
        "runs_record": {
            "status": "running",
            "_key": run_id,
        },
        "debug": debug,
        "dry_run": dry_run,
    }

    summary_file = p1_artifacts / "init_summary.json"
    summary_file.write_text(json.dumps(summary, indent=2))

    # Create PhaseResult
    phase_result = PhaseResult(
        status=PhaseStatus.OK if not errors else PhaseStatus.FAILED_SOFT,
        counts={
            "config_sources": 3,  # defaults, file, env
        },
        errors=errors,
        warnings=warnings,
        artifacts={
            "init_summary": str(summary_file),
        },
    )

    context.phase_results["init"] = phase_result

    return context, phase_result


def _create_failed_context(
    run_id: str,
    code_path: Path,
    scope: str,
    debug: bool,
    dry_run: bool,
    errors: list[dict[str, Any]],
) -> tuple[RunContext, PhaseResult]:
    """Create a failed context for early exit."""
    config = CurateConfig(debug=debug)
    started_at = datetime.now(timezone.utc)

    # Create minimal artifacts path
    artifacts_path = Path("run/artifacts") / run_id
    artifacts_path.mkdir(parents=True, exist_ok=True)

    context = RunContext(
        run_id=run_id,
        code_path=code_path,
        scope=scope,
        config=config,
        artifacts_path=artifacts_path,
        started_at=started_at,
        dry_run=dry_run,
    )

    phase_result = PhaseResult(
        status=PhaseStatus.FAILED_SOFT,
        errors=errors,
    )

    context.phase_results["init"] = phase_result

    return context, phase_result
