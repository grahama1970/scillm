"""Content acquisition commands: acquire content, gaps, request, list."""
from __future__ import annotations
from loguru import logger

import json
from typing import Dict, List, Optional

import typer

from ._helpers import app, _json_output

acquire_app = typer.Typer(help="Acquire and learn from external content (URLs, files, movies).")
app.add_typer(acquire_app, name="acquire")


# Content source types for auto-routing
class SourceType:
    ARXIV = "arxiv"
    YOUTUBE = "youtube"
    GITHUB = "github"
    PDF = "pdf"
    AUDIOBOOK = "audiobook"
    MOVIE = "movie"
    URL = "url"
    FILE = "file"
    UNKNOWN = "unknown"


def _detect_source_type(source: str) -> str:
    """Auto-detect content source type from URL or path."""
    from urllib.parse import urlparse
    from pathlib import Path

    if source.startswith(("http://", "https://")):
        domain = urlparse(source).netloc.lower()
        if "arxiv.org" in domain:
            return SourceType.ARXIV
        if any(yt in domain for yt in ["youtube.com", "youtu.be"]):
            return SourceType.YOUTUBE
        if "github.com" in domain:
            return SourceType.GITHUB
        if source.lower().endswith(".pdf"):
            return SourceType.PDF
        return SourceType.URL

    source_lower = source.lower()
    if source_lower.endswith(".pdf"):
        return SourceType.PDF
    if source_lower.endswith((".aax", ".aaxc", ".m4b", ".m4a")):
        return SourceType.AUDIOBOOK
    if source_lower.endswith((".mkv", ".mp4", ".avi", ".mov")):
        return SourceType.MOVIE
    if Path(source).exists():
        return SourceType.FILE

    # Check if it looks like a movie title request
    movie_keywords = ["movie", "film", "watch", "nosferatu", "cinema"]
    if any(kw in source_lower for kw in movie_keywords):
        return SourceType.MOVIE

    return SourceType.UNKNOWN


def _find_skill(skill_name: str) -> "Optional[Path]":
    """Find a skill directory by name."""
    from pathlib import Path
    skills_dirs = [
        Path.home() / ".claude" / "skills",
        Path.home() / ".pi" / "skills",
        Path.home() / ".agent" / "skills",
        Path.home() / "workspace" / "experiments" / "pi-mono" / ".pi" / "skills",
        Path.home() / "workspace" / "experiments" / "pi-mono" / ".agent" / "skills",
    ]
    for skills_dir in skills_dirs:
        skill_path = skills_dir / skill_name
        if skill_path.exists() and (skill_path / "run.sh").exists():
            return skill_path
    return None


def _run_skill(skill_name: str, args: "List[str]") -> "tuple[bool, str, int]":
    """Run a skill and return (success, output, qa_count)."""
    import subprocess
    import re

    skill_dir = _find_skill(skill_name)
    if not skill_dir:
        return False, f"Skill not found: {skill_name}", 0

    typer.echo(f"[acquire] Running: {skill_name} {' '.join(args[:3])}...")

    try:
        result = subprocess.run(
            [str(skill_dir / "run.sh")] + args,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(skill_dir),
        )
        output = result.stdout + result.stderr
        qa_match = re.search(r"(\d+)\s*(?:Q&A|pairs|questions)", output, re.IGNORECASE)
        qa_count = int(qa_match.group(1)) if qa_match else 0
        return result.returncode == 0, output[:2000], qa_count
    except subprocess.TimeoutExpired:
        return False, "Timeout after 5 minutes", 0
    except Exception as e:
        return False, str(e), 0


@acquire_app.command("content")
def acquire_content(
    source: str = typer.Argument(..., help="URL, file path, or content title to acquire"),
    scope: str = typer.Option("operational", "--scope", "-s", help="Memory scope for storage"),
    context: str = typer.Option("general", "--context", "-c", help="Domain context for extraction"),
    force: bool = typer.Option(False, "--force", "-f", help="Re-acquire even if already learned"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Preview without acquiring"),
) -> None:
    """Acquire content from any source and store in memory.

    Auto-detects source type and routes to appropriate backend skill:
    - arxiv.org URLs → /arxiv skill
    - youtube.com URLs → /youtube-transcripts + /distill
    - PDF files → /extractor + /distill
    - Audiobooks (.aax, .m4b) → /audiobook-ingest + /distill
    - Movies (.mkv, .mp4) → /movie-ingest
    - Other URLs → /fetcher + /distill
    - Local files → /distill

    Example:
        memory-agent acquire content https://arxiv.org/abs/2302.02083 --scope horus_lore
        memory-agent acquire content https://youtube.com/watch?v=xyz --scope project_kb
        memory-agent acquire content ./document.pdf --scope research
        memory-agent acquire content "Nosferatu movie" --scope horus_lore
    """
    import hashlib
    from datetime import datetime, timezone
    from pathlib import Path

    source_type = _detect_source_type(source)

    if dry_run:
        typer.echo(f"[DRY RUN] Would acquire {source_type}: {source}")
        typer.echo(f"  Scope: {scope}")
        typer.echo(f"  Context: {context}")
        return

    typer.echo(f"[acquire] Detected type: {source_type}")

    # Check if already learned (simple hash check)
    learn_dir = Path.home() / ".learn" / scope
    learn_dir.mkdir(parents=True, exist_ok=True)
    learned_file = learn_dir / "learned.json"

    source_hash = hashlib.sha256(source.encode()).hexdigest()[:16]
    learned_data: Dict = {"items": [], "hashes": {}}
    if learned_file.exists():
        try:
            learned_data = json.loads(learned_file.read_text())
        except json.JSONDecodeError:
            pass

    if not force and source_hash in learned_data.get("hashes", {}):
        typer.echo(f"[acquire] Already learned: {source} (use --force to re-acquire)")
        return

    # Route to appropriate handler
    success, message, qa_count = False, "Unknown source type", 0

    if source_type == SourceType.ARXIV:
        import re
        match = re.search(r"(\d+\.\d+)", source)
        if match:
            paper_id = match.group(1)
            success, message, qa_count = _run_skill("arxiv", ["learn", paper_id, "--scope", scope, "--context", context, "--skip-interview"])
        else:
            message = "Could not extract arXiv ID"

    elif source_type == SourceType.YOUTUBE:
        success, transcript, _ = _run_skill("youtube-transcripts", [source])
        if success:
            temp = Path("/tmp/acquire_yt.txt")
            temp.write_text(transcript)
            success, message, qa_count = _run_skill("distill", ["--file", str(temp), "--scope", scope, "--context", context])
        else:
            message = f"Failed to get transcript: {transcript}"

    elif source_type == SourceType.PDF:
        success, content, _ = _run_skill("extractor", [source])
        if success:
            temp = Path("/tmp/acquire_pdf.txt")
            temp.write_text(content)
            success, message, qa_count = _run_skill("distill", ["--file", str(temp), "--scope", scope, "--context", context])
        else:
            message = f"Failed to extract PDF: {content}"

    elif source_type == SourceType.AUDIOBOOK:
        success, output, _ = _run_skill("audiobook-ingest", ["ingest", source])
        if success:
            book_name = Path(source).stem
            transcript_path = Path.home() / "clawd" / "library" / "books" / book_name / "transcript.txt"
            if transcript_path.exists():
                success, message, qa_count = _run_skill("distill", ["--file", str(transcript_path), "--scope", scope, "--context", context])
            else:
                success, message = False, "Transcript not found after ingestion"
        else:
            message = f"Failed to ingest audiobook: {output}"

    elif source_type == SourceType.MOVIE:
        # Route to movie-ingest for movie acquisition
        typer.echo(f"[acquire] Movie request detected: {source}")

        # Extract movie title (remove keywords like "movie", "film")
        movie_title = source
        for kw in ["movie", "film", "watch", "get", "download", "acquire", "the original", "can you"]:
            movie_title = movie_title.lower().replace(kw, "").strip()
        movie_title = " ".join(movie_title.split()).title()  # Clean up whitespace and title case

        # Check Radarr connection
        typer.echo(f"[acquire] Checking Radarr for: {movie_title}")
        check_success, check_output, _ = _run_skill("movie-ingest", ["acquire", "check"])

        if check_success and "connected" in check_output.lower():
            # Radarr available - try to add via emotion preset
            typer.echo(f"[acquire] Radarr connected. For emotion-based acquisition, use:")
            typer.echo(f"  movie-ingest acquire radarr --emotion <emotion> --execute")
            typer.echo(f"[acquire] Or search NZBGeek directly:")
            typer.echo(f"  movie-ingest search \"{movie_title}\"")
            success, message = True, f"Movie request logged: {movie_title} (Radarr available)"
        else:
            # Radarr not available
            typer.echo(f"[acquire] Radarr not available. Manual acquisition needed.")
            typer.echo(f"[acquire] Once movie is in library, use:")
            typer.echo(f"  movie-ingest agent recommend <emotion> --library <path>")
            typer.echo(f"  movie-ingest agent quick --movie <path> --emotion <emo> --timestamp HH:MM:SS-HH:MM:SS")
            success, message = True, f"Movie request queued: {movie_title}"

        # Save as request for tracking
        requests_file = learn_dir / "requests.json"
        requests: List = []
        if requests_file.exists():
            try:
                requests = json.loads(requests_file.read_text())
            except json.JSONDecodeError:
                pass
        requests.append({
            "source": source,
            "movie_title": movie_title,
            "type": "movie",
            "context": context,
            "scope": scope,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        })
        requests_file.write_text(json.dumps(requests, indent=2))
        typer.echo(f"[acquire] Request tracked: {requests_file}")

    elif source_type == SourceType.URL:
        success, content, _ = _run_skill("fetcher", [source])
        if success:
            temp = Path("/tmp/acquire_url.txt")
            temp.write_text(content)
            success, message, qa_count = _run_skill("distill", ["--file", str(temp), "--scope", scope, "--context", context])
        else:
            message = f"Failed to fetch URL: {content}"

    elif source_type == SourceType.FILE:
        success, message, qa_count = _run_skill("distill", ["--file", source, "--scope", scope, "--context", context])

    else:
        typer.echo(f"[acquire] Unknown source type: {source}")
        raise typer.Exit(1)

    # Record result
    if success:
        learned_data["items"].append({
            "source": source,
            "source_type": source_type,
            "scope": scope,
            "context": context,
            "learned_at": datetime.now(timezone.utc).isoformat(),
            "qa_count": qa_count,
            "success": True,
        })
        learned_data["hashes"][source_hash] = len(learned_data["items"]) - 1
        learned_file.write_text(json.dumps(learned_data, indent=2))
        typer.echo(f"[acquire] OK: {message[:200]}")
    else:
        typer.echo(f"[acquire] FAILED: {message[:200]}", err=True)
        raise typer.Exit(1)


@acquire_app.command("gaps")
def acquire_gaps(
    scope: str = typer.Option("operational", "--scope", "-s", help="Memory scope to analyze"),
    max_gaps: int = typer.Option(20, "--max", "-m", help="Maximum gaps to show"),
) -> None:
    """Find knowledge gaps from past failures and unresolved sessions.

    Analyzes:
    - Skill execution failures in logs
    - Learning failures in history
    - Unresolved sessions from episodic memory
    - Recurring errors and questions

    Use this to guide what to learn next.

    Example:
        memory-agent acquire gaps --scope horus_lore
    """
    from pathlib import Path

    gaps: List[Dict] = []

    # 1. Check skill failures in logs
    log_paths = [
        Path.home() / "workspace" / "experiments" / "pi-mono" / "logs",
        Path.home() / ".claude" / "logs",
        Path("/tmp"),
    ]

    skill_failures: Dict[str, int] = {}
    for log_dir in log_paths:
        if not log_dir.exists():
            continue
        for log_file in log_dir.glob("*.log"):
            try:
                content = log_file.read_text()[-50000:]
                for line in content.split("\n"):
                    line_lower = line.lower()
                    if "fail" in line_lower or "error" in line_lower:
                        for skill in ["fixture-graph", "fixture-table", "code-review", "anvil",
                                      "extractor", "distill", "arxiv", "fetcher", "movie-ingest"]:
                            if skill in line_lower:
                                skill_failures[skill] = skill_failures.get(skill, 0) + 1
                                if skill_failures[skill] <= 3:
                                    gaps.append({
                                        "type": "skill_failure",
                                        "content": line[:200],
                                        "skill": skill,
                                        "reason": f"/{skill} failed - may need deeper understanding",
                                    })
            except Exception as exc:
                logger.error("Suppressed error in acquire: {}", exc)

    # 2. Check learned items for failures
    learn_dir = Path.home() / ".learn"
    for scope_dir in learn_dir.glob("*"):
        learned_file = scope_dir / "learned.json"
        if learned_file.exists():
            try:
                data = json.loads(learned_file.read_text())
                for item in data.get("items", []):
                    if not item.get("success"):
                        gaps.append({
                            "type": "learn_failure",
                            "content": item.get("source", "")[:100],
                            "reason": item.get("error", "Learning failed")[:100],
                        })
            except Exception as exc:
                logger.error("Suppressed error in acquire: {}", exc)

    # 3. Try ArangoDB for episodic memory
    try:
        from ..arango_client import get_db
        db = get_db()

        # Find unresolved sessions
        if db.has_collection("unresolved_sessions"):
            query = """
            FOR doc IN unresolved_sessions
                FILTER doc.status == "pending"
                SORT doc.archived_at DESC
                LIMIT 10
                RETURN doc
            """
            for doc in db.aql.execute(query):
                resolution = doc.get("resolution", {})
                gaps.append({
                    "type": "unresolved_session",
                    "content": doc.get("summary", "")[:200],
                    "reason": resolution.get("reason", "Session not resolved"),
                    "priority": "high",
                })

        # Find errors from conversations
        if db.has_collection("agent_conversations"):
            query = """
            FOR doc IN agent_conversations
                FILTER doc.category IN ["error", "question"]
                SORT doc.timestamp DESC
                LIMIT 20
                RETURN {body: doc.body, category: doc.category}
            """
            for doc in db.aql.execute(query):
                gaps.append({
                    "type": doc.get("category", "unknown"),
                    "content": doc.get("body", "")[:200],
                    "reason": "From episodic memory",
                })
    except Exception as e:
        typer.echo(f"[gaps] Episodic memory unavailable: {e}", err=True)

    # Dedupe and show
    seen: set = set()
    unique_gaps: List[Dict] = []
    for gap in gaps:
        key = gap["content"][:50]
        if key not in seen:
            seen.add(key)
            unique_gaps.append(gap)

    if not unique_gaps:
        typer.echo("[gaps] No knowledge gaps found")
        return

    typer.echo(f"\nFound {len(unique_gaps)} knowledge gaps:\n")
    for i, gap in enumerate(unique_gaps[:max_gaps], 1):
        typer.echo(f"{i}. [{gap['type']}] {gap['content'][:60]}...")
        typer.echo(f"   Reason: {gap.get('reason', 'unknown')}")
        typer.echo()


@acquire_app.command("request")
def acquire_request(
    source: str = typer.Argument(..., help="Content to request (title, URL, etc.)"),
    scope: str = typer.Option("operational", "--scope", "-s", help="Memory scope"),
    context: str = typer.Option("general", "--context", "-c", help="Context for acquisition"),
) -> None:
    """Queue a content request for later acquisition.

    Use this when content isn't available yet (e.g., audiobooks to purchase,
    movies to download, papers behind paywalls).

    Example:
        memory-agent acquire request "Horus Rising audiobook" --scope horus_lore
        memory-agent acquire request "Nosferatu 1922 movie" --scope horus_lore
    """
    from datetime import datetime, timezone
    from pathlib import Path

    requests_file = Path.home() / ".learn" / scope / "requests.json"
    requests_file.parent.mkdir(parents=True, exist_ok=True)

    requests: List = []
    if requests_file.exists():
        try:
            requests = json.loads(requests_file.read_text())
        except json.JSONDecodeError:
            pass

    source_type = _detect_source_type(source)
    requests.append({
        "source": source,
        "type": source_type,
        "context": context,
        "scope": scope,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
    })

    requests_file.write_text(json.dumps(requests, indent=2))
    typer.echo(f"[request] Queued: {source}")
    typer.echo(f"[request] Type: {source_type}")
    typer.echo(f"[request] Saved to: {requests_file}")


@acquire_app.command("list")
def acquire_list(
    scope: str = typer.Option("operational", "--scope", "-s", help="Memory scope to list"),
    pending_only: bool = typer.Option(False, "--pending", "-p", help="Show only pending requests"),
) -> None:
    """List acquired content and pending requests.

    Example:
        memory-agent acquire list --scope horus_lore
        memory-agent acquire list --pending
    """
    from pathlib import Path

    learn_dir = Path.home() / ".learn" / scope

    # Show learned items
    learned_file = learn_dir / "learned.json"
    if learned_file.exists():
        data = json.loads(learned_file.read_text())
        items = data.get("items", [])
        if items and not pending_only:
            typer.echo(f"\n=== Acquired Content ({scope}) ===\n")
            for item in items[-20:]:  # Last 20
                status = "\u2713" if item.get("success") else "\u2717"
                typer.echo(f"  {status} [{item.get('source_type', '?')}] {item.get('source', '?')[:60]}")
                typer.echo(f"      {item.get('learned_at', '?')[:10]} | Q&A: {item.get('qa_count', 0)}")

    # Show pending requests
    requests_file = learn_dir / "requests.json"
    if requests_file.exists():
        requests = json.loads(requests_file.read_text())
        pending = [r for r in requests if r.get("status") == "pending"]
        if pending:
            typer.echo(f"\n=== Pending Requests ({scope}) ===\n")
            for req in pending:
                typer.echo(f"  \u23f3 [{req.get('type', '?')}] {req.get('source', '?')[:60]}")
                typer.echo(f"      Requested: {req.get('requested_at', '?')[:10]}")
