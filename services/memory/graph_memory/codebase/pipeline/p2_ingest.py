"""P2: ingest phase for curate pipeline.

Purpose: Discover files, extract code symbols, chunk documentation.

Sub-steps:
1. discover_files: Apply ignore rules, build scan set
2. ingest_code_symbols: Tree-sitter extraction
3. ingest_docs: Chunk markdown/rst/txt/tex/html

Assertions (from 02_SPEC.md):
- Ignore rules applied (.gitignore + .graph-memory-operator-ignore)
- code_files upserted (Count > 0 if code exists)
- code_symbols upserted (Count > 0 if parseable)
- doc_chunks created (Count > 0 if docs exist)
- Parse failures recorded (Non-fatal, logged)
"""

import json
from pathlib import Path
from typing import Any
from loguru import logger

from graph_memory.codebase.pipeline.types import (
    PhaseResult,
    PhaseStatus,
    RunContext,
)

# File extensions for code files
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".java",
    ".c", ".cpp", ".h", ".hpp", ".rb", ".php", ".swift", ".kt",
    ".scala", ".lua", ".hs", ".ml", ".lean"
}

# File extensions for doc files
DOC_EXTENSIONS = {".md", ".rst", ".txt", ".tex", ".html", ".htm"}

# Default ignore patterns
DEFAULT_IGNORES = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules",
    ".venv", "venv", ".tox", ".eggs", "*.egg-info",
    "dist", "build", ".cache", ".pytest_cache",
}


def load_ignore_patterns(code_path: Path) -> set[str]:
    """Load ignore patterns from .gitignore and .graph-memory-operator-ignore."""
    patterns = set(DEFAULT_IGNORES)

    # Load .gitignore
    gitignore = code_path / ".gitignore"
    if gitignore.exists():
        for line in gitignore.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.add(line.rstrip("/"))

    # Load .graph-memory-operator-ignore
    operator_ignore = code_path / ".graph-memory-operator-ignore"
    if operator_ignore.exists():
        for line in operator_ignore.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.add(line.rstrip("/"))

    return patterns


def should_ignore(path: Path, code_path: Path, ignore_patterns: set[str]) -> bool:
    """Check if a path should be ignored."""
    rel_path = path.relative_to(code_path)

    # Check each part of the path
    for part in rel_path.parts:
        if part in ignore_patterns:
            return True
        # Check wildcard patterns
        for pattern in ignore_patterns:
            if pattern.startswith("*") and part.endswith(pattern[1:]):
                return True

    return False


def discover_files(
    code_path: Path,
    ignore_patterns: set[str],
    max_files: int | None = None,
) -> tuple[list[Path], list[Path]]:
    """Discover code and doc files.

    Returns:
        (code_files, doc_files)
    """
    code_files = []
    doc_files = []
    total_files = 0

    for path in code_path.rglob("*"):
        if not path.is_file():
            continue

        if should_ignore(path, code_path, ignore_patterns):
            continue

        suffix = path.suffix.lower()

        if suffix in CODE_EXTENSIONS:
            code_files.append(path)
            total_files += 1
        elif suffix in DOC_EXTENSIONS:
            doc_files.append(path)
            total_files += 1

        if max_files and total_files >= max_files:
            break

    return code_files, doc_files


def extract_symbols(file_path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Extract symbols from a code file using treesitter-tools.

    Returns:
        (symbols, errors)
    """
    symbols = []
    errors = []

    # Map extensions to languages
    ext_to_lang = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".jsx": "jsx",
        ".rs": "rust",
        ".go": "go",
        ".java": "java",
        ".c": "c",
        ".cpp": "cpp",
        ".h": "c",
        ".hpp": "cpp",
        ".rb": "ruby",
        ".lean": "lean",
    }

    lang = ext_to_lang.get(file_path.suffix.lower())
    if not lang:
        return symbols, errors

    try:
        from treesitter_tools.api import list_symbols

        result = list_symbols(str(file_path), lang)
        if result:
            for sym in result:
                # CodeSymbol has to_dict() method or direct attributes
                if hasattr(sym, "to_dict"):
                    sym_dict = sym.to_dict()
                    symbols.append({
                        "name": sym_dict.get("name", ""),
                        "kind": sym_dict.get("kind", "unknown"),
                        "start_line": sym_dict.get("start_line", 0),
                        "end_line": sym_dict.get("end_line", 0),
                        "file_path": str(file_path),
                    })
                else:
                    symbols.append({
                        "name": getattr(sym, "name", ""),
                        "kind": getattr(sym, "kind", "unknown"),
                        "start_line": getattr(sym, "start_line", 0),
                        "end_line": getattr(sym, "end_line", 0),
                        "file_path": str(file_path),
                    })
    except ImportError:
        errors.append(f"treesitter-tools not available")
    except Exception as e:
        errors.append(f"Error extracting symbols from {file_path}: {e}")

    return symbols, errors


def chunk_document(file_path: Path, max_chunk_size: int = 2000) -> list[dict[str, Any]]:
    """Chunk a document file.

    Simple chunking by paragraphs/sections.
    """
    chunks = []

    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.error("Suppressed error in p2_ingest: {}", exc)
        return chunks

    # Split by double newlines (paragraphs)
    paragraphs = content.split("\n\n")

    current_chunk = []
    current_size = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        para_size = len(para)

        if current_size + para_size > max_chunk_size and current_chunk:
            # Save current chunk
            chunks.append({
                "content": "\n\n".join(current_chunk),
                "source_path": str(file_path),
                "chunk_index": len(chunks),
            })
            current_chunk = []
            current_size = 0

        current_chunk.append(para)
        current_size += para_size

    # Save remaining chunk
    if current_chunk:
        chunks.append({
            "content": "\n\n".join(current_chunk),
            "source_path": str(file_path),
            "chunk_index": len(chunks),
        })

    return chunks


def run_p2_ingest(context: RunContext) -> PhaseResult:
    """Execute P2: ingest phase.

    Args:
        context: RunContext from P1 init

    Returns:
        PhaseResult
    """
    errors: list[dict[str, Any]] = []
    warnings: list[str] = []
    parse_failures: list[str] = []

    # Use worktree path if available, otherwise code_path
    scan_path = context.worktree_path or context.code_path

    # Load ignore patterns
    ignore_patterns = load_ignore_patterns(scan_path)

    # Determine max files for debug mode
    max_files = None
    if context.config.debug:
        max_files = context.config.debug_max_files

    # Discover files
    code_files, doc_files = discover_files(scan_path, ignore_patterns, max_files)

    # Extract symbols from code files
    all_symbols = []
    if context.config.treesitter_enabled:
        for code_file in code_files:
            symbols, sym_errors = extract_symbols(code_file)
            all_symbols.extend(symbols)
            parse_failures.extend(sym_errors)
    else:
        warnings.append("Treesitter extraction disabled")

    # Chunk documents
    all_chunks = []
    for doc_file in doc_files:
        chunks = chunk_document(doc_file)
        all_chunks.extend(chunks)

    # Create P2 artifacts directory
    p2_artifacts = context.artifacts_path / "p2_ingest"
    p2_artifacts.mkdir(parents=True, exist_ok=True)

    # Write artifacts
    if context.config.debug_verbose_artifacts:
        # Write detailed artifacts in debug mode
        (p2_artifacts / "code_files.json").write_text(
            json.dumps([str(f) for f in code_files], indent=2)
        )
        (p2_artifacts / "code_symbols.json").write_text(
            json.dumps(all_symbols, indent=2)
        )
        (p2_artifacts / "doc_chunks.json").write_text(
            json.dumps(all_chunks, indent=2)
        )

    # Write summary
    summary = {
        "counts": {
            "code_files": len(code_files),
            "code_symbols": len(all_symbols),
            "doc_chunks": len(all_chunks),
            "doc_files": len(doc_files),
        },
        "ignore_rules_applied": True,
        "ignore_patterns_count": len(ignore_patterns),
        "parse_failures": parse_failures,
        "scan_path": str(scan_path),
    }

    summary_file = p2_artifacts / "ingest_summary.json"
    summary_file.write_text(json.dumps(summary, indent=2))

    # Determine status
    if len(code_files) == 0 and len(doc_files) == 0:
        status = PhaseStatus.SKIPPED
        warnings.append("No code or doc files found")
    elif parse_failures:
        status = PhaseStatus.PARTIAL
    else:
        status = PhaseStatus.OK

    return PhaseResult(
        status=status,
        counts={
            "code_files": len(code_files),
            "code_symbols": len(all_symbols),
            "doc_chunks": len(all_chunks),
        },
        errors=errors,
        warnings=warnings,
        artifacts={
            "ingest_summary": str(summary_file),
        },
    )
