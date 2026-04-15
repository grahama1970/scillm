"""Curate pipeline for graph-memory codebase ingestion.

Pipeline phases:
- P1: init - Create run context, validate inputs, resolve config
- P2: ingest - Discover files, extract symbols, chunk docs
- P3: pdf_pipeline - Discover, fetch, extract PDFs
- P4: lean_pipeline - Extract candidates, compile theorems
- P5: finalize - Write artifacts, update run record
"""

from graph_memory.codebase.pipeline.types import (
    CurateConfig,
    PhaseResult,
    PhaseStatus,
    RunContext,
)

__all__ = [
    "CurateConfig",
    "PhaseResult",
    "PhaseStatus",
    "RunContext",
]
