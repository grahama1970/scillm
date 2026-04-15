"""Sanity script commands: learn-script, verify-script, list-scripts, search-scripts, deprecate-script, contradict-script."""
from __future__ import annotations

from typing import Optional

import typer

from ._helpers import app, _json_output


@app.command("learn-script")
def learn_script_cmd(
    name: str = typer.Option(..., "--name", "-n", help="Human-readable name for the script"),
    request: str = typer.Option(..., "--request", "-r", help="The problem/request that led to this script"),
    script_path: str = typer.Option(..., "--script", "-s", help="Path to the script file"),
    language: str = typer.Option("python", "--language", "-l", help="Programming language"),
    deps: Optional[str] = typer.Option(None, "--deps", "-d", help="Comma-separated dependencies"),
    tags: Optional[str] = typer.Option(None, "--tags", "-t", help="Comma-separated tags"),
    scope: str = typer.Option("global", "--scope", help="Scope for the script"),
    description: str = typer.Option("", "--description", help="Optional longer description"),
    blessed_by: str = typer.Option("", "--blessed-by", help="Who verified this script works"),
    project: str = typer.Option("", "--project", help="Project where this was first created"),
) -> None:
    """Store a new sanity script (executable code example).

    Sanity scripts are working code examples that agents can reference
    before writing project code. They must be verified to work (exit 0, no stderr).

    Example:
        memory-agent learn-script \\
          --name "Camelot Stream Extraction" \\
          --request "Extract tables from PDFs without borders" \\
          --script ./extract_tables.py \\
          --language python \\
          --deps "camelot-py[cv],ghostscript" \\
          --tags "pdf,tables,camelot" \\
          --blessed-by "graham"
    """
    from pathlib import Path

    from ..api import learn_script

    script_file = Path(script_path)
    if not script_file.exists():
        typer.echo(f"Script file not found: {script_path}", err=True)
        raise typer.Exit(1)

    script_content = script_file.read_text()
    deps_list = [d.strip() for d in (deps or "").split(",") if d.strip()]
    tags_list = [t.strip() for t in (tags or "").split(",") if t.strip()]

    result = learn_script(
        name=name,
        request=request,
        script=script_content,
        language=language,
        dependencies=deps_list or None,
        tags=tags_list or None,
        scope=scope,
        description=description,
        blessed_by=blessed_by,
        project_origin=project,
    )
    _json_output(result)


@app.command("verify-script")
def verify_script_cmd(
    key: str = typer.Option(..., "--key", "-k", help="The _key of the sanity script to verify"),
    args: Optional[str] = typer.Option(None, "--args", "-a", help="Space-separated arguments to pass"),
    timeout: int = typer.Option(30, "--timeout", help="Timeout in seconds"),
) -> None:
    """Run a sanity script and verify it meets expectations.

    Checks that the script:
    - Exits with code 0
    - Has empty stderr
    - Matches expected stdout pattern (if defined)

    Example:
        memory-agent verify-script --key "camelot_stream_extraction"
    """
    from ..api import verify_script

    args_list = args.split() if args else None
    result = verify_script(script_key=key, args=args_list, timeout_sec=timeout)
    _json_output(result)


@app.command("list-scripts")
def list_scripts_cmd(
    scope: str = typer.Option("", "--scope", help="Filter by scope"),
    tags: Optional[str] = typer.Option(None, "--tags", "-t", help="Comma-separated tags to filter by"),
    status: str = typer.Option("active", "--status", help="Filter by status (active, deprecated, contradicted)"),
    language: str = typer.Option("", "--language", "-l", help="Filter by language"),
    k: int = typer.Option(20, "--k", help="Max results"),
) -> None:
    """List sanity scripts matching criteria.

    Example:
        memory-agent list-scripts --tags "pdf,tables" --status active
    """
    from ..api import list_scripts

    tags_list = [t.strip() for t in (tags or "").split(",") if t.strip()] or None
    result = list_scripts(scope=scope, tags=tags_list, status=status, language=language, k=k)
    _json_output(result)


@app.command("search-scripts")
def search_scripts_cmd(
    q: str = typer.Option(..., "--q", "-q", help="Search query"),
    scope: str = typer.Option("", "--scope", help="Filter by scope"),
    k: int = typer.Option(5, "--k", help="Max results"),
) -> None:
    """Search sanity scripts using BM25.

    Example:
        memory-agent search-scripts --q "camelot table extraction"
    """
    from ..api import search_scripts

    result = search_scripts(q=q, scope=scope, k=k)
    _json_output(result)


@app.command("deprecate-script")
def deprecate_script_cmd(
    key: str = typer.Option(..., "--key", "-k", help="The _key of the script to deprecate"),
    replaced_by: str = typer.Option("", "--replaced-by", help="_key of the replacement script"),
    reason: str = typer.Option("", "--reason", help="Reason for deprecation"),
) -> None:
    """Mark a sanity script as deprecated.

    Example:
        memory-agent deprecate-script --key "camelot_v1" --replaced-by "camelot_v2"
    """
    from ..api import deprecate_script

    result = deprecate_script(script_key=key, replaced_by=replaced_by, reason=reason)
    _json_output(result)


@app.command("contradict-script")
def contradict_script_cmd(
    key: str = typer.Option(..., "--key", "-k", help="The _key of the script to contradict"),
    reason: str = typer.Option(..., "--reason", "-r", help="Why the script was found to be wrong"),
    discovered_in: str = typer.Option("", "--discovered-in", help="Project where the issue was discovered"),
) -> None:
    """Mark a sanity script as contradicted (found to be wrong).

    Example:
        memory-agent contradict-script \\
          --key "camelot_stream_extraction" \\
          --reason "Fails on multi-column PDFs" \\
          --discovered-in "research-papers"
    """
    from ..api import contradict_script

    result = contradict_script(script_key=key, reason=reason, discovered_in=discovered_in)
    _json_output(result)
