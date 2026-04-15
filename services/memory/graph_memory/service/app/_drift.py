"""Cognitive drift endpoint — teacher-vs-classifier agreement metrics.

Reads shadow JSONL files written by classifier pipelines and computes
per-scope agreement rates between LLM "teacher" labels and trained
classifier predictions.

Inputs:
    - Shadow JSONL files at well-known paths under ~/.pi/
    - Query params: threshold (float), min_samples (int)

Outputs:
    - JSON dict with per-scope agreement rates, drift flags, and source info

Failure modes:
    - Shadow files missing → HTTP 200 with status="no_data" (expected)
    - Corrupted JSONL lines → skipped with warning log, partial results returned
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter
from loguru import logger

router = APIRouter()

SHADOW_SOURCES: dict[str, dict] = {
    "taxonomy": {
        "path": Path.home() / ".pi/monitor-personas/shadow_taxonomy.jsonl",
        "agreement_key": "agreement",
        "scope_key": "scope",
    },
    "extraction": {
        "path": Path.home() / ".pi/doc2qra/shadow.jsonl",
        "agreement_key": "agreed",
        "scope_key": "scope",
    },
    "stress": {
        "path": Path.home() / ".pi/assistant/shadow_deltas.jsonl",
        "agreement_key": "target_met",
        "scope_key": "persona",
    },
}

DRIFT_THRESHOLD = 0.80


def _read_shadow_sources(
    threshold: float,
    min_samples: int,
) -> dict:
    """Read all shadow JSONL files and compute per-scope agreement metrics."""
    buckets: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"agreed": 0, "total": 0},
    )
    sources_read: list[str] = []
    sources_missing: list[str] = []
    total_entries = 0

    for source_name, cfg in SHADOW_SOURCES.items():
        path: Path = cfg["path"]
        if not path.exists():
            sources_missing.append(source_name)
            continue
        sources_read.append(source_name)
        agree_key: str = cfg["agreement_key"]
        scope_key: str = cfg["scope_key"]

        with open(path) as f:
            for line_no, line in enumerate(f, 1):
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError) as exc:
                    logger.warning(
                        "Corrupt JSONL line in {} at line {}: {}",
                        path,
                        line_no,
                        exc,
                    )
                    continue
                scope = entry.get(scope_key, "unknown")
                agreed = entry.get(agree_key)
                if agreed is None:
                    continue
                key = (source_name, scope)
                buckets[key]["total"] += 1
                if agreed:
                    buckets[key]["agreed"] += 1
                total_entries += 1

    # No data at all — expected when classifiers haven't been trained yet
    if not sources_read:
        logger.info(
            "No shadow JSONL files found (missing: {}). No classifiers trained yet.",
            ", ".join(sources_missing),
        )
        return {"status": "no_data", "metrics": {}}

    # Build per-scope results
    all_scopes: list[dict] = []
    drifting: list[dict] = []

    for (source, scope), counts in buckets.items():
        total = counts["total"]
        agreed = counts["agreed"]
        rate = agreed / total if total > 0 else 0.0
        entry = {
            "scope": scope,
            "source": source,
            "rate": round(rate, 3),
            "agreed": agreed,
            "total": total,
        }
        all_scopes.append(entry)
        if rate < threshold and total >= min_samples:
            drifting.append(entry)

    all_scopes.sort(key=lambda x: x["rate"])

    return {
        "total_entries": total_entries,
        "sources_read": sources_read,
        "sources_missing": sources_missing,
        "drifting_count": len(drifting),
        "drifting": drifting,
        "all_scopes": all_scopes,
    }


@router.get("/drift")
def get_drift_metrics(
    threshold: float = DRIFT_THRESHOLD,
    min_samples: int = 10,
) -> dict:
    """Teacher-vs-classifier agreement per scope.

    Returns drift metrics from shadow JSONL files. Scopes below
    *threshold* with at least *min_samples* entries are flagged as drifting.
    """
    return _read_shadow_sources(threshold=threshold, min_samples=min_samples)
