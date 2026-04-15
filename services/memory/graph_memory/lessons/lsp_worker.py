"""Language Server enrichment helpers.

We attempt to mirror Serena's LSP-powered enrichment, but keep dependencies
optional. When ``multilspy`` is available we will use it to request definition
and reference data. Otherwise the module falls back to Tree-Sitter derived
metadata so downstream flows retain predictable behaviour.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .symbols import SymbolRecord, TreeSitterUnavailable, collect_symbols
from loguru import logger

try:  # Optional real LSP client
    from multilspy import LspClient, initialize  # type: ignore
except Exception as exc:  # pragma: no cover - optional dependency
    logger.error("lsp_worker import failed: {exc}", exc=exc)
    LspClient = None  # type: ignore
    initialize = None  # type: ignore


@dataclass
class LspEnrichment:
    """Normalized payload returned from language server enrichment."""

    symbol: str
    uri: str
    definition: Optional[str]
    references: List[str]
    provider: str
    server_version: Optional[str] = None
    language: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:  # pragma: no cover - simple helper
        return asdict(self)


def enrich_symbols(
    scope: str,
    repo_root: str,
    languages: Optional[List[str]] = None,
    debounce_seconds: float = 0.0,
    persist: bool = True,
) -> List[LspEnrichment]:
    """Return enrichment data for symbols within ``repo_root``.

    Parameters mirror the Tree-Sitter collector. ``languages`` allows callers to
    focus on a subset; when ``multilspy`` is unavailable a Tree-Sitter fallback
    is used.
    """

    repo_path = Path(repo_root)
    if languages:
        language_filter = {lang.lower() for lang in languages}
    else:
        language_filter = None

    try:
        symbol_records = collect_symbols(
            scope=scope,
            repo_path=repo_path,
            file_paths=None,
            persist=False,
        )
    except TreeSitterUnavailable:
        symbol_records = []

    if language_filter:
        symbol_records = [rec for rec in symbol_records if rec.language.lower() in language_filter]

    enrichments: List[LspEnrichment] = []

    if symbol_records and LspClient and initialize:
        enrichments = _enrich_via_multilspy(symbol_records, repo_path)
    else:
        enrichments = _enrich_via_fallback(symbol_records, repo_path)

    if persist and enrichments:
        _persist(scope, repo_path, enrichments)

    return enrichments


def _enrich_via_multilspy(records: Sequence[SymbolRecord], repo_path: Path) -> List[LspEnrichment]:
    # Lazily initialize multilspy client per language to avoid spawning too many servers.
    enrichments: List[LspEnrichment] = []
    clients: Dict[str, LspClient] = {}

    for record in records:
        language = record.language.lower()
        if language not in clients:
            try:
                clients[language] = initialize(language, str(repo_path))
            except Exception as exc:
                logger.error("_enrich_via_multilspy init failed: {exc}", exc=exc)
                clients[language] = None  # type: ignore
        client = clients.get(language)
        if client is None:
            enrichments.extend(_enrich_via_fallback([record], repo_path))
            continue
        uri = f"file://{(repo_path / record.file_path).as_posix()}"
        try:
            definition = client.definition(uri, record.start_line, 0)
            references = client.references(uri, record.start_line, 0)
        except Exception as exc:
            logger.error("_enrich_via_multilspy LSP query failed: {exc}", exc=exc)
            definition = None
            references = []
        enrichments.append(
            LspEnrichment(
                symbol=record.name,
                uri=uri,
                definition=str(definition) if definition else None,
                references=[str(ref) for ref in references] if references else [],
                provider="multilspy",
                server_version=os.getenv("GM_LSP_SERVER_VERSION"),
                language=record.language,
            )
        )

    for client in clients.values():
        try:
            if client:
                client.shutdown()
        except Exception as exc:  # pragma: no cover - best effort cleanup
            logger.error("_enrich_via_multilspy shutdown failed: {exc}", exc=exc)

    return enrichments


def _enrich_via_fallback(records: Sequence[SymbolRecord], repo_path: Path) -> List[LspEnrichment]:
    enrichments: List[LspEnrichment] = []
    for record in records:
        uri = f"file://{(repo_path / record.file_path).as_posix()}"
        definition = f"{record.name} defined at {uri}:{record.start_line}"
        references = [definition]
        enrichments.append(
            LspEnrichment(
                symbol=record.name,
                uri=uri,
                definition=definition,
                references=references,
                provider="tree-sitter-fallback",
                server_version=None,
                language=record.language,
            )
        )
    return enrichments


def _persist(scope: str, repo_path: Path, enrichments: Sequence[LspEnrichment]) -> None:
    try:
        from ..arango_client import get_db
    except Exception as exc:
        logger.error("_persist import failed: {exc}", exc=exc)
        return

    try:
        db = get_db()
        col = db.collection("symbol_snapshots")
    except Exception as exc:
        logger.error("_persist collection access failed: {exc}", exc=exc)
        return

    ts = int(time.time())
    grouped: Dict[str, List[Dict[str, object]]] = {}
    for enr in enrichments:
        abs_path = Path(enr.uri.replace("file://", ""))
        try:
            rel_path = abs_path.relative_to(repo_path)
        except ValueError:
            rel_path = abs_path
        grouped.setdefault(str(rel_path), []).append(enr.to_dict())

    for rel_path, payload in grouped.items():
        key = hashlib.sha1(f"{scope}|{rel_path}".encode("utf-8")).hexdigest()
        doc = col.get(key)
        if not doc:
            doc = {
                "_key": key,
                "scope": scope,
                "repo_root": str(repo_path),
                "file_path": rel_path,
            }
        provenance = doc.get("provenance", {}) or {}
        provenance["lsp"] = {
            "provider": payload[0].get("provider"),
            "server_version": payload[0].get("server_version"),
            "updated_at": ts,
        }
        doc["provenance"] = provenance
        doc["lsp_enrichment"] = payload
        doc["updated_at"] = ts
        col.insert(doc, overwrite=True)
