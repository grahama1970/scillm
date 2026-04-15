"""Core CLI commands: status, info, recall, trace, consolidate, residue, learn, ingest, serve."""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional

import typer

from ._helpers import app, _SERVICE_URL, _SERVICE_TIMEOUT, _service_post, _json_output
from ..http_clients import get_session as _get_session


@app.command()
def status() -> None:
    """Check memory health and connection."""
    if _SERVICE_URL:
        try:
            response = _get_session().get(f"{_SERVICE_URL.rstrip('/')}/health", timeout=_SERVICE_TIMEOUT)
            response.raise_for_status()
            _json_output(response.json())
            return
        except Exception as exc:
            typer.echo(f"[memory-agent] Health check via service failed: {exc}", err=True)
            raise typer.Exit(1)
    from ..lessons.status import app as status_app
    from typer.testing import CliRunner
    runner = CliRunner()
    res = runner.invoke(status_app, ["--json"], catch_exceptions=False)
    print(res.stdout or "")


@app.command("info")
def info_cmd(
    health: bool = typer.Option(
        True,
        "--health/--no-health",
        help="Check /health when MEMORY_SERVICE_URL is set.",
    )
) -> None:
    """Print the active Memory First configuration (for Pi agents + humans)."""
    summary: Dict[str, Any] = {
        "memory_first_contract": "recall → (optional) scan → learn",
        "service_url": _SERVICE_URL or None,
        "memory_agent": "graph-memory",
    }

    # Service health (if applicable)
    service_meta: Dict[str, Any] = {"mode": "cli" if not _SERVICE_URL else "service"}
    if _SERVICE_URL:
        service_meta["url"] = _SERVICE_URL
        service_meta["timeout_sec"] = _SERVICE_TIMEOUT
        if health:
            try:
                response = _get_session().get(f"{_SERVICE_URL.rstrip('/')}/health", timeout=_SERVICE_TIMEOUT)
                response.raise_for_status()
                service_meta["health"] = response.json()
            except Exception as exc:
                service_meta["health_error"] = str(exc)
    summary["service"] = service_meta

    # Embedding / dense retrieval config
    summary["embedding"] = {
        "model": os.getenv("EMBEDDING_MODEL") or os.getenv("GM_MODEL_ID") or "all-MiniLM-L6-v2",
        "device": os.getenv("EMBEDDING_DEVICE") or os.getenv("GM_DEVICE") or "auto",
        "force_cpu": str(os.getenv("GM_FORCE_CPU", "")).lower() in {"1", "true", "yes"},
    }
    summary["vector_engine"] = {
        "engine": (os.getenv("VECTOR_ENGINE") or "faiss").lower(),
        "cuvs_url": os.getenv("VECTOR_URL"),
        "use_gpu": str(os.getenv("GM_USE_GPU", "0")).lower() in {"1", "true", "yes"},
        "cuda_device": os.getenv("GM_CUDA_DEVICE") or os.getenv("CUDA_DEVICE"),
    }

    # Episodic recall configuration
    episodic_enabled = os.getenv("RECALL_INCLUDE_AGENT_CONVERSATIONS", "1").lower() not in {"0", "false", "no"}
    summary["episodic"] = {
        "agent_conversations_enabled": episodic_enabled,
        "episode_limit": int(os.getenv("RECALL_EPISODE_LIMIT", "6") or "6"),
        "edge_limit": int(os.getenv("RECALL_EPISODE_EDGE_LIMIT", "5") or "5"),
    }

    # Edge verifier / LLM config
    summary["edge_verifier"] = {
        "enabled": bool(os.getenv("SCILLM_PROXY_KEY", "")),
        "model": "text",  # scillm handles routing
        "api_base": os.getenv("SCILLM_API_BASE", "http://localhost:4001"),
        "max_llm": int(os.getenv("EDGE_VERIFIER_MAX_LLM", "0") or "0"),
    }

    # Supplemental recall sources (episodes, custom collections, etc.)
    try:
        from ..arango_client import get_db
        from ..lessons.recall_sources import load_supplemental_sources

        db = get_db()
        supplemental = []
        for src in load_supplemental_sources(db):
            data = asdict(src)
            supplemental.append(
                {
                    "name": data.get("name"),
                    "kind": data.get("kind"),
                    "view": data.get("view"),
                    "limit": data.get("limit"),
                    "include_edges": data.get("include_edges"),
                }
            )
        summary["supplemental_sources"] = supplemental
    except Exception as exc:
        summary["supplemental_sources_error"] = str(exc)

    summary["config_files"] = {
        "recall_sources_json": os.getenv("RECALL_SOURCES_JSON"),
        "recall_sources_file": os.getenv("RECALL_SOURCES_FILE"),
    }

    _json_output(summary)


# =============================================================================
# CORE COMMANDS - All an agent needs
# =============================================================================


def _brief_recall(result: Dict[str, Any]) -> Dict[str, Any]:
    """Pare recall results to context-safe essentials.

    Strips taxonomy, raw scores, _source, _key, etc. down to:
      problem (≤200 chars), solution (≤500 chars), playbook (≤300 chars), score, tags
    Plus: best matching skill_chain from the skill_chains collection.
    Typically ~5x smaller than full output.
    """
    query = result.get("meta", {}).get("q", "")

    slim_items = []
    for item in result.get("items", []):
        scores = item.get("scores", {})
        best_score = round(max(scores.values()), 2) if scores else 0.0
        slim: Dict[str, Any] = {
            "problem": (item.get("problem") or "")[:200],
            "solution": (item.get("solution") or "")[:500],
            "score": best_score,
        }
        # Include playbook only if it adds info beyond the solution
        playbook = (item.get("playbook") or "").strip()
        solution = slim["solution"]
        if playbook and playbook != solution and not solution.startswith(playbook):
            slim["playbook"] = playbook[:300]
        # Include tags if present (cheap, useful for routing)
        tags = item.get("tags")
        if tags:
            slim["tags"] = tags
        slim_items.append(slim)

    out: Dict[str, Any] = {
        "found": result.get("found", False),
        "should_scan": result.get("should_scan", True),
        "confidence": result.get("confidence", 0),
        "items": slim_items,
    }

    # Enrich with best matching skill chain
    # Priority: 1) edge from checkpoint → chain, 2) semantic match
    if out["found"]:
        chain = None
        # Try edge-based lookup first (deterministic)
        for item in slim_items:
            if item.get("tags") and "checkpoint" in item["tags"]:
                _key = None
                for orig in result.get("items", []):
                    if (orig.get("problem") or "")[:200] == item.get("problem"):
                        _key = orig.get("_key")
                        break
                if _key:
                    chain = _chain_from_edge(_key)
                    if chain:
                        break
        # Fall back to semantic match
        if not chain and query:
            chain = _best_skill_chain(query)
        if chain:
            out["skill_chain"] = chain

    return out


def _chain_from_edge(checkpoint_key: str) -> Optional[Dict[str, Any]]:
    """Follow skill_chain_edges to get the exact chain linked to a checkpoint."""
    try:
        from ..arango_client import get_db
        db = get_db()
        if not db.has_collection("skill_chain_edges"):
            return None
        cursor = db.aql.execute("""
            FOR e IN skill_chain_edges
                FILTER e._from == CONCAT("checkpoints/", @key)
                LIMIT 1
                LET chain = DOCUMENT(e._to)
                FILTER chain != null
                RETURN KEEP(chain, "skills", "task_type", "success_rate",
                            "observation_count", "elegance_grade", "source")
        """, bind_vars={"key": checkpoint_key})
        results = list(cursor)
        if not results:
            return None
        top = results[0]
        return {
            "skills": top.get("skills", []),
            "task_type": top.get("task_type", ""),
            "success_rate": top.get("success_rate", 0),
            "observations": top.get("observation_count", 0),
            "elegance": top.get("elegance_grade", ""),
            "source": "edge",
        }
    except Exception:
        return None


def _best_skill_chain(query: str) -> Optional[Dict[str, Any]]:
    """Find the best matching proven skill chain for a query.

    Prioritizes production chains (from /checkpoint) over transcript-mined
    chains, and filters out noisy kitchen-sink chains (>8 skills).

    Returns a slim dict with just the chain, task_type, success_rate,
    and elegance_grade — or None if nothing matches.
    """
    MAX_CHAIN_LENGTH = 8  # Chains longer than this are transcript noise

    try:
        from ..lessons.skill_chains import recall_chains
        # Fetch more candidates so we can filter
        results = recall_chains(query, limit=10, min_score=0.4)
        if not results:
            return None

        # Filter out kitchen-sink chains
        results = [r for r in results if len(r.get("skills", [])) <= MAX_CHAIN_LENGTH]
        if not results:
            return None

        # Prefer production chains (from /checkpoint — ground truth)
        production = [r for r in results if r.get("source") == "production"]
        top = production[0] if production else results[0]

        return {
            "skills": top.get("skills", []),
            "task_type": top.get("task_type", ""),
            "success_rate": top.get("success_rate", 0),
            "observations": top.get("observation_count", 0),
            "elegance": top.get("elegance_grade", ""),
            "score": top.get("score", 0),
        }
    except Exception:
        return None


@app.command("recall")
def recall_cmd(
    q: str = typer.Option(..., "--q", "-q", help="Problem/error/task to search for"),
    scope: str = typer.Option("", help="Project scope (auto-detected if empty)"),
    collections: Optional[str] = typer.Option(
        None, "--collections", "-c",
        help="Filter by source collections (comma-separated). Examples: lessons, horus_lore, agent_conversations, sanity_scripts"
    ),
    tags: Optional[str] = typer.Option(None, "--tags", "-t", help="Filter by tags (comma-separated)"),
    k: int = typer.Option(5, help="Number of results"),
    threshold: float = typer.Option(0.3, help="Minimum confidence (0-1)"),
    sort: Optional[str] = typer.Option(None, "--sort", help="Sort results by field (e.g. 'updated_at'). Default: relevance."),
    prefix: Optional[str] = typer.Option(None, "--prefix", help="Filter items where problem starts with this prefix"),
    brief: bool = typer.Option(False, "--brief", "-b", help="Slim output: just problem/solution/playbook/score. ~5x smaller, safe for agent context windows."),
) -> None:
    """CALL THIS FIRST. Query memory BEFORE scanning any codebase.

    Uses semantic search + BM25 + multi-hop graph traversal.

    Returns:
        found: true if relevant prior knowledge exists
        should_scan: true only if NO matches (proceed with codebase scan)
        confidence: relevance score (0-1)
        items: matching lessons with problem/solution

    Example:
        memory-agent recall --q "ImportError torch"
        memory-agent recall --q "siege tactics" --collections horus_lore
        memory-agent recall --q "pdf extraction" --collections lessons,sanity_scripts
        memory-agent recall --q "CHECKPOINT:" --tags checkpoint --sort updated_at --k 1
        memory-agent recall --q "how did we fix the timeout?" --brief

        If found=true:  Apply solution, DO NOT scan codebase
        If found=false: Scan codebase, solve, then use 'learn'
    """
    # Parse collections filter
    collections_filter = None
    if collections:
        collections_filter = [c.strip() for c in collections.split(",") if c.strip()]

    # Parse tags filter
    tag_filter = None
    if tags:
        tag_filter = [t.strip() for t in tags.split(",") if t.strip()]

    # Over-fetch when sorting/filtering client-side to ensure enough results.
    # Prefix filter can drop 90%+ of semantic results, so fetch aggressively.
    if prefix:
        fetch_k = max(k * 10, 50)
    elif sort:
        fetch_k = max(k * 3, 15)
    else:
        fetch_k = k

    payload = {"q": q, "scope": scope, "k": fetch_k, "threshold": threshold}
    if collections_filter:
        payload["collections"] = collections_filter
    if tag_filter:
        payload["tags"] = tag_filter

    result = _service_post("/recall", payload)

    # Post-retrieval: prefix filter
    if prefix and "items" in result:
        result["items"] = [
            i for i in result["items"]
            if i.get("problem", "").startswith(prefix)
        ]

    # Post-retrieval: temporal sort
    if sort and "items" in result:
        reverse = True  # Default DESC for temporal fields
        result["items"].sort(
            key=lambda x: x.get(sort, 0) or 0,
            reverse=reverse,
        )

    # Trim to requested k after filtering/sorting
    if "items" in result:
        result["items"] = result["items"][:k]
        result["found"] = len(result["items"]) > 0
        if "meta" in result:
            result["meta"]["sort"] = sort
            result["meta"]["prefix"] = prefix

    # --brief: pare down to question/answer/playbook that fits in context
    if brief and "items" in result:
        result = _brief_recall(result)

    _json_output(result)


@app.command("trace")
def trace_cmd(
    q: str = typer.Option(..., "--q", "-q", help="Query text"),
    answer: str = typer.Option("", "--answer", "-a", help="Answer text to verify claims against"),
    scope: str = typer.Option("", help="Scope filter"),
    mode: str = typer.Option("fast", help="Speed tier: instant|fast|accurate"),
    k: int = typer.Option(10, help="Max retrieval results"),
    depth: int = typer.Option(3, help="Graph traversal depth (accurate mode)"),
    json_out: bool = typer.Option(True, "--json/--no-json", help="Output as JSON"),
) -> None:
    """Trace provenance for a query and optional answer.

    Returns directed provenance graph showing which documents,
    controls, and edges contributed to (or should have contributed
    to) the answer.

    Three speed tiers:
        instant: cached lookup (~5ms)
        fast: BM25 + 1-hop graph, no LLM (~200ms)
        accurate: full recall + multi-hop BFS + claim verification (~3-5s)

    Examples:
        memory-agent trace --q "What countermeasures apply to SV-AC-2?" --scope sparta
        memory-agent trace --q "SV-AC-2" --answer "SV-AC-2 requires MFA..." --mode accurate
    """
    if mode not in ("instant", "fast", "accurate"):
        typer.echo(f"[memory-agent] Invalid mode: {mode}. Use instant|fast|accurate", err=True)
        raise typer.Exit(1)

    payload = {"q": q, "answer": answer, "scope": scope, "mode": mode, "k": k, "depth": depth}
    result = _service_post("/trace", payload)
    if json_out:
        _json_output(result)
    else:
        _print_trace_summary(result)


def _print_trace_summary(result: Dict[str, Any]) -> None:
    """Human-readable trace summary."""
    typer.echo(f"Trace {result.get('trace_id', '?')[:8]}  mode={result.get('mode')}  took={result.get('took_ms', 0)}ms  cached={result.get('cached', False)}")
    retrieval = result.get("retrieval", {})
    results = retrieval.get("results", [])
    typer.echo(f"Lanes: {', '.join(retrieval.get('lanes_used', []))}")
    typer.echo(f"Results: {len(results)}")
    for i, r in enumerate(results[:10], 1):
        scores = r.get("scores", {})
        bm25 = scores.get("bm25", 0)
        typer.echo(f"  {i}. [{r.get('label', '?')}] {r.get('title', r.get('key', '?'))[:60]}  bm25={bm25:.3f}  ctrl={r.get('control_id', '-')}")
    verification = result.get("verification")
    if verification:
        typer.echo(f"Claims: {verification.get('claims_verified', 0)}/{verification.get('claims_total', 0)} verified")
    evidence = result.get("evidence", {})
    if evidence:
        typer.echo(f"Evidence richness={evidence.get('richness_score', 0):.3f}  frameworks={evidence.get('framework_coverage', {})}")


@app.command("consolidate")
def consolidate_cmd(
    scope: str = typer.Option("", "--scope", "-s", help="Scope filter"),
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Preview only (default) or apply changes"),
    residue_limit: int = typer.Option(20, "--residue-limit", help="Day residue items"),
    quality_hours: int = typer.Option(48, "--quality-hours", help="Hours window for quality check"),
    json_out: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Run sleep-time memory consolidation cycle.

    Orchestrates: dedup, audit, quality check, day residue, retention.
    Safe by default (--dry-run). Use --apply with LTM_RETENTION_ENABLE=1.

    Example:
        memory-agent consolidate --dry-run --json
        memory-agent consolidate --apply --scope memory
    """
    from ..maintenance.sleep_consolidation import run_consolidation_cycle

    report = run_consolidation_cycle(
        scope=scope,
        dry_run=dry_run,
        residue_limit=residue_limit,
        quality_hours=quality_hours,
    )

    if json_out:
        _json_output(asdict(report))
    else:
        print(report.summary())


@app.command("residue")
def residue_cmd(
    limit: int = typer.Option(10, "--limit", "-l", help="Number of items to return"),
) -> None:
    """Fetch weighted temporal memories (Day Residue, Dream Lag, Semantic Resonance)."""
    payload = {"limit": limit}
    result = _service_post("/residue", payload)
    _json_output(result)


@app.command("learn")
def learn_cmd(
    problem: str = typer.Option(..., "--problem", "-p", help="The problem encountered"),
    solution: str = typer.Option(..., "--solution", "-s", help="How it was solved"),
    scope: str = typer.Option("", help="Project scope"),
    tags: Optional[List[str]] = typer.Option(None, "--tag", "-t", help="Tags"),
    verify: bool = typer.Option(False, "--verify/--no-verify",
                                help="Verify storage by immediate recall"),
) -> None:
    """Capture a lesson AFTER solving a problem.

    Completes the Memory First loop:
    1. recall returned found=false
    2. You scanned codebase and solved the problem
    3. NOW call learn to capture for future agents

    Example:
        memory-agent learn \\
          --problem "ImportError when running outside venv" \\
          --solution "Activate venv: source .venv/bin/activate"
    """
    payload = {"problem": problem, "solution": solution, "scope": scope}
    if tags is not None:
        payload["tags"] = tags
    result = _service_post("/learn", payload)

    if verify and result.get("items"):
        recall_payload = {"q": problem[:100], "scope": scope, "k": 1}
        recall_result = _service_post("/recall", recall_payload)
        if recall_result.get("items"):
            top_item = recall_result["items"][0]
            if top_item.get("_key") == result["items"][0].get("_key"):
                result["verified"] = True
                result["recall_score"] = top_item.get("scores", {}).get("dense", 0)
            else:
                result["verified"] = False
                result["warning"] = "Stored item not in top recall result"
        else:
            result["verified"] = False
            result["warning"] = "Recall returned no results"

    _json_output(result)


@app.command("ingest")
def ingest_cmd(
    path: str = typer.Argument(".", help="Project path to ingest"),
    scope: str = typer.Option("", help="Override scope (default: workspace:<dirname>)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan only, no writes"),
) -> None:
    """Set up memory for a project. Run ONCE per project.

    Detects project root, ingests docs (.md, .rst, .txt),
    and proposes relationships between lessons.

    Example:
        cd /path/to/myproject
        memory-agent ingest .

    After ingest, agents can use recall/learn in this project.
    """
    from ..workspace.build import app as build_app
    from typer.testing import CliRunner

    runner = CliRunner()
    args = ["--root", path]
    if scope:
        args += ["--scope", scope]
    if dry_run:
        args.append("--dry-run")

    res = runner.invoke(build_app, args, catch_exceptions=False)
    print(res.stdout or "")


@app.command("ingest-skills")
def ingest_skills_cmd(
    skills_root: str = typer.Argument(
        ..., help="Path to skills directory (e.g., .pi/skills/)",
    ),
    skill: Optional[str] = typer.Option(
        None, "--skill", "-s",
        help="Comma-separated skill names to ingest (default: all)",
    ),
) -> None:
    """Ingest SKILL.md frontmatter into ArangoDB for BM25/semantic/graph recall.

    Parses SKILL.md YAML frontmatter, upserts skill documents with embeddings,
    and creates COMPOSES + TAXONOMY edges for graph traversal.

    Example:
        memory-agent ingest-skills .pi/skills/
        memory-agent ingest-skills .pi/skills/ --skill taxonomy,normalize
    """
    from pathlib import Path
    from ..lessons.skill_registry import ingest_skills

    root = Path(skills_root)
    if not root.is_dir():
        typer.echo(f"[memory-agent] Not a directory: {skills_root}", err=True)
        raise typer.Exit(1)

    skill_filter = None
    if skill:
        skill_filter = [s.strip() for s in skill.split(",") if s.strip()]

    summary = ingest_skills(root, skill_filter=skill_filter)
    _json_output(summary)


@app.command("skills")
def skills_cmd(
    query: str = typer.Argument("", help="Free-text search (name, description, triggers)"),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Filter by skill name (substring)"),
    taxonomy: Optional[str] = typer.Option(None, "--taxonomy", "-t", help="Filter by taxonomy tag"),
    composes: Optional[str] = typer.Option(None, "--composes", help="Filter skills that compose this skill"),
    provides: Optional[str] = typer.Option(None, "--provides", help="Filter by provides capability"),
    graph: Optional[str] = typer.Option(None, "--graph", "-g", help="Show composition graph for this skill"),
    tags: bool = typer.Option(False, "--tags", help="List all taxonomy tags with counts"),
    k: int = typer.Option(20, help="Max results"),
    no_semantic: bool = typer.Option(False, "--no-semantic", help="Disable embedding-based ranking"),
) -> None:
    """Search the skill registry. BM25 + semantic + graph traversal over 220+ skills.

    Examples:
        memory-agent skills "pdf extraction"
        memory-agent skills --taxonomy Precision
        memory-agent skills --composes memory
        memory-agent skills --name monitor
        memory-agent skills --graph taxonomy
        memory-agent skills --tags
    """
    from ..lessons.skill_registry import search_skills, list_taxonomy_tags, get_skill_graph

    if tags:
        _json_output(list_taxonomy_tags())
        return

    if graph:
        _json_output(get_skill_graph(graph))
        return

    results = search_skills(
        query=query,
        name=name or "",
        taxonomy=taxonomy or "",
        composes=composes or "",
        provides=provides or "",
        k=k,
        semantic=not no_semantic,
    )
    _json_output({"found": len(results) > 0, "count": len(results), "skills": results})


@app.command("profile")
def profile_cmd(
    q: str = typer.Option(
        "What SPARTA countermeasures protect firmware from tampering?",
        "--q", "-q", help="Query to profile",
    ),
    full: bool = typer.Option(False, "--full", help="Also profile full api.search() path"),
    fmt: str = typer.Option("text", "--format", "-f", help="Output format: text or json"),
    budget: int = typer.Option(500, "--budget", help="Budget in ms (for SLOW/OK verdict)"),
) -> None:
    """Profile /memory search performance with per-step breakdown.

    Shows timing for each step: BM25, embedding, hybrid search, entity extraction, etc.

    Example:
        memory-agent profile --q "firmware tampering"
        memory-agent profile --q "cybersecurity" --full --format json
    """
    from ..profiling import profile_search, profile_full_recall

    p = profile_search(q)
    if fmt == "json":
        _json_output(p.as_dict())
    else:
        typer.echo(p.report(budget_ms=budget))

    if full:
        typer.echo()
        p2 = profile_full_recall(q)
        if fmt == "json":
            _json_output(p2.as_dict())
        else:
            typer.echo(p2.report(budget_ms=2000))


@app.command("serve")
def serve_cmd(
    host: str = typer.Option("0.0.0.0", "--host", help="Server host interface"),
    port: int = typer.Option(8601, "--port", help="Server port"),
    reload: bool = typer.Option(False, "--reload", help="Enable auto-reload (dev only)"),
) -> None:
    """Run the persistent FastAPI service that keeps embeddings warm."""
    try:
        import uvicorn  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency
        typer.echo(f"uvicorn is required for serve: {exc}", err=True)
        raise typer.Exit(1)
    uvicorn.run("graph_memory.service.app:app", host=host, port=port, reload=reload)
