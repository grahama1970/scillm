"""Shared types for curate pipeline.

Based on 02_SPEC.md definitions.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class PhaseStatus(str, Enum):
    """Status for pipeline phases."""

    OK = "ok"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"
    FAILED_SOFT = "failed_soft"
    RUNNING = "running"


@dataclass
class PhaseResult:
    """Result from a pipeline phase."""

    status: PhaseStatus
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)


@dataclass
class CurateConfig:
    """Configuration for curate pipeline.

    Merged from: defaults → file → env → CLI
    """

    # PDF settings
    pdf_enabled: bool = True
    pdf_remote: bool = True
    pdf_remote_domains: list[str] = field(
        default_factory=lambda: ["arxiv.org", "ieee.org", "acm.org", "openreview.net"]
    )
    pdf_allow_any_domain: bool = False
    pdf_max_remote: int = 10
    pdf_max_bytes: int = 52428800  # 50MB
    pdf_timeout_s: int = 90
    pdf_marker_llm_mode: bool = True

    # Lean settings
    lean_enabled: bool = True
    lean_max_theorems: int = 50
    lean_time_budget_s: int = 600
    lean_candidate_max: int = 2000
    lean_tactics: list[str] = field(
        default_factory=lambda: ["simp", "omega", "decide", "native_decide"]
    )

    # Treesitter settings
    treesitter_enabled: bool = True

    # Debug settings
    debug: bool = False
    debug_max_files: int = 50
    debug_verbose_artifacts: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "pdf": {
                "enabled": self.pdf_enabled,
                "remote": self.pdf_remote,
                "remote_domains": self.pdf_remote_domains,
                "allow_any_domain": self.pdf_allow_any_domain,
                "max_remote": self.pdf_max_remote,
                "max_bytes": self.pdf_max_bytes,
                "timeout_s": self.pdf_timeout_s,
                "marker_llm_mode": self.pdf_marker_llm_mode,
            },
            "lean": {
                "enabled": self.lean_enabled,
                "max_theorems": self.lean_max_theorems,
                "time_budget_s": self.lean_time_budget_s,
                "candidate_max": self.lean_candidate_max,
                "tactics": self.lean_tactics,
            },
            "treesitter": {
                "enabled": self.treesitter_enabled,
            },
            "debug": {
                "enabled": self.debug,
                "max_files": self.debug_max_files,
                "verbose_artifacts": self.debug_verbose_artifacts,
            },
        }


@dataclass
class RunContext:
    """Context for a curate pipeline run."""

    run_id: str
    code_path: Path
    scope: str
    config: CurateConfig
    artifacts_path: Path
    started_at: datetime
    is_git_repo: bool = False
    worktree_path: Optional[Path] = None
    repo_fingerprint: Optional[str] = None
    dry_run: bool = False

    # Phase results
    phase_results: dict[str, PhaseResult] = field(default_factory=dict)

    def get_phase_status(self, phase: str) -> PhaseStatus:
        """Get status for a phase."""
        if phase in self.phase_results:
            return self.phase_results[phase].status
        return PhaseStatus.RUNNING
