"""Codebase ingestion and retrieval for graph-memory.

This module provides:
- curate: Ingest code, docs, PDFs, and formal theorems
- search: Query code symbols and theorems
"""


def __getattr__(name: str):
    """Lazy import for CLI app (requires typer)."""
    if name == "app":
        from graph_memory.codebase.cli import app
        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["app"]
