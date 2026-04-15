"""DB-backed vocabulary caches for intent mapping.

All domain knowledge lives in ArangoDB, accessed via daemon endpoints.
NO bespoke AQL — use /list and /recall endpoints only.
"""

from __future__ import annotations

import os

import httpx
from loguru import logger


# ──────────────────────────────────────────────────────────────────────
#  DB-backed vocabulary caches (populated on first use via daemon)
# ──────────────────────────────────────────────────────────────────────

_tactical_keywords_cache: dict[str, list[str]] | None = None
_ood_terms_cache: list[str] | None = None
_sparta_keywords_cache: list[str] | None = None
_broad_tech_cache: list[str] | None = None


def _daemon_client() -> httpx.Client:
    """Get httpx client for daemon. Uses UDS on host, HTTP in Docker."""
    sock = "/run/user/1000/embry/memory.sock"
    if os.path.exists(sock):
        return httpx.Client(
            transport=httpx.HTTPTransport(uds=sock),
            base_url="http://localhost",
            timeout=5,
        )
    # Inside Docker — use HTTP to memory service
    base = os.getenv("MEMORY_SERVICE_URL", "http://localhost:8601")
    return httpx.Client(base_url=base, timeout=5)


def _list_terms(collection: str, category: str, fields: list[str]) -> list[dict]:
    """Fetch domain terms from a collection via /list endpoint."""
    try:
        client = _daemon_client()
        resp = client.post("/list", json={
            "collection": collection,
            "filters": {"category": category},
            "limit": 500,
        })
        resp.raise_for_status()
        data = resp.json()
        return data.get("documents", data.get("items", []))
    except Exception as exc:
        logger.error("Failed to load {}/{} via /list: {}", collection, category, exc)
        return []


def _get_tactical_keywords() -> dict[str, list[str]]:
    global _tactical_keywords_cache
    if _tactical_keywords_cache is not None:
        return _tactical_keywords_cache
    _tactical_keywords_cache = {}
    for row in _list_terms("taxonomy_vocabulary", "tactical_keyword", ["tactical_tag", "term"]):
        tag = row.get("tactical_tag", "")
        term = row.get("term", "")
        if tag and term:
            _tactical_keywords_cache.setdefault(tag, []).append(term.lower())
    return _tactical_keywords_cache


def _get_ood_terms() -> list[str]:
    global _ood_terms_cache
    if _ood_terms_cache is not None:
        return _ood_terms_cache
    _ood_terms_cache = [
        row.get("term", "").lower()
        for row in _list_terms("domain_terms", "out_of_domain", ["term"])
        if row.get("term")
    ]
    return _ood_terms_cache


def _get_sparta_keywords() -> list[str]:
    global _sparta_keywords_cache
    if _sparta_keywords_cache is not None:
        return _sparta_keywords_cache
    _sparta_keywords_cache = [
        row.get("term", "").lower()
        for row in _list_terms("domain_terms", "sparta_keyword", ["term"])
        if row.get("term")
    ]
    return _sparta_keywords_cache


def _get_broad_tech_terms() -> list[str]:
    global _broad_tech_cache
    if _broad_tech_cache is not None:
        return _broad_tech_cache
    _broad_tech_cache = [
        row.get("term", "").lower()
        for row in _list_terms("domain_terms", "broad_tech_term", ["term"])
        if row.get("term")
    ]
    return _broad_tech_cache


# Classifier thresholds
AMBIGUITY_CONFIDENCE_THRESHOLD = 0.7
INTENT_CONFIDENCE_THRESHOLD = 0.6
