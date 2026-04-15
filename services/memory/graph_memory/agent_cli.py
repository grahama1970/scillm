"""Memory agent CLI — thin shell that re-exports sub-app commands.

Two commands for agents: recall and learn.
One command for setup: ingest.
Everything else is operator maintenance (use lessons-* commands).
"""
from __future__ import annotations

from loguru import logger as _logger

# Best-effort .env auto-loading
try:
    from dotenv import load_dotenv, find_dotenv
except Exception as exc:
    _logger.debug("dotenv import failed: {}", exc)
    load_dotenv = None
    find_dotenv = None

if load_dotenv and find_dotenv:
    try:
        _env_path = find_dotenv(usecwd=True)
        load_dotenv(_env_path or None)
    except Exception as exc:
        _logger.debug("dotenv loading failed: {}", exc)

from .cli import app  # noqa: F401 — entry point for pyproject.toml

if __name__ == "__main__":
    app()
