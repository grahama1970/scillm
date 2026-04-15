"""Parallel backfill evidence_case field for all QRAs.

Uses multiprocessing to leverage all CPU cores. Each worker:
- Has its own DB connection (ArangoDB connections aren't thread-safe)
- Loads spaCy model once at worker init (not per document)
- Processes a chunk of QRAs and returns updates

Usage:
    python -m graph_memory.maintenance.evidence_case_backfill_parallel --workers 20
    python -m graph_memory.maintenance.evidence_case_backfill_parallel --workers 20 --limit 10000
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import time
from datetime import datetime
from typing import Any

from loguru import logger

# Version tag for tracking processed documents
EVIDENCE_CASE_VERSION = "v2"

# Global worker state (initialized once per worker)
_worker_db = None
_worker_initialized = False


def _init_worker():
    """Initialize worker process with DB connection and spaCy model.

    Called once per worker at pool creation. Loads heavy resources once,
    not per document.
    """
    global _worker_db, _worker_initialized

    if _worker_initialized:
        return

    # Load environment
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv())

    # Create DB connection for this worker
    from ..arango_client import get_db
    _worker_db = get_db()

    # Pre-load spaCy model (extract_entities will use cached model)
    # This import triggers flashtext processor loading too
    from ..entity_extraction import extract_entities

    _worker_initialized = True
    logger.debug("Worker {} initialized", os.getpid())


def _process_qra(qra: dict) -> dict | None:
    """Process a single QRA in worker process.

    Returns update dict or None if skipped.
    """
    global _worker_db

    from ..entity_extraction import extract_entities

    question = qra.get("question") or qra.get("problem") or ""
    if not question:
        return None

    try:
        result = extract_entities(
            question,
            db=_worker_db,
            include_taxonomy=False,
            skip_daemon_recall=True
        )

        # Build glossary from control_metadata
        glossary = []
        for meta in (result.control_metadata or []):
            glossary.append({
                "id": meta.get("control_id"),
                "name": meta.get("name", ""),
                "framework": meta.get("framework", ""),
                "type": meta.get("type", ""),
                "description": meta.get("description", ""),
            })

        if not glossary:
            return None

        # Build crosswalk chains
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

        # Build resolved_entities
        resolved_entities = []
        for cid, info in (result.resolution_map or {}).items():
            if info.get("exists"):
                resolved_entities.append({
                    "id": cid,
                    "name": info.get("name", ""),
                    "framework": "",
                })

        # Include spans for UI highlighting
        spans = result.spans or []

        return {
            "_key": qra["_key"],
            "evidence_case": {
                "question_text": question[:500],
                "resolved_entities": resolved_entities,
                "glossary": glossary,
                "crosswalk_chains": crosswalk_chains,
                "control_ids": result.control_ids or [],
                "spans": spans,
                "review_status": "auto",
                "extracted_at": datetime.utcnow().isoformat(),
            }
        }
    except Exception as exc:
        logger.warning("Worker {} failed on {}: {}", os.getpid(), qra.get("_key"), exc)
        return None


def _process_chunk(qras: list[dict]) -> list[dict]:
    """Process a chunk of QRAs. Called by pool.map().

    Returns list of update dicts (excludes None results).
    """
    _init_worker()  # Ensure worker is initialized

    results = []
    for qra in qras:
        update = _process_qra(qra)
        if update:
            results.append(update)
    return results


def batch_update(db, updates: list[dict]) -> int:
    """Batch update documents using optimized AQL."""
    if not updates:
        return 0

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


def backfill_parallel(
    workers: int = 20,
    chunk_size: int = 100,
    batch_size: int = 500,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Parallel backfill using multiprocessing.

    Args:
        workers: Number of worker processes (default 20 for Threadripper)
        chunk_size: QRAs per worker chunk (tune for memory/overhead balance)
        batch_size: Updates per DB write (cluster optimization)
        limit: Max documents to process
        dry_run: Preview mode

    Returns:
        Stats dict
    """
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv())

    from ..arango_client import get_db
    db = get_db()

    stats = {
        "total_processed": 0,
        "total_updated": 0,
        "total_skipped": 0,
        "workers": workers,
        "started_at": datetime.utcnow().isoformat(),
    }

    # Count documents to process
    count_aql = """
    RETURN LENGTH(
        FOR doc IN sparta_qra
        FILTER doc.evidence_case_version == null OR doc.evidence_case_version < @version
        RETURN 1
    )
    """
    total_to_process = list(db.aql.execute(count_aql, bind_vars={"version": EVIDENCE_CASE_VERSION}))[0]

    if limit:
        total_to_process = min(total_to_process, limit)

    logger.info("Documents to process: {} with {} workers", total_to_process, workers)

    if total_to_process == 0:
        logger.info("Nothing to process")
        stats["finished_at"] = datetime.utcnow().isoformat()
        stats["duration_s"] = 0
        return stats

    # Fetch all keys to process (lightweight - just _key and question)
    fetch_aql = """
    FOR doc IN sparta_qra
    FILTER doc.evidence_case_version == null OR doc.evidence_case_version < @version
    LIMIT @limit
    RETURN {_key: doc._key, question: doc.question, problem: doc.problem}
    """

    logger.info("Fetching document keys...")
    all_docs = list(db.aql.execute(
        fetch_aql,
        bind_vars={"version": EVIDENCE_CASE_VERSION, "limit": limit or 999999999},
    ))
    logger.info("Fetched {} documents", len(all_docs))

    # Split into chunks for workers
    chunks = [all_docs[i:i + chunk_size] for i in range(0, len(all_docs), chunk_size)]
    logger.info("Split into {} chunks of ~{} docs each", len(chunks), chunk_size)

    # Process with multiprocessing pool
    start_time = time.time()
    all_updates = []

    # Use spawn to avoid fork issues with DB connections
    ctx = mp.get_context("spawn")

    with ctx.Pool(processes=workers) as pool:
        # Process chunks with progress logging
        for i, result_batch in enumerate(pool.imap_unordered(_process_chunk, chunks)):
            all_updates.extend(result_batch)
            stats["total_processed"] += chunk_size

            # Log progress every 10 chunks
            if (i + 1) % 10 == 0:
                elapsed = time.time() - start_time
                rate = stats["total_processed"] / elapsed if elapsed > 0 else 0
                pct = 100 * stats["total_processed"] / total_to_process
                logger.info(
                    "Progress: {:.1f}% ({}/{}) rate={:.1f}/s updates={}",
                    pct, stats["total_processed"], total_to_process, rate, len(all_updates)
                )

            # Batch write to DB when we have enough updates
            if len(all_updates) >= batch_size and not dry_run:
                updated = batch_update(db, all_updates[:batch_size])
                stats["total_updated"] += updated
                all_updates = all_updates[batch_size:]

    # Final batch
    if all_updates and not dry_run:
        updated = batch_update(db, all_updates)
        stats["total_updated"] += updated
    elif dry_run:
        stats["total_updated"] = len(all_updates)

    stats["total_skipped"] = total_to_process - stats["total_updated"]
    stats["finished_at"] = datetime.utcnow().isoformat()
    stats["duration_s"] = time.time() - start_time

    return stats


def main():
    # Reduce log noise
    logger.remove()
    logger.add(
        lambda msg: print(msg, end=""),
        format="{time:HH:mm:ss} | {level: <8} | {message}",
        level="INFO",
    )

    parser = argparse.ArgumentParser(description="Parallel backfill evidence_case for QRAs")
    parser.add_argument("--workers", type=int, default=20, help="Worker processes (default 20)")
    parser.add_argument("--chunk-size", type=int, default=100, help="QRAs per worker chunk")
    parser.add_argument("--batch-size", type=int, default=500, help="Updates per DB write")
    parser.add_argument("--limit", type=int, default=None, help="Max documents to process")
    parser.add_argument("--dry-run", action="store_true", help="Don't write changes")
    args = parser.parse_args()

    logger.info("Starting parallel evidence_case backfill")
    logger.info("  Workers: {}", args.workers)
    logger.info("  Chunk size: {}", args.chunk_size)
    logger.info("  Batch size: {}", args.batch_size)
    logger.info("  Limit: {}", args.limit or "none")
    logger.info("  Dry run: {}", args.dry_run)

    stats = backfill_parallel(
        workers=args.workers,
        chunk_size=args.chunk_size,
        batch_size=args.batch_size,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    logger.info("Backfill complete!")
    print(f"\nResults:")
    print(f"  Processed: {stats['total_processed']}")
    print(f"  Updated:   {stats['total_updated']}")
    print(f"  Skipped:   {stats['total_skipped']}")
    print(f"  Duration:  {stats['duration_s']:.1f}s")
    if stats['duration_s'] > 0:
        rate = stats['total_processed'] / stats['duration_s']
        print(f"  Rate:      {rate:.1f} docs/s")
        remaining = 230000 - stats['total_processed']
        if remaining > 0 and rate > 0:
            eta_min = remaining / rate / 60
            print(f"  ETA:       {eta_min:.0f} minutes for remaining docs")


if __name__ == "__main__":
    main()
