"""Tree-Sitter backed code metadata extraction.

The module keeps the implementation intentionally narrow: we support Python and
TypeScript/TSX symbols, persist snapshots in ArangoDB, and synthesise
code-specific lessons. Downstream CLIs can opt into the functionality by
setting environment flags; default flows remain untouched to honour the Happy
Path contract.
"""

from __future__ import annotations

import hashlib
import os
import time
from loguru import logger
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence

try:  # Optional dependency – callers guard via env flags.
    from tree_sitter import Parser, Query  # type: ignore
    from tree_sitter_languages import get_language  # type: ignore
except Exception as exc:  # pragma: no cover - degraded mode handled at runtime.
    logger.error("tree-sitter not available, running in degraded mode: {}", exc)
    Parser = None  # type: ignore
    Query = None  # type: ignore
    get_language = None  # type: ignore

SUPPORTED_EXTENSIONS = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
}

LANGUAGE_QUERIES = {
    "python": """
        (function_definition name: (identifier) @function.name)
        (class_definition name: (identifier) @class.name)
    """,
    "typescript": """
        (function_declaration name: (identifier) @function.name)
        (method_definition name: (property_identifier) @method.name)
        (class_declaration name: (type_identifier) @class.name)
    """,
}


@dataclass
class SymbolRecord:
    """Normalized representation of a parsed symbol."""

    name: str
    kind: str
    language: str
    file_path: str
    start_line: int
    end_line: int
    docstring: Optional[str] = None
    parameters: Optional[List[str]] = None

    def to_dict(self) -> Dict[str, object]:  # pragma: no cover - simple dataclass helper
        return asdict(self)


class TreeSitterUnavailable(RuntimeError):
    """Raised when the tree-sitter runtime is not importable."""


_PARSER_CACHE: Dict[str, Parser] = {}
_QUERY_CACHE: Dict[str, Query] = {}


def _ensure_language(language: str) -> Parser:
    if Parser is None or get_language is None:
        raise TreeSitterUnavailable(
            "tree-sitter runtime unavailable. Install tree_sitter and tree-sitter-languages."
        )
    if language not in _PARSER_CACHE:
        lang = get_language(language)
        parser = Parser()
        parser.set_language(lang)
        _PARSER_CACHE[language] = parser
    return _PARSER_CACHE[language]


def _get_query(language: str) -> Query:
    if Query is None or get_language is None:
        raise TreeSitterUnavailable("tree-sitter query runtime unavailable")
    if language not in _QUERY_CACHE:
        lang = get_language(language)
        query_src = LANGUAGE_QUERIES.get(language)
        if not query_src:
            raise ValueError(f"No tree-sitter query registered for {language}")
        # Prefer language-bound query() for compatibility; fallback to legacy Query constructor.
        try:
            _QUERY_CACHE[language] = lang.query(query_src)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.error("Language-bound query() failed, using legacy Query constructor: {}", exc)
            _QUERY_CACHE[language] = Query(lang, query_src)  # type: ignore[misc]
    return _QUERY_CACHE[language]


def _gather_parameters(node) -> List[str]:
    names: List[str] = []

    def _walk(n) -> None:
        if n.type in {"identifier", "property_identifier"}:
            try:
                names.append(n.text.decode("utf-8"))
            except Exception as exc:
                logger.error("Failed to decode parameter name: {}", exc)
        for child in n.children:
            _walk(child)

    for child in getattr(node, "children", []):
        if child.type in {"parameters", "formal_parameters", "parameter_list"}:
            _walk(child)
    deduped: List[str] = []
    for name in names:
        if name and name not in deduped:
            deduped.append(name)
    return deduped


def _iter_symbol_records(language: str, rel_path: Path, content: bytes) -> Iterator[SymbolRecord]:
    parser = _ensure_language(language)
    query = _get_query(language)
    tree = parser.parse(content)
    captures = query.captures(tree.root_node)
    for node, capture in captures:
        kind = capture.split(".")[0]
        try:
            name = node.text.decode("utf-8")
        except Exception as exc:
            logger.error("Failed to decode symbol name: {}", exc)
            continue
        start_line, _ = node.start_point
        end_line, _ = node.end_point
        params = _gather_parameters(node.parent) if node.parent else []
        yield SymbolRecord(
            name=name,
            kind=kind,
            language=language,
            file_path=str(rel_path.as_posix()),
            start_line=start_line + 1,
            end_line=end_line + 1,
            docstring=None,
            parameters=params,
        )


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _default_files(repo_path: Path) -> List[Path]:
    files: List[Path] = []
    for ext in SUPPORTED_EXTENSIONS:
        files.extend(repo_path.rglob(f"*{ext}"))
    return sorted(files)


def collect_symbols(
    scope: str,
    repo_path: Path,
    file_paths: Optional[Iterable[Path]] = None,
    persist: bool = True,
) -> List[SymbolRecord]:
    """Collect symbol metadata for a repository.

    Parameters
    ----------
    scope: str
        Target scope for newly created lessons/snapshots.
    repo_path: Path
        Location of the source tree to analyse.
    file_paths: Optional[Iterable[Path]]
        Explicit subset of files to inspect. Defaults to all supported files.
    persist: bool
        When ``True`` (default) the function stores snapshots and synthetic
        lessons in Arango.
    """

    repo_path = Path(repo_path)
    if not repo_path.exists():
        raise FileNotFoundError(f"Repository path not found: {repo_path}")

    selection: Sequence[Path]
    if file_paths:
        selection = [Path(p) if Path(p).is_absolute() else repo_path / Path(p) for p in file_paths]
    else:
        selection = _default_files(repo_path)

    records: List[SymbolRecord] = []
    batches: Dict[str, Dict[str, object]] = {}

    for path in selection:
        if not path.exists() or not path.is_file():
            continue
        ext = path.suffix.lower()
        language = SUPPORTED_EXTENSIONS.get(ext)
        if not language:
            continue
        content = path.read_bytes()
        rel_path = path.relative_to(repo_path)
        entries = list(_iter_symbol_records(language, rel_path, content))
        records.extend(entries)
        if not entries:
            continue
        batches[str(rel_path)] = {
            "scope": scope,
            "language": language,
            "file_path": str(rel_path.as_posix()),
            "file_hash": _hash_bytes(content),
            "symbols": [r.to_dict() for r in entries],
        }

    if persist and batches:
        _persist_batches(scope, repo_path, batches)

    return records


def _persist_batches(scope: str, repo_path: Path, batches: Dict[str, Dict[str, object]]) -> None:
    try:
        from ..arango_client import get_db
    except Exception as exc:
        logger.error("Cannot import arango_client in _persist_batches: {}", exc)
        return

    try:
        db = get_db()
        snapshots = db.collection("symbol_snapshots")
    except Exception as exc:
        logger.error("Failed to access symbol_snapshots collection: {}", exc)
        return

    ts = int(time.time())
    for rel_path, payload in batches.items():
        key = hashlib.sha1(f"{scope}|{rel_path}".encode("utf-8")).hexdigest()
        doc = {
            "_key": key,
            "scope": scope,
            "repo_root": str(repo_path),
            "file_path": payload["file_path"],
            "language": payload["language"],
            "file_hash": payload["file_hash"],
            "symbols": payload["symbols"],
            "updated_at": ts,
            "provenance": {"tree_sitter": {"version": os.getenv("GM_TREE_SITTER_VERSION", "0.1")}},
        }
        snapshots.insert(doc, overwrite=True)
        _upsert_symbol_lessons(scope, doc)


def _upsert_symbol_lessons(scope: str, snapshot: Dict[str, object]) -> None:
    try:
        from ..arango_client import get_db
    except Exception as exc:
        logger.error("Cannot import arango_client in _upsert_symbol_lessons: {}", exc)
        return

    db = get_db()
    lessons = db.collection("lessons")
    for symbol in snapshot.get("symbols", []):
        if not isinstance(symbol, dict):
            continue
        name = symbol.get("name")
        if not name:
            continue
        symbol_key = hashlib.sha1(
            f"{scope}|{snapshot['file_path']}|{symbol.get('start_line', 0)}|{name}".encode("utf-8")
        ).hexdigest()
        doc = {
            "_key": symbol_key,
            "title": f"{name} ({snapshot['file_path']})",
            "scope": scope,
            "problem": f"{symbol.get('kind', 'symbol')} defined in {snapshot['file_path']}",
            "playbook": "",
            "tags": ["code", snapshot.get("language")],
            "code_symbol": True,
            "language": snapshot.get("language"),
            "code_location": {
                "file_path": snapshot["file_path"],
                "start_line": symbol.get("start_line"),
                "end_line": symbol.get("end_line"),
            },
            "parameters": symbol.get("parameters") or [],
            "updated_at": snapshot.get("updated_at"),
        }
        from .store import store_lesson
        store_lesson(db, doc)
