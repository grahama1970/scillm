"""TODO management commands: add, list, complete, cancel, delete, search."""
from __future__ import annotations
from loguru import logger

import time
from typing import Any, Dict, List, Optional

import typer

from ._helpers import app, _json_output
from ..arango_client import get_db

todo_app = typer.Typer(help="Manage TODOs - actionable items that don't pollute lesson recall.")
app.add_typer(todo_app, name="todo")


@todo_app.command("add")
def todo_add(
    title: str = typer.Option(..., "--title", "-t", help="Short title for the TODO"),
    description: str = typer.Option("", "--description", "-d", help="Detailed description"),
    scope: str = typer.Option("", "--scope", "-s", help="Project scope"),
    priority: str = typer.Option("medium", "--priority", "-p", help="Priority: high, medium, low"),
    tags: Optional[List[str]] = typer.Option(None, "--tag", help="Tags for categorization"),
) -> None:
    """Add a new TODO item.

    TODOs are actionable items that should NOT pollute lesson recall.
    Use this instead of 'learn' for:
    - Infrastructure improvements
    - Future enhancements
    - Bugs to fix
    - Research tasks

    Example:
        memory-agent todo add --title "Add semantic dedup to learn()" --priority high
        memory-agent todo add -t "Research graph algorithms" -d "For multi-hop traversal" --tag research
    """
    from ..setup_schema import ensure_collections_and_view

    ensure_collections_and_view()
    db = get_db()
    todos = db.collection("todos")

    if priority not in ("high", "medium", "low"):
        typer.echo(f"Invalid priority: {priority}. Use: high, medium, low", err=True)
        raise typer.Exit(1)

    now = int(time.time())
    doc = {
        "title": title,
        "description": description,
        "scope": scope,
        "priority": priority,
        "status": "pending",
        "tags": tags or [],
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
        "converted_to_lesson": None,
    }

    result = todos.insert(doc)
    doc["_key"] = result["_key"]

    _json_output({
        "status": "created",
        "key": result["_key"],
        "title": title,
        "priority": priority,
    })


@todo_app.command("list")
def todo_list(
    status: str = typer.Option("pending", "--status", "-s", help="Filter by status: pending, in_progress, completed, cancelled, all"),
    scope: str = typer.Option("", "--scope", help="Filter by scope"),
    priority: str = typer.Option("", "--priority", "-p", help="Filter by priority"),
    limit: int = typer.Option(20, "--limit", "-l", help="Max results"),
) -> None:
    """List TODO items.

    Example:
        memory-agent todo list
        memory-agent todo list --status all
        memory-agent todo list --priority high --status pending
    """
    from ..setup_schema import ensure_collections_and_view

    ensure_collections_and_view()
    db = get_db()

    if not db.has_collection("todos"):
        _json_output({"items": [], "count": 0})
        return

    # Build AQL query
    filters = []
    bind_vars: Dict[str, Any] = {"limit": limit}

    if status != "all":
        filters.append("doc.status == @status")
        bind_vars["status"] = status

    if scope:
        filters.append("doc.scope == @scope")
        bind_vars["scope"] = scope

    if priority:
        filters.append("doc.priority == @priority")
        bind_vars["priority"] = priority

    filter_clause = " AND ".join(filters) if filters else "true"
    query = f"""
    FOR doc IN todos
        FILTER {filter_clause}
        SORT doc.priority == "high" ? 0 : doc.priority == "medium" ? 1 : 2, doc.created_at DESC
        LIMIT @limit
        RETURN doc
    """

    items = list(db.aql.execute(query, bind_vars=bind_vars))

    _json_output({
        "items": items,
        "count": len(items),
        "filters": {"status": status, "scope": scope, "priority": priority},
    })


@todo_app.command("complete")
def todo_complete(
    key: str = typer.Argument(..., help="TODO _key to complete"),
    as_lesson: bool = typer.Option(False, "--as-lesson", "-l", help="Convert to a lesson"),
    solution: str = typer.Option("", "--solution", "-s", help="Solution (required if --as-lesson)"),
) -> None:
    """Complete a TODO item.

    Optionally converts it to a lesson for future recall.

    Example:
        memory-agent todo complete abc123
        memory-agent todo complete abc123 --as-lesson --solution "Fixed by adding dedup check"
    """
    db = get_db()
    todos = db.collection("todos")

    if not todos.has(key):
        typer.echo(f"TODO not found: {key}", err=True)
        raise typer.Exit(1)

    todo = todos.get(key)
    now = int(time.time())

    lesson_key = None
    if as_lesson:
        if not solution:
            typer.echo("--solution is required when using --as-lesson", err=True)
            raise typer.Exit(1)

        # Create lesson from TODO
        from .. import api
        client = api.MemoryClient(scope=todo.get("scope", ""))
        result = client.learn(
            problem=todo["title"],
            solution=solution,
            tags=todo.get("tags", []),
        )
        lesson_key = result.get("key")

    # Update TODO status
    todos.update({
        "_key": key,
        "status": "completed",
        "completed_at": now,
        "updated_at": now,
        "converted_to_lesson": lesson_key,
    })

    output = {
        "status": "completed",
        "key": key,
        "title": todo["title"],
    }
    if lesson_key:
        output["converted_to_lesson"] = lesson_key

    _json_output(output)


@todo_app.command("cancel")
def todo_cancel(
    key: str = typer.Argument(..., help="TODO _key to cancel"),
    reason: str = typer.Option("", "--reason", "-r", help="Reason for cancellation"),
) -> None:
    """Cancel a TODO item.

    Example:
        memory-agent todo cancel abc123 --reason "No longer needed"
    """
    db = get_db()
    todos = db.collection("todos")

    if not todos.has(key):
        typer.echo(f"TODO not found: {key}", err=True)
        raise typer.Exit(1)

    todo = todos.get(key)
    now = int(time.time())

    todos.update({
        "_key": key,
        "status": "cancelled",
        "cancelled_reason": reason,
        "updated_at": now,
    })

    _json_output({
        "status": "cancelled",
        "key": key,
        "title": todo["title"],
        "reason": reason,
    })


@todo_app.command("delete")
def todo_delete(
    key: str = typer.Argument(..., help="TODO _key to delete"),
    force: bool = typer.Option(False, "--force", "-f", help="Delete without confirmation"),
) -> None:
    """Delete a TODO item permanently.

    Example:
        memory-agent todo delete abc123
        memory-agent todo delete abc123 --force
    """
    db = get_db()
    todos = db.collection("todos")

    if not todos.has(key):
        typer.echo(f"TODO not found: {key}", err=True)
        raise typer.Exit(1)

    todo = todos.get(key)

    if not force:
        typer.echo(f"Delete TODO: {todo['title']}")
        typer.echo(f"  Status: {todo['status']}")
        typer.echo(f"  Priority: {todo['priority']}")
        confirm = typer.confirm("Are you sure?")
        if not confirm:
            raise typer.Exit(0)

    todos.delete(key)

    _json_output({
        "status": "deleted",
        "key": key,
        "title": todo["title"],
    })


@todo_app.command("search")
def todo_search(
    q: str = typer.Option(..., "--q", "-q", help="Search query"),
    status: str = typer.Option("all", "--status", "-s", help="Filter by status"),
    limit: int = typer.Option(10, "--limit", "-l", help="Max results"),
) -> None:
    """Search TODOs using BM25 text search.

    Example:
        memory-agent todo search --q "deduplication"
        memory-agent todo search --q "graph" --status pending
    """
    from ..setup_schema import ensure_collections_and_view

    ensure_collections_and_view()
    db = get_db()

    status_filter = ""
    bind_vars: Dict[str, Any] = {"q": q, "limit": limit}
    if status != "all":
        status_filter = "FILTER doc.status == @status"
        bind_vars["status"] = status

    query = f"""
    FOR doc IN todos_search
        SEARCH ANALYZER(
            doc.title IN TOKENS(@q, 'text_en') OR
            doc.description IN TOKENS(@q, 'text_en'),
            'text_en'
        )
        {status_filter}
        SORT BM25(doc) DESC
        LIMIT @limit
        RETURN doc
    """

    try:
        items = list(db.aql.execute(query, bind_vars=bind_vars))
    except Exception as exc:
        logger.warning("todos_search view unavailable, falling back: {}", exc)
        # Fallback: LIKE with case-insensitive flag (3rd arg = true).
        # Still a collection scan (no stemming, no BM25 ranking) but
        # avoids the CONTAINS(LOWER()) anti-pattern.
        # TODO: ensure todos_search view is created so this path is not hit.
        q_pattern = f"%{q}%"
        fallback_query = """
        FOR doc IN todos
            FILTER LIKE(doc.title, @q_pattern, true)
                OR LIKE(doc.description, @q_pattern, true)
            LIMIT @limit
            RETURN doc
        """
        items = list(db.aql.execute(
            fallback_query,
            bind_vars={"q_pattern": q_pattern, "limit": limit},
        ))

    _json_output({
        "items": items,
        "count": len(items),
        "query": q,
    })
