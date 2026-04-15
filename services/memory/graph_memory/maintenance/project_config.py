"""Project configuration for memory ingestion.

Defines which projects are enabled for nightly automation,
their paths, scopes, and ingestion settings.

Usage:
    from graph_memory.maintenance.project_config import PROJECTS, get_enabled_projects

    for project in get_enabled_projects():
        print(f"Ingesting {project['name']}...")
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, TypedDict


class ProjectConfig(TypedDict):
    """Project configuration for memory ingestion."""
    name: str
    path: str
    database: str
    scope: str
    enabled: bool
    # Ingestion settings
    ingest_code: bool      # treesitter code symbols
    ingest_docs: bool      # markdown, PDF extraction
    ingest_lean: bool      # Lean4 theorems
    embed: bool            # generate embeddings
    verify_edges: bool     # KNN + LLM edge verification
    # Resource limits
    max_files: int         # max files to scan per run
    max_llm_calls: int     # max LLM calls for edge verification


# Project definitions
PROJECTS: List[ProjectConfig] = [
    {
        "name": "memory",
        "path": "/home/graham/workspace/experiments/memory",
        "database": "memory",
        "scope": "memory",
        "enabled": True,
        "ingest_code": True,
        "ingest_docs": True,
        "ingest_lean": True,
        "embed": True,
        "verify_edges": True,
        "max_files": 500,
        "max_llm_calls": 100,
    },
    {
        "name": "pi-mono",
        "path": "/home/graham/workspace/experiments/pi-mono",
        "database": "memory",
        "scope": "pi-mono",
        "enabled": True,
        "ingest_code": True,
        "ingest_docs": True,
        "ingest_lean": False,
        "embed": True,
        "verify_edges": True,
        "max_files": 1000,
        "max_llm_calls": 100,
    },
    {
        "name": "litellm",
        "path": "/home/graham/workspace/experiments/litellm",
        "database": "memory",
        "scope": "litellm",
        "enabled": True,
        "ingest_code": True,
        "ingest_docs": True,
        "ingest_lean": False,
        "embed": True,
        "verify_edges": False,  # too many files, skip LLM verification
        "max_files": 500,
        "max_llm_calls": 0,
    },
    {
        "name": "devops",
        "path": "/home/graham/workspace/experiments/devops",
        "database": "sparta",
        "scope": "devops",
        "enabled": True,
        "ingest_code": True,
        "ingest_docs": True,
        "ingest_lean": True,
        "embed": True,
        "verify_edges": True,
        "max_files": 500,
        "max_llm_calls": 200,
    },
    {
        "name": "lean4",
        "path": "/home/graham/workspace/experiments/lean4",
        "database": "memory",
        "scope": "lean4",
        "enabled": True,
        "ingest_code": True,
        "ingest_docs": True,
        "ingest_lean": True,
        "embed": True,
        "verify_edges": True,
        "max_files": 200,
        "max_llm_calls": 50,
    },
    # Add more projects as needed
]


def get_enabled_projects() -> List[ProjectConfig]:
    """Get list of projects enabled for automation."""
    return [p for p in PROJECTS if p["enabled"]]


def get_project(name: str) -> ProjectConfig | None:
    """Get project config by name."""
    for p in PROJECTS:
        if p["name"] == name:
            return p
    return None


def get_projects_by_database(database: str) -> List[ProjectConfig]:
    """Get all projects using a specific database."""
    return [p for p in PROJECTS if p["database"] == database and p["enabled"]]
