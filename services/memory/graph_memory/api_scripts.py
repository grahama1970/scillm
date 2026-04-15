"""Peer module for sanity-script lifecycle functions.

Extracted from api.py to keep modules under 800 lines.
All public functions are re-exported by api.py so existing
``from graph_memory.api import X`` imports continue to work.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List

from loguru import logger

from .arango_client import get_db


# ---------------------------------------------------------------------------
# Sanity Scripts: Executable code examples for agent reference
# ---------------------------------------------------------------------------


def _extract_symbols_treesitter(script: str, language: str) -> Dict[str, Any]:
    """Extract symbols from script using treesitter.

    Returns dict with 'symbols' list and 'symbol_names' flattened list.
    Falls back gracefully if treesitter is not available.
    """
    import subprocess
    import tempfile
    import os as _os

    treesitter_skill = _os.path.expanduser("~/.pi/skills/treesitter/run.sh")
    if not _os.path.exists(treesitter_skill):
        # Try alternate location
        treesitter_skill = os.environ.get("TREESITTER_SKILL", str(Path(__file__).resolve().parent.parent.parent.parent / "pi-mono" / ".pi" / "skills" / "treesitter" / "run.sh"))

    if not _os.path.exists(treesitter_skill):
        return {"symbols": [], "symbol_names": [], "error": "treesitter skill not found"}

    # Map language to file extension
    ext_map = {
        "python": ".py", "py": ".py",
        "javascript": ".js", "js": ".js",
        "typescript": ".ts", "ts": ".ts",
        "rust": ".rs", "rs": ".rs",
        "go": ".go",
        "java": ".java",
        "c": ".c",
        "cpp": ".cpp", "c++": ".cpp",
        "ruby": ".rb", "rb": ".rb",
        "bash": ".sh", "sh": ".sh",
    }
    ext = ext_map.get(language.lower(), ".txt")

    try:
        # Write script to temp file to avoid command line length limits
        with tempfile.NamedTemporaryFile(mode="w", suffix=ext, delete=False) as f:
            f.write(script)
            temp_path = f.name

        try:
            # Use 'symbols' command with file path (more reliable than 'parse --code')
            result = subprocess.run(
                [treesitter_skill, "symbols", temp_path, "--json"],
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode != 0:
                return {"symbols": [], "symbol_names": [], "error": result.stderr[:500]}

            import json as _json
            # Handle both array and object outputs
            try:
                data = _json.loads(result.stdout)
            except _json.JSONDecodeError:
                return {"symbols": [], "symbol_names": [], "error": "invalid JSON from treesitter"}

            # treesitter-tools symbols returns a list of symbol objects
            if isinstance(data, list):
                symbols = data
            elif isinstance(data, dict):
                symbols = data.get("symbols", [])
            else:
                symbols = []

            symbol_names: List[str] = []
            for sym in symbols:
                if isinstance(sym, dict) and sym.get("name"):
                    symbol_names.append(sym["name"])
                    # Also extract parameter names if available
                    for param in (sym.get("parameters") or []):
                        if param and param not in symbol_names:
                            symbol_names.append(param)

            return {"symbols": symbols, "symbol_names": symbol_names}
        finally:
            _os.unlink(temp_path)
    except Exception as exc:
        return {"symbols": [], "symbol_names": [], "error": str(exc)}


def _generate_script_embedding(
    name: str, request: str, description: str, tags: List[str], symbol_names: List[str]
) -> List[float] | None:
    """Generate embedding for a sanity script."""
    try:
        from sentence_transformers import SentenceTransformer
        import os as _os

        model_id = _os.getenv('EMBEDDING_MODEL') or _os.getenv('GM_MODEL_ID') or 'all-MiniLM-L6-v2'
        model = SentenceTransformer(model_id)

        # Compose embedding text
        text_parts = [name, request or "", description or ""]
        if tags:
            text_parts.append(" ".join(tags))
        if symbol_names:
            text_parts.append(" ".join(symbol_names))

        embedding_text = "\n".join([p for p in text_parts if p])
        vector = model.encode([embedding_text], normalize_embeddings=True)[0]
        return vector.tolist()
    except Exception as exc:
        logger.error("script embedding generation failed: {}", exc)
        return None


def learn_script(
    name: str,
    request: str,
    script: str,
    language: str = "python",
    dependencies: List[str] | None = None,
    tags: List[str] | None = None,
    scope: str = "global",
    description: str = "",
    blessed_by: str = "",
    project_origin: str = "",
    expected_outputs: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Store a new sanity script (executable code example).

    Args:
        name: Human-readable name for the script
        request: The problem/request that led to this script
        script: The actual script code
        language: Programming language (python, bash, node, etc.)
        dependencies: Required packages/tools
        tags: Discovery tags
        scope: Scope for the script (default: global)
        description: Optional longer description
        blessed_by: Who verified this script works
        project_origin: Project where this was first created
        expected_outputs: Expected execution results {exit_code, stdout_pattern, stderr}

    Returns:
        Result with created script ID
    """
    from .arango_client import get_db
    from .setup_schema import ensure_collections_and_view
    import hashlib

    ensure_collections_and_view()
    db = get_db()
    ts = int(time.time())

    # Extract symbols using treesitter
    symbols_result = _extract_symbols_treesitter(script, language)
    symbols = symbols_result.get("symbols", [])
    symbol_names = symbols_result.get("symbol_names", [])

    # Generate embedding
    embedding = _generate_script_embedding(name, request, description, tags or [], symbol_names)

    # Create document
    doc = {
        "name": name,
        "request": request,
        "description": description or "",
        "script": script,
        "language": language,
        "dependencies": dependencies or [],
        "tags": tags or [],
        "scope": scope,
        "symbols": symbols,
        "symbol_names": symbol_names,
        "expected_inputs": [],
        "expected_outputs": expected_outputs or {"exit_code": 0, "stderr": ""},
        "last_run": None,
        "reasoning": {},
        "status": "active",
        "deprecated_by": None,
        "contradiction": None,
        "project_origin": project_origin,
        "blessed_by": blessed_by,
        "blessed_at": ts if blessed_by else None,
        "usage_count": 0,
        "used_at": None,
        "used_in_projects": [project_origin] if project_origin else [],
        "is_midterm": False,
        "created_at": ts,
        "updated_at": ts,
    }

    # Upsert to allow updates
    result = list(db.aql.execute(
        "UPSERT { name: @name, scope: @scope } INSERT @doc UPDATE @doc IN sanity_scripts RETURN NEW",
        bind_vars={"name": name, "scope": scope, "doc": doc}
    ))

    if not result:
        return {"meta": {"ok": False}, "items": [], "errors": ["insert failed"]}

    script_id = result[0]["_id"]
    script_key = result[0]["_key"]

    # Store embedding if generated
    if embedding:
        model_id = os.getenv('EMBEDDING_MODEL') or os.getenv('GM_MODEL_ID') or 'all-MiniLM-L6-v2'
        embed_key = hashlib.sha1((model_id + '|' + script_id).encode('utf-8')).hexdigest()
        embed_doc = {
            "_key": embed_key,
            "script_id": script_id,
            "model": model_id,
            "embedding": embedding,
            "created_at": ts,
        }
        try:
            db.aql.execute(
                "UPSERT { _key: @key } INSERT @doc UPDATE @doc IN script_embeddings",
                bind_vars={"key": embed_key, "doc": embed_doc}
            )
        except Exception as exc:
            logger.error("script embedding upsert failed: {}", exc)

    return {
        "meta": {"ok": True},
        "items": [{"_key": script_key, "_id": script_id, "name": name}],
        "errors": []
    }


def verify_script(script_key: str, args: List[str] | None = None, timeout_sec: int = 30) -> Dict[str, Any]:
    """Run a sanity script and verify it meets expectations.

    Args:
        script_key: The _key of the sanity script to verify
        args: Optional command line arguments to pass
        timeout_sec: Timeout in seconds (default: 30)

    Returns:
        Verification result with stdout, stderr, exit_code, and pass/fail status
    """
    from .arango_client import get_db
    import subprocess
    import tempfile
    import os as _os

    db = get_db()
    doc = db.collection("sanity_scripts").get(script_key)
    if not doc:
        return {"meta": {"ok": False}, "items": [], "errors": ["script not found"]}

    # Determine interpreter
    interpreters = {
        "python": ["python3"],
        "bash": ["bash"],
        "sh": ["sh"],
        "node": ["node"],
        "javascript": ["node"],
    }
    interpreter = interpreters.get(doc.get("language", "python"), ["python3"])

    # Write script to temp file
    ext_map = {"python": ".py", "bash": ".sh", "sh": ".sh", "node": ".js", "javascript": ".js"}
    ext = ext_map.get(doc.get("language", "python"), ".txt")

    t0 = time.time()
    script_path = ""
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=ext, delete=False) as f:
            f.write(doc["script"])
            script_path = f.name

        cmd = interpreter + [script_path] + (args or [])
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec
        )

        duration_ms = int((time.time() - t0) * 1000)

        # Check against expected outputs
        expected = doc.get("expected_outputs") or {}
        errors: List[str] = []
        passed = True

        expected_exit = expected.get("exit_code", 0)
        if result.returncode != expected_exit:
            passed = False
            errors.append(f"exit_code={result.returncode}, expected={expected_exit}")

        if expected.get("stderr", "") == "" and result.stderr.strip():
            passed = False
            errors.append(f"stderr not empty: {result.stderr[:200]}")

        stdout_pattern = expected.get("stdout_pattern")
        if stdout_pattern:
            import re
            if not re.search(stdout_pattern, result.stdout):
                passed = False
                errors.append(f"stdout doesn't match pattern: {stdout_pattern}")

        # Update last_run
        ts = int(time.time())
        last_run = {
            "stdout": result.stdout[:10000],
            "stderr": result.stderr[:10000],
            "exit_code": result.returncode,
            "duration_ms": duration_ms,
            "at": ts,
            "passed": passed,
            "environment": {
                "platform": _os.uname().sysname if hasattr(_os, "uname") else "unknown",
            }
        }

        db.collection("sanity_scripts").update({
            "_key": script_key,
            "last_run": last_run,
            "updated_at": ts,
        })

        return {
            "meta": {"ok": True, "passed": passed},
            "items": [{
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "duration_ms": duration_ms,
                "passed": passed,
                "errors": errors,
            }],
            "errors": []
        }

    except subprocess.TimeoutExpired:
        return {"meta": {"ok": False, "passed": False}, "items": [], "errors": [f"timeout after {timeout_sec}s"]}
    except Exception as exc:
        return {"meta": {"ok": False, "passed": False}, "items": [], "errors": [str(exc)]}
    finally:
        try:
            if script_path:
                import os as _os2
                _os2.unlink(script_path)
        except Exception as exc:
            logger.error("temp script cleanup failed: {}", exc)


def list_scripts(
    scope: str = "",
    tags: List[str] | None = None,
    status: str = "active",
    language: str = "",
    k: int = 20,
) -> Dict[str, Any]:
    """List sanity scripts matching criteria.

    Args:
        scope: Filter by scope (empty = all)
        tags: Filter by tags (any match)
        status: Filter by status (active, deprecated, contradicted)
        language: Filter by language
        k: Max results

    Returns:
        List of matching scripts
    """
    from .arango_client import get_db

    db = get_db()

    filters = ["doc.status == @status"]
    bind_vars: Dict[str, Any] = {"status": status, "k": k}

    if scope:
        filters.append("(doc.scope == @scope OR doc.scope == 'global')")
        bind_vars["scope"] = scope

    if language:
        filters.append("doc.language == @language")
        bind_vars["language"] = language

    if tags:
        filters.append("LENGTH(INTERSECTION(doc.tags, @tags)) > 0")
        bind_vars["tags"] = tags

    filter_clause = " AND ".join(filters) if filters else "true"

    aql = f"""
    FOR doc IN sanity_scripts
        FILTER {filter_clause}
        SORT doc.usage_count DESC, doc.updated_at DESC
        LIMIT @k
        RETURN KEEP(doc, '_key', '_id', 'name', 'request', 'language', 'tags', 'scope', 'status', 'usage_count', 'last_run')
    """

    results = list(db.aql.execute(aql, bind_vars=bind_vars))

    return {
        "meta": {"count": len(results), "scope": scope, "status": status},
        "items": results,
        "errors": []
    }


def search_scripts(q: str, scope: str = "", k: int = 5) -> Dict[str, Any]:
    """Search sanity scripts using BM25.

    Args:
        q: Search query
        scope: Filter by scope
        k: Max results

    Returns:
        Matching scripts with BM25 scores
    """
    from .arango_client import get_db

    db = get_db()

    aql = """
    FOR doc IN sanity_scripts_search
        SEARCH ANALYZER(
            doc.name IN TOKENS(@q, 'text_en') OR
            doc.request IN TOKENS(@q, 'text_en') OR
            doc.description IN TOKENS(@q, 'text_en') OR
            doc.symbol_names IN TOKENS(@q, 'text_en') OR
            doc.tags IN TOKENS(@q, 'text_en'),
            'text_en'
        )
        FILTER doc.status == 'active'
        FILTER @scope == '' OR doc.scope == @scope OR doc.scope == 'global'
        SORT BM25(doc) DESC
        LIMIT @k
        RETURN MERGE(doc, { score: BM25(doc) })
    """

    results = list(db.aql.execute(aql, bind_vars={"q": q, "scope": scope, "k": k}))

    # Mark source type
    for r in results:
        r["_source"] = "sanity_scripts"
        r["_type"] = "sanity_script"

    return {
        "meta": {"q": q, "scope": scope, "count": len(results)},
        "items": results,
        "errors": []
    }


def deprecate_script(script_key: str, replaced_by: str = "", reason: str = "") -> Dict[str, Any]:
    """Mark a sanity script as deprecated.

    Args:
        script_key: The _key of the script to deprecate
        replaced_by: _key of the replacement script (optional)
        reason: Reason for deprecation

    Returns:
        Result of the update
    """
    from .arango_client import get_db

    db = get_db()
    ts = int(time.time())

    doc = db.collection("sanity_scripts").get(script_key)
    if not doc:
        return {"meta": {"ok": False}, "items": [], "errors": ["script not found"]}

    update = {
        "_key": script_key,
        "status": "deprecated",
        "deprecated_by": replaced_by or None,
        "updated_at": ts,
    }

    db.collection("sanity_scripts").update(update)

    # Create supersedes edge if replaced_by provided
    if replaced_by:
        try:
            from_id = f"sanity_scripts/{replaced_by}"
            to_id = f"sanity_scripts/{script_key}"
            edge = {
                "_from": from_id,
                "_to": to_id,
                "type": "supersedes",
                "weight": 1.0,
                "rationale": reason or "Deprecated",
                "created_at": ts,
            }
            db.collection("lesson_edges").insert(edge)
        except Exception as exc:
            logger.error("deprecation edge insert failed: {}", exc)

    return {"meta": {"ok": True}, "items": [{"_key": script_key, "status": "deprecated"}], "errors": []}


def contradict_script(script_key: str, reason: str, discovered_in: str = "") -> Dict[str, Any]:
    """Mark a sanity script as contradicted (found to be wrong).

    Args:
        script_key: The _key of the script to contradict
        reason: Why the script was found to be wrong
        discovered_in: Project where the issue was discovered

    Returns:
        Result of the update
    """
    from .arango_client import get_db

    db = get_db()
    ts = int(time.time())

    doc = db.collection("sanity_scripts").get(script_key)
    if not doc:
        return {"meta": {"ok": False}, "items": [], "errors": ["script not found"]}

    contradiction = {
        "reason": reason,
        "discovered_at": ts,
        "in_project": discovered_in,
    }

    update = {
        "_key": script_key,
        "status": "contradicted",
        "contradiction": contradiction,
        "updated_at": ts,
    }

    db.collection("sanity_scripts").update(update)

    return {
        "meta": {"ok": True},
        "items": [{"_key": script_key, "status": "contradicted", "contradiction": contradiction}],
        "errors": []
    }


def record_script_usage(script_key: str, project: str = "") -> Dict[str, Any]:
    """Record usage of a sanity script.

    Args:
        script_key: The _key of the script that was used
        project: Project where it was used

    Returns:
        Updated usage stats
    """
    from .arango_client import get_db

    db = get_db()
    ts = int(time.time())

    doc = db.collection("sanity_scripts").get(script_key)
    if not doc:
        return {"meta": {"ok": False}, "items": [], "errors": ["script not found"]}

    usage_count = (doc.get("usage_count") or 0) + 1
    used_in_projects = list(set((doc.get("used_in_projects") or []) + ([project] if project else [])))
    is_midterm = usage_count >= 3

    update = {
        "_key": script_key,
        "usage_count": usage_count,
        "used_at": ts,
        "used_in_projects": used_in_projects,
        "is_midterm": is_midterm,
        "updated_at": ts,
    }

    db.collection("sanity_scripts").update(update)

    return {
        "meta": {"ok": True},
        "items": [{"_key": script_key, "usage_count": usage_count, "is_midterm": is_midterm}],
        "errors": []
    }


def trace_provenance(q: str, answer: str = "", scope: str = "", mode: str = "fast", k: int = 10, depth: int = 3, tags: List[str] | None = None) -> Dict[str, Any]:
    """Trace provenance for a query and optional answer.

    Returns directed provenance graph showing which documents,
    controls, and edges contributed to the answer.
    """
    from .trace import trace
    return trace(q=q, answer=answer, scope=scope, mode=mode, k=k, depth=depth, tags=tags)
