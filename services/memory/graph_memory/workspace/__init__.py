"""Workspace-aware helpers (detect, ingest, build).

Kept isolated from the core lessons recall stack so consumers can opt in
without touching existing agent surfaces or MCP tooling.
"""

from .detect import detect_workspace  # re-export for callers
