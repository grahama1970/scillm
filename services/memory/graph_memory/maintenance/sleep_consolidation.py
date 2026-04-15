#!/usr/bin/env python3
"""Sleep-Time Memory Consolidation — orchestrated nightly cycle.

Wires existing modules into a coherent consolidation pipeline:
  Phase 1: Decay + Dedup (ltm.consolidate)
  Phase 2: Audit graph (audit.contradictions)
  Phase 3: Quality re-check recent lessons
  Phase 4: Day-residue rehearsal (residue.fetch_day_residue)
  Phase 5: Retention scoring + archival (ltm.retain)

Usage:
    python -m graph_memory.maintenance.sleep_consolidation
    python -m graph_memory.maintenance.sleep_consolidation --dry-run
    memory-agent consolidate --dry-run
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from loguru import logger

# Best-effort .env
try:
    from dotenv import load_dotenv, find_dotenv
    _env = find_dotenv(usecwd=True)
    if _env:
        load_dotenv(_env, override=False)
except Exception as exc:
    logger.error("dotenv loading skipped: {}", exc)


@dataclass
class PhaseResult:
    name: str
    ok: bool = True
    items_processed: int = 0
    items_changed: int = 0
    elapsed_ms: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)


@dataclass
class ConsolidationReport:
    started_at: str = ""
    completed_at: str = ""
    total_ms: float = 0.0
    dry_run: bool = True
    phases: List[PhaseResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(p.ok for p in self.phases)

    def summary(self) -> str:
        lines = [f"Sleep Consolidation {'(DRY RUN) ' if self.dry_run else ''}Report"]
        lines.append(f"  Duration: {self.total_ms:.0f}ms")
        for p in self.phases:
            status = "OK" if p.ok else "FAIL"
            lines.append(f"  {p.name}: {status} ({p.items_processed} processed, {p.items_changed} changed)")
            if p.errors:
                for e in p.errors[:3]:
                    lines.append(f"    ERROR: {e}")
        return "\n".join(lines)


def _get_db():
    from graph_memory.arango_client import get_db
    return get_db()


def _phase_dedup(db, scope: str, dry_run: bool) -> PhaseResult:
    """Phase 1: Consolidate near-duplicate lessons."""
    t0 = time.monotonic()
    result = PhaseResult(name="dedup")
    try:
        from graph_memory.lessons import ltm
        from typer.testing import CliRunner

        runner = CliRunner()
        args = ["--scope", scope, "--json"]
        if not dry_run:
            args.append("--apply")
        res = runner.invoke(ltm.app, ["consolidate"] + args, catch_exceptions=False)
        output = json.loads(res.stdout) if res.stdout else {}
        meta = output.get("meta", {})
        items = output.get("items", [])
        result.items_processed = len(items)
        result.items_changed = meta.get("changed", 0)
        result.details = {"min_title_jaccard": meta.get("min_title_jaccard", 0.85)}
    except Exception as e:
        result.ok = False
        result.errors.append(str(e))
    result.elapsed_ms = (time.monotonic() - t0) * 1000
    return result


def _phase_audit(db, scope: str) -> PhaseResult:
    """Phase 2: Audit graph for contradictions and orphans."""
    t0 = time.monotonic()
    result = PhaseResult(name="audit")
    try:
        from graph_memory.lessons.audit import contradictions
        audit_data = contradictions(scope=scope)
        issues = audit_data.get("items", [])
        result.items_processed = len(issues)
        # Categorize issues
        by_kind: Dict[str, int] = {}
        for issue in issues:
            kind = issue.get("kind", "unknown")
            by_kind[kind] = by_kind.get(kind, 0) + 1
        result.details = {"issues_by_kind": by_kind}
    except Exception as e:
        result.ok = False
        result.errors.append(str(e))
    result.elapsed_ms = (time.monotonic() - t0) * 1000
    return result


def _phase_quality_check(db, hours: int = 48) -> PhaseResult:
    """Phase 3: Quality re-check recently added lessons."""
    t0 = time.monotonic()
    result = PhaseResult(name="quality_check")
    try:
        cutoff = int(time.time()) - (hours * 3600)
        recent = list(db.aql.execute(
            """
            FOR l IN lessons_v2
              FILTER l.updated_at >= @cutoff AND l.status == 'active'
              SORT l.updated_at DESC
              LIMIT 100
              RETURN KEEP(l, ['_key', 'title', 'problem', 'solution', 'scope'])
            """,
            bind_vars={"cutoff": cutoff},
        ))
        result.items_processed = len(recent)

        # Use Tier 1 provability check (fast, no LLM needed)
        try:
            from graph_memory.integrations.proof_assessment import get_provability_score
            provable_count = 0
            for lesson in recent:
                text = f"{lesson.get('problem', '')} {lesson.get('solution', '')}"
                score = get_provability_score(text)
                if score.get("likely_provable"):
                    provable_count += 1
            result.details = {
                "recent_lessons": len(recent),
                "likely_provable": provable_count,
                "hours_window": hours,
            }
        except ImportError:
            result.details = {"recent_lessons": len(recent), "provability_check": "skipped"}
    except Exception as e:
        result.ok = False
        result.errors.append(str(e))
    result.elapsed_ms = (time.monotonic() - t0) * 1000
    return result


def _phase_residue(db, limit: int = 20) -> PhaseResult:
    """Phase 4: Day-residue rehearsal — fetch weighted temporal memories."""
    t0 = time.monotonic()
    result = PhaseResult(name="day_residue")
    try:
        from graph_memory.lessons.residue import fetch_day_residue
        items = fetch_day_residue(db, limit=limit)
        result.items_processed = len(items)

        # Categorize by type
        by_type: Dict[str, int] = {}
        for item in items:
            t = item.get("type", "unknown")
            by_type[t] = by_type.get(t, 0) + 1
        result.details = {"items_by_type": by_type}
    except Exception as e:
        result.ok = False
        result.errors.append(str(e))
    result.elapsed_ms = (time.monotonic() - t0) * 1000
    return result


def _phase_retain(db, scope: str, dry_run: bool) -> PhaseResult:
    """Phase 5: Retention scoring — archive low-value lessons."""
    t0 = time.monotonic()
    result = PhaseResult(name="retention")
    try:
        from graph_memory.lessons import ltm
        from typer.testing import CliRunner

        runner = CliRunner()
        args = ["--scope", scope, "--json"]
        if not dry_run:
            args.append("--apply")
        res = runner.invoke(ltm.app, ["retain"] + args, catch_exceptions=False)
        output = json.loads(res.stdout) if res.stdout else {}
        meta = output.get("meta", {})
        items = output.get("items", [])
        result.items_processed = len(items)
        result.items_changed = meta.get("changed", 0)
        result.details = {
            "min_score": meta.get("min_score", 0.10),
            "half_life_days": meta.get("half_life_days", 90.0),
        }
    except Exception as e:
        result.ok = False
        result.errors.append(str(e))
    result.elapsed_ms = (time.monotonic() - t0) * 1000
    return result


def run_consolidation_cycle(
    scope: str = "",
    dry_run: bool = True,
    residue_limit: int = 20,
    quality_hours: int = 48,
) -> ConsolidationReport:
    """Run the full sleep-time consolidation cycle."""
    from datetime import datetime, timezone

    report = ConsolidationReport(
        started_at=datetime.now(timezone.utc).isoformat(),
        dry_run=dry_run,
    )
    t0 = time.monotonic()

    db = _get_db()

    # Phase 1: Dedup
    report.phases.append(_phase_dedup(db, scope, dry_run))

    # Phase 2: Audit
    report.phases.append(_phase_audit(db, scope))

    # Phase 3: Quality check recent
    report.phases.append(_phase_quality_check(db, hours=quality_hours))

    # Phase 4: Day residue
    report.phases.append(_phase_residue(db, limit=residue_limit))

    # Phase 5: Retention
    report.phases.append(_phase_retain(db, scope, dry_run))

    report.completed_at = datetime.now(timezone.utc).isoformat()
    report.total_ms = (time.monotonic() - t0) * 1000

    # Log consolidation event
    try:
        from graph_memory.events import log_event
        log_event(db, "sleep_consolidation", "cycle_complete", {
            "dry_run": dry_run,
            "scope": scope,
            "total_ms": report.total_ms,
            "phases_ok": sum(1 for p in report.phases if p.ok),
            "phases_total": len(report.phases),
        })
    except Exception as exc:
        logger.error("failed to log consolidation event: {}", exc)

    return report


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Sleep-time memory consolidation")
    parser.add_argument("--scope", default="", help="Scope filter (default: all)")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Preview only, no writes (default)")
    parser.add_argument("--apply", action="store_true",
                        help="Apply changes (requires LTM_RETENTION_ENABLE=1)")
    parser.add_argument("--residue-limit", type=int, default=20,
                        help="Day residue items to fetch")
    parser.add_argument("--quality-hours", type=int, default=48,
                        help="Hours window for quality re-check")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    args = parser.parse_args()

    dry_run = not args.apply

    report = run_consolidation_cycle(
        scope=args.scope,
        dry_run=dry_run,
        residue_limit=args.residue_limit,
        quality_hours=args.quality_hours,
    )

    if args.json:
        print(json.dumps(asdict(report), indent=2, default=str))
    else:
        print(report.summary())
        if not report.ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
