"""Memory sources commands: list and inspect available recall sources."""
from __future__ import annotations
from loguru import logger

import json

import typer

from ._helpers import app, _json_output

sources_app = typer.Typer(help="Query available memory sources for collection filtering.")
app.add_typer(sources_app, name="sources")


# Descriptions for built-in sources (helps agents understand what to query)
SOURCE_DESCRIPTIONS = {
    "task_states": "Agent task plans and reasoning states - useful for resuming interrupted work",
    "code_symbols": "Codebase symbols (functions, classes, methods) extracted via treesitter",
    "agent_conversations": "Episodic agent conversation turns with session context",
    "session_summaries": "LLM-assessed session analysis for nightly reflection",
    "doc_chunks": "Ingested documentation chunks from project docs (.md, .rst, .txt)",
    "datalake_chunks": "Extracted document content (PDFs, tables, figures) with control edges",
    "lean_theorems": "Formal Lean4 proofs from DeepSeek-Prover datasets",
    "sanity_scripts": "Executable code examples (working reference implementations)",
    "lesson_texts": "Full-text lesson content for detailed problem/solution pairs",
    "lean4_proofs": "DeepSeek-Prover V2 proofs for few-shot theorem proving",
    "lean4_autoformalization": "English↔Lean4 autoformalization pairs for RAG proving",
    "skill_descriptions": "Skill capability descriptions for overlap detection",
    "lessons": "Core lessons - problem/solution pairs with scope and tags",
    "horus_lore": "Horus persona lore - 40K canon, character knowledge, trauma triggers",
    # SPARTA pipeline collections (11K controls, 218K QRAs, 131K relationships)
    "sparta_controls": "SPARTA/ATT&CK/NIST/CWE/D3FEND controls (11K) with framework metadata",
    "sparta_qra": "Question-Reasoning-Answer pairs (218K) with grounding scores and mind tags",
    "sparta_relationships": "Cross-framework relationship edges (131K) with NRS scores",
    "technique_knowledge": "Technique-level ground truth from URL content extraction",
    "sparta_urls": "Fetched URL metadata (6.8K) — url, domain, fetch status",
    "sparta_url_knowledge": "Extracted knowledge chunks (42K) from fetched URLs — text, topic, control mappings",
}


@sources_app.command("list")
def sources_list(
    available_only: bool = typer.Option(
        True, "--available/--all",
        help="Only show sources with existing database views (default: available only)"
    ),
    json_output: bool = typer.Option(
        False, "--json", "-j",
        help="Output as JSON for programmatic use"
    ),
) -> None:
    """List available memory sources for --collections filtering.

    Shows what sources are available for the `recall --collections` filter.
    Use this to know what data you can search.

    Example:
        memory-agent sources list
        memory-agent sources list --json

        # Then use in recall:
        memory-agent recall --q "siege tactics" --collections horus_lore
        memory-agent recall --q "pdf extraction" --collections lessons,sanity_scripts
    """
    from ..lessons.recall_sources import builtin_sources, load_supplemental_sources

    sources_list_output = []

    if available_only:
        # Get actually available sources (views exist in DB)
        try:
            from ..arango_client import get_db
            db = get_db()
            sources = load_supplemental_sources(db)
        except Exception as e:
            if json_output:
                _json_output({"error": str(e), "sources": []})
            else:
                typer.echo(f"[WARN] Could not connect to database: {e}", err=True)
                typer.echo("Showing all built-in sources (availability unknown):", err=True)
            sources = builtin_sources()
    else:
        sources = builtin_sources()

    # Always add "lessons" as core source (it's always available)
    core_sources = [
        {
            "name": "lessons",
            "kind": "problem_solution",
            "description": SOURCE_DESCRIPTIONS.get("lessons", "Core lesson database"),
            "available": True,
        }
    ]

    # Check for horus_lore collection if DB available
    try:
        from ..arango_client import get_db
        db = get_db()
        if db.has_collection("horus_lore_docs"):
            core_sources.append({
                "name": "horus_lore",
                "kind": "persona_knowledge",
                "description": SOURCE_DESCRIPTIONS.get("horus_lore", "Persona lore and knowledge"),
                "available": True,
            })
    except Exception as exc:
        logger.error("Suppressed error in sources: {}", exc)

    for src in sources:
        sources_list_output.append({
            "name": src.name,
            "kind": src.kind,
            "description": SOURCE_DESCRIPTIONS.get(src.name, f"{src.kind} data source"),
            "limit": src.limit,
            "weight": src.weight,
            "include_edges": src.include_edges,
            "available": True,
        })

    # Combine core + supplemental, dedupe by name
    seen = set()
    all_sources = []
    for src in core_sources + sources_list_output:
        if src["name"] not in seen:
            all_sources.append(src)
            seen.add(src["name"])

    if json_output:
        _json_output({
            "sources": all_sources,
            "count": len(all_sources),
            "usage": "memory-agent recall --q 'query' --collections <name1>,<name2>",
        })
    else:
        typer.echo("\n📚 Available Memory Sources")
        typer.echo("=" * 60)
        typer.echo("\nUse these names with: recall --collections <name1>,<name2>\n")

        for src in all_sources:
            status = "✓" if src.get("available") else "?"
            typer.echo(f"  {status} {src['name']:<25} ({src['kind']})")
            typer.echo(f"    └─ {src['description']}")

        typer.echo("\n" + "-" * 60)
        typer.echo("Examples:")
        typer.echo("  memory-agent recall --q 'siege tactics' --collections horus_lore")
        typer.echo("  memory-agent recall --q 'pdf extraction' --collections lessons,sanity_scripts")
        typer.echo("  memory-agent recall --q 'ImportError' --collections agent_conversations")
        typer.echo("  memory-agent recall --q 'GPS spoofing' --collections sparta_qra")
        typer.echo("  memory-agent recall --q 'T0100' --collections sparta_controls")
        typer.echo("")


@sources_app.command("info")
def sources_info(
    name: str = typer.Argument(..., help="Source name to get details about"),
) -> None:
    """Get detailed information about a specific source.

    Example:
        memory-agent sources info horus_lore
        memory-agent sources info sanity_scripts
    """
    from ..lessons.recall_sources import builtin_sources, load_supplemental_sources, RecallSource

    # Check built-ins first
    for src in builtin_sources():
        if src.name == name:
            info = {
                "name": src.name,
                "kind": src.kind,
                "description": SOURCE_DESCRIPTIONS.get(src.name, f"{src.kind} data source"),
                "view": src.view,
                "collection": src.collection,
                "text_fields": src.text_fields,
                "result_fields": src.result_fields,
                "limit": src.limit,
                "weight": src.weight,
                "include_edges": src.include_edges,
                "edge_limit": src.edge_limit if src.include_edges else None,
            }
            _json_output(info)
            return

    # Check special sources
    if name == "lessons":
        _json_output({
            "name": "lessons",
            "kind": "problem_solution",
            "description": SOURCE_DESCRIPTIONS.get("lessons"),
            "note": "Core source - always queried by default in recall",
        })
        return

    if name == "horus_lore":
        _json_output({
            "name": "horus_lore",
            "kind": "persona_knowledge",
            "description": SOURCE_DESCRIPTIONS.get("horus_lore"),
            "note": "Query via: memory-agent lore query horus --q 'query'",
        })
        return

    typer.echo(f"Source '{name}' not found. Use 'sources list' to see available sources.", err=True)
    raise typer.Exit(1)
