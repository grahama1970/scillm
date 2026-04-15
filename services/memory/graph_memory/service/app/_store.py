"""Store endpoint — single write endpoint for all collections.

/store is the ONE way to write to memory. No other write endpoints.

Modes:
  1. Document mode: {document: dict, collection: str}
     → Direct upsert to any user collection. Auto-generates _key if omitted.
  2. Lesson mode: {document: {problem: str, solution: str, ...}}
     → When collection is omitted or "lessons", writes via store_lesson()
       which adds embeddings, dedup, and taxonomy validation.
  3. Legacy fact mode: {fact: str}
     → Shorthand for lesson with problem=fact, solution=fact.

/learn is deprecated. It redirects here with collection="lessons".
"""
from __future__ import annotations

import asyncio
import time
from functools import partial
from typing import Optional

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from ...arango_client import get_db
from ...lessons.store import store_lesson
from ._misuse_guard import validate_collection_name, validate_store_request

router = APIRouter()

# System collections that are never writable
_BLOCKED_COLLECTIONS = frozenset({
    "_graphs", "_analyzers", "_jobs", "_queues", "_statistics",
})


class StoreRequest(BaseModel):
    """Universal store request. Two primary modes:

    Document mode (preferred):
      {document: {...}, collection: "sparta_qra"}

    Legacy fact mode (convenience):
      {fact: "something learned", scope: "operational", tags: ["x"]}
      → Equivalent to {document: {problem: fact, solution: fact}, collection: "lessons"}
    """
    document: Optional[dict] = None
    collection: Optional[str] = None
    # Legacy fields for backward compatibility
    fact: Optional[str] = Field(None, min_length=1)
    scope: str = "operational"
    tags: list[str] = []


def _store_lesson_sync(document: dict) -> dict:
    """Write to lessons_v2 via store_lesson (adds embeddings, dedup, taxonomy)."""
    db = get_db()
    return store_lesson(db, document)


def _store_document_sync(document: dict, collection: str) -> dict:
    """Direct document insert/upsert into a named collection."""
    db = get_db()
    if not db.has_collection(collection):
        db.create_collection(collection)
    coll = db.collection(collection)
    now = int(time.time())
    document.setdefault("created_at", now)
    document["updated_at"] = now
    if "_key" in document:
        if coll.has(document["_key"]):
            coll.update(document)
        else:
            coll.insert(document)
    else:
        result = coll.insert(document)
        document["_key"] = result["_key"]
    return document


@router.post("/store")
async def store(body: StoreRequest) -> dict:
    """Single write endpoint for all collections.

    - collection omitted or "lessons" → store_lesson() with embeddings + dedup
    - collection specified → direct upsert to that collection
    - fact field → legacy shorthand, converted to document mode
    """
    # Validate collection name early — catches typos like "sparta_qras" → "sparta_qra"
    if body.collection:
        validate_collection_name(body.collection, "/store")

    # Legacy fact mode → convert to document
    if body.fact and body.document is None:
        body.document = {
            "problem": body.fact,
            "solution": body.fact,
            "scope": body.scope,
        }
        if body.tags:
            body.document["tags"] = body.tags
        body.collection = body.collection or "lessons"

    if body.document is None:
        raise HTTPException(
            status_code=422,
            detail="'document' is required. Example: {\"document\": {...}, \"collection\": \"lessons\"}",
        )

    # Validate store request — checks for missing _key, lessons without tags
    validate_store_request({"document": body.document, "collection": body.collection})

    collection = body.collection or "lessons"

    # Block system collections
    if collection.startswith("_") or collection in _BLOCKED_COLLECTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"System collection not writable: {collection}",
        )

    loop = asyncio.get_event_loop()

    # Lessons collection gets special treatment (embeddings, dedup, taxonomy)
    if collection in ("lessons", "lessons_v2"):
        doc = body.document
        doc.setdefault("scope", body.scope)
        if body.tags and "tags" not in doc:
            doc["tags"] = body.tags
        if not doc.get("problem") and not doc.get("solution"):
            raise HTTPException(
                status_code=422,
                detail="Lessons require 'problem' and/or 'solution' fields",
            )
        try:
            result = await loop.run_in_executor(None, partial(_store_lesson_sync, doc))
            logger.info("Stored lesson scope={} key={}", doc.get("scope"), result.get("_key", "?"))
            return {"stored": True, "collection": "lessons", "_key": result.get("_key", "")}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception as exc:
            logger.error("Failed to store lesson: {}", exc)
            raise HTTPException(status_code=500, detail=f"Storage failed: {exc}")

    # All other collections: direct upsert
    try:
        result = await loop.run_in_executor(
            None,
            partial(_store_document_sync, body.document, collection),
        )
        logger.info("Stored doc collection={} key={}", collection, result.get("_key", "?"))
        return {"stored": True, "collection": collection, "_key": result.get("_key", "")}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.error("Failed to store document: {}", exc)
        raise HTTPException(status_code=500, detail=f"Storage failed: {exc}")
