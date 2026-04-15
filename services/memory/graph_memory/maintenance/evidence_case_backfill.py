"""Backfill evidence_case field for all QRAs.

Uses streaming cursor (reduces memory) + batch UPDATE (cluster optimized).
Calls extract_entities per QRA to build glossary + crosswalk_chains.

Usage:
    python -m graph_memory.maintenance.evidence_case_backfill --batch-size 500 --limit 1000
    python -m graph_memory.maintenance.evidence_case_backfill --dry-run
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime
from typing import Any

from loguru import logger

# Version tag for tracking processed documents
# v1: glossary, crosswalk_chains, resolved_entities
# v2: + spans (character positions with type/status/framework/category)
EVIDENCE_CASE_VERSION = "v2"


def ensure_index(db) -> None:
    """Create sparse index on evidence_case_version if not exists."""
    coll = db.collection("sparta_qra")
    indexes = coll.indexes()

    # Check if index already exists
    for idx in indexes:
        fields = idx.get("fields", [])
        if "evidence_case_version" in fields:
            logger.info("Index on evidence_case_version already exists")
            return

    # Create sparse persistent index
    coll.add_index({
        "type": "persistent",
        "fields": ["evidence_case_version"],
        "sparse": True,
        "name": "idx_evidence_case_version"
    })
    logger.info("Created sparse index on evidence_case_version")


def build_evidence_case(qra: dict, db) -> dict | None:
    """Build evidence_case for a single QRA using extract_entities.

    Returns the evidence_case dict or None on failure.
    """
    from ..entity_extraction import extract_entities

    # Use QRA question as input text
    question = qra.get("question") or qra.get("problem") or ""
    if not question:
        return None

    try:
        result = extract_entities(question, db=db, include_taxonomy=False, skip_daemon_recall=True)

        # Build glossary from control_metadata (list of dicts)
        glossary = []
        for meta in (result.control_metadata or []):
            glossary.append({
                "id": meta.get("control_id"),
                "name": meta.get("name", ""),
                "framework": meta.get("framework", ""),
                "type": meta.get("type", ""),
                "description": meta.get("description", ""),
            })

        # Build crosswalk chains from control_metadata taxonomy
        crosswalk_chains = []
        for meta in (result.control_metadata or []):
            taxonomy = meta.get("taxonomy") or []
            for tx in taxonomy:
                crosswalk_chains.append({
                    "source_id": tx.get("from"),
                    "source_framework": meta.get("framework", ""),
                    "target_id": tx.get("to"),
                    "target_framework": tx.get("framework", ""),
                    "method": tx.get("method", ""),
                })

        # Build resolved_entities from resolution_map
        resolved_entities = []
        for cid, info in (result.resolution_map or {}).items():
            if info.get("exists"):
                resolved_entities.append({
                    "id": cid,
                    "name": info.get("name", ""),
                    "framework": "",  # Not in resolution_map
                })

        # Include spans for sentence markup (character positions + metadata)
        # Each span: {entity, start, end, type, status, framework, category, name}
        spans = result.spans or []

        return {
            "question_text": question[:500],
            "resolved_entities": resolved_entities,
            "glossary": glossary,
            "crosswalk_chains": crosswalk_chains,
            "control_ids": result.control_ids or [],
            "spans": spans,  # Character positions for UI highlighting
            "review_status": "auto",
            "extracted_at": datetime.utcnow().isoformat(),
        }
    except Exception as exc:
        logger.warning("extract_entities failed for {}: {}", qra.get("_key"), exc)
        return None


def batch_update(db, updates: list[dict]) -> int:
    """Batch update documents using optimized AQL (no RETURN).

    Returns count of updated documents.
    """
    if not updates:
        return 0

    # AQL batch update - triggers cluster optimization
    aql = """
    FOR item IN @batch
      UPDATE item._key WITH {
        evidence_case: item.evidence_case,
        evidence_case_version: @version
      } IN sparta_qra
    """

    db.aql.execute(aql, bind_vars={
        "batch": updates,
        "version": EVIDENCE_CASE_VERSION,
    })

    return len(updates)


def backfill_evidence_case(
    db,
    batch_size: int = 500,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Backfill evidence_case for QRAs missing it.

    Uses streaming cursor to reduce memory pressure.

    Args:
        db: ArangoDB handle
        batch_size: Documents per batch
        limit: Max documents to process (None = all)
        dry_run: If True, don't write changes

    Returns:
        Stats dict with counts and timing
    """
    stats = {
        "total_processed": 0,
        "total_updated": 0,
        "total_skipped": 0,
        "total_failed": 0,
        "batches": 0,
        "started_at": datetime.utcnow().isoformat(),
    }

    # Ensure index exists
    if not dry_run:
        ensure_index(db)

    # Count documents to process
    count_aql = """
    RETURN LENGTH(
        FOR doc IN sparta_qra
        FILTER doc.evidence_case_version == null OR doc.evidence_case_version < @version
        RETURN 1
    )
    """
    total_to_process = list(db.aql.execute(count_aql, bind_vars={"version": EVIDENCE_CASE_VERSION}))[0]
    logger.info("Documents to process: {} (limit: {})", total_to_process, limit or "none")

    if limit:
        total_to_process = min(total_to_process, limit)

    # Streaming cursor - reduces memory, suspends between batches
    fetch_aql = """
    FOR doc IN sparta_qra
    FILTER doc.evidence_case_version == null OR doc.evidence_case_version < @version
    LIMIT @limit
    RETURN doc
    """

    cursor = db.aql.execute(
        fetch_aql,
        bind_vars={"version": EVIDENCE_CASE_VERSION, "limit": limit or 999999999},
        stream=True,
        batch_size=batch_size,
        ttl=300,  # 5 min TTL, renewed on each fetch
    )

    batch_updates = []
    batch_start = time.time()

    try:
        for doc in cursor:
            stats["total_processed"] += 1

            # Build evidence_case
            evidence_case = build_evidence_case(doc, db)

            if evidence_case is None:
                stats["total_skipped"] += 1
                continue

            if not evidence_case.get("glossary"):
                stats["total_skipped"] += 1
                continue

            batch_updates.append({
                "_key": doc["_key"],
                "evidence_case": evidence_case,
            })

            # Write batch when full
            if len(batch_updates) >= batch_size:
                if not dry_run:
                    try:
                        updated = batch_update(db, batch_updates)
                        stats["total_updated"] += updated
                    except Exception as exc:
                        logger.error("Batch update failed: {}", exc)
                        stats["total_failed"] += len(batch_updates)
                else:
                    stats["total_updated"] += len(batch_updates)

                stats["batches"] += 1
                elapsed = time.time() - batch_start
                rate = stats["total_processed"] / elapsed if elapsed > 0 else 0
                logger.info(
                    "Batch {}: processed={}, updated={}, rate={:.1f}/s",
                    stats["batches"],
                    stats["total_processed"],
                    stats["total_updated"],
                    rate,
                )
                batch_updates = []

        # Final batch
        if batch_updates:
            if not dry_run:
                try:
                    updated = batch_update(db, batch_updates)
                    stats["total_updated"] += updated
                except Exception as exc:
                    logger.error("Final batch update failed: {}", exc)
                    stats["total_failed"] += len(batch_updates)
            else:
                stats["total_updated"] += len(batch_updates)
            stats["batches"] += 1

    finally:
        try:
            cursor.close()
        except Exception:
            pass

    stats["finished_at"] = datetime.utcnow().isoformat()
    stats["duration_s"] = (
        datetime.fromisoformat(stats["finished_at"]) -
        datetime.fromisoformat(stats["started_at"])
    ).total_seconds()

    return stats


def main():
    parser = argparse.ArgumentParser(description="Backfill evidence_case for QRAs")
    parser.add_argument("--batch-size", type=int, default=500, help="Documents per batch")
    parser.add_argument("--limit", type=int, default=None, help="Max documents to process")
    parser.add_argument("--dry-run", action="store_true", help="Don't write changes")
    args = parser.parse_args()

    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv())

    from ..arango_client import get_db
    db = get_db()

    logger.info("Starting evidence_case backfill (batch_size={}, limit={}, dry_run={})",
                args.batch_size, args.limit, args.dry_run)

    stats = backfill_evidence_case(
        db,
        batch_size=args.batch_size,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    logger.info("Backfill complete: {}", stats)
    print(f"\nResults:")
    print(f"  Processed: {stats['total_processed']}")
    print(f"  Updated:   {stats['total_updated']}")
    print(f"  Skipped:   {stats['total_skipped']}")
    print(f"  Failed:    {stats['total_failed']}")
    print(f"  Duration:  {stats['duration_s']:.1f}s")
    if stats['duration_s'] > 0:
        print(f"  Rate:      {stats['total_processed'] / stats['duration_s']:.1f} docs/s")


if __name__ == "__main__":
    main()
