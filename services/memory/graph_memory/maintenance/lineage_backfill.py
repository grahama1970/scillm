"""Lineage backfill for sparta_qra documents.

Pass 1: Backfills the `lineage` field for QRAs that don't have it, using:
- Keyset pagination (no streaming cursor, no TTL expiration)
- Batched AQL queries with IN + COLLECT pattern (4 queries for N control IDs)
- Direct AQL UPSERT (no HTTP round-trips)

Pass 2: Builds `related_qra_keys` by finding QRAs with overlapping SPARTA techniques:
- Build inverted index: SPARTA_technique → [qra_keys]
- For each QRA, lookup related keys via shared SPARTA entities
- Filter by technique overlap, capped at 50 candidates per QRA
- LLM filters at inference time via filter_related_qras()

Usage:
    from graph_memory.maintenance.lineage_backfill import backfill_lineage, backfill_related_qra_keys

    # Pass 1: Entity extraction
    result = backfill_lineage(batch_size=200, max_docs=None, dry_run=False)

    # Pass 2: Related QRA keys
    result = backfill_related_qra_keys(batch_size=500, max_docs=None, dry_run=False)
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from queue import Queue, Empty
from threading import Thread, Lock
from typing import Any, Callable, Optional

import httpx
from loguru import logger


def _get_embedding_default(text: str) -> Optional[list[float]]:
    """Get embedding from local embedding service (port 8602)."""
    try:
        resp = httpx.post(
            "http://localhost:8602/embed",
            json={"text": text[:2000]},
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json().get("embedding")
    except Exception as e:
        logger.warning("Embedding failed: {}", e)
    return None


def build_lineage_bulk(db, control_ids: list[str]) -> dict[str, dict]:
    """Build crosswalk chains for control IDs using direct AQL (bidirectional).

    Uses batched queries with IN + COLLECT pattern:
    - Query 1: Fetch source controls
    - Query 2: Fetch direct edges (CWE/CAPEC→SPARTA) — forward
    - Query 3: Fetch NIST 2-hop edges — forward
    - Query 4: Fetch reverse edges (what maps TO SPARTA controls) — reverse

    Args:
        db: ArangoDB database connection
        control_ids: List of control IDs (CWE-79, CAPEC-86, IA-0001, etc.)

    Returns:
        {control_id: {chains: [...], source_framework: str, reverse_sources: [...]}}
    """
    cids = [cid.upper().strip() for cid in control_ids if cid]
    if not cids:
        return {}

    results: dict[str, dict] = {}

    # --- Query 1: Batch fetch all source controls ---
    source_docs: dict[str, dict] = {}
    try:
        cursor = db.aql.execute(
            "FOR doc IN sparta_controls FILTER doc.control_id IN @cids RETURN doc",
            bind_vars={"cids": cids},
        )
        for doc in cursor:
            cid = doc.get("control_id")
            if cid:
                source_docs[cid] = doc
    except Exception as exc:
        logger.error("build_lineage_bulk: source fetch failed: {}", exc)
        return {}

    # --- Query 2: Batch fetch direct edges (CWE/CAPEC→SPARTA) ---
    direct_edges: dict[str, list] = {cid: [] for cid in cids}
    try:
        cursor = db.aql.execute("""
            FOR e IN sparta_relationships
                FILTER e.source_control_id IN @cids
                FILTER e.target_framework IN ["SPARTA", "sparta"]
                COLLECT source_id = e.source_control_id INTO edges = e
                RETURN {source_id, edges}
        """, bind_vars={"cids": cids})
        for row in cursor:
            sid = row.get("source_id")
            if sid:
                direct_edges[sid] = row.get("edges", [])
    except Exception as exc:
        logger.error("build_lineage_bulk: direct edges query failed: {}", exc)

    # --- Query 3: Batch fetch NIST edges (for CWEs with nist_control_ids) ---
    nist_to_cwe: dict[str, list[str]] = {}
    for cid, doc in source_docs.items():
        for nist_id in (doc.get("nist_control_ids") or []):
            nist_to_cwe.setdefault(nist_id, []).append(cid)

    nist_edges: dict[str, list] = {}
    if nist_to_cwe:
        try:
            nist_ids = list(nist_to_cwe.keys())
            cursor = db.aql.execute("""
                FOR e IN sparta_relationships
                    FILTER e.source_control_id IN @nist_ids
                    FILTER e.target_framework IN ["SPARTA", "sparta"]
                    COLLECT source_id = e.source_control_id INTO edges = e
                    RETURN {source_id, edges}
            """, bind_vars={"nist_ids": nist_ids})
            for row in cursor:
                sid = row.get("source_id")
                if sid:
                    nist_edges[sid] = row.get("edges", [])
        except Exception as exc:
            logger.error("build_lineage_bulk: NIST edges query failed: {}", exc)

    # --- Query 4: Reverse lookup — what maps TO SPARTA controls ---
    # For SPARTA-anchored QRAs, find CWEs/CAPECs/NIST that map to them
    # Identify SPARTA controls by checking against the database (not hardcoded prefixes)
    sparta_cids: list[str] = []
    try:
        cursor = db.aql.execute("""
            FOR doc IN sparta_controls
                FILTER doc.source_framework IN ["SPARTA", "sparta"]
                FILTER UPPER(doc.control_id) IN @cids
                RETURN UPPER(doc.control_id)
        """, bind_vars={"cids": cids})
        sparta_cids = list(cursor)
    except Exception as exc:
        logger.error("build_lineage_bulk: SPARTA control lookup failed: {}", exc)

    reverse_edges: dict[str, list[str]] = {cid: [] for cid in sparta_cids}

    if sparta_cids:
        try:
            cursor = db.aql.execute("""
                FOR e IN sparta_relationships
                    FILTER e.target_control_id IN @sparta_cids
                    COLLECT target_id = e.target_control_id INTO sources = e.source_control_id
                    RETURN {target_id, sources}
            """, bind_vars={"sparta_cids": sparta_cids})
            for row in cursor:
                tid = row.get("target_id")
                if tid:
                    reverse_edges[tid] = row.get("sources", [])
        except Exception as exc:
            logger.error("build_lineage_bulk: reverse lookup query failed: {}", exc)

    # --- Assemble chains for each control ID ---
    for cid in cids:
        if cid not in source_docs:
            results[cid] = {"chains": [], "source_framework": ""}
            continue

        source_doc = source_docs[cid]
        cid_fw = "CWE" if cid.startswith("CWE-") else (
            "CAPEC" if cid.startswith("CAPEC-") else source_doc.get("source_framework", "")
        )

        chains: list[dict] = []
        reverse_sources: list[str] = []  # CWEs/CAPECs that map TO this control

        # Priority 1: Direct edges (forward: this control → SPARTA)
        for edge in direct_edges.get(cid, []):
            target_id = edge.get("target_control_id")
            if target_id:
                chains.append({
                    "source_id": cid,
                    "source_framework": cid_fw,
                    "target_id": target_id,
                    "target_framework": "SPARTA",
                    "method": "direct",
                    "hops": [],
                })

        # Priority 2: NIST 2-hop (if no direct edges)
        if not chains:
            for nist_id in (source_doc.get("nist_control_ids") or []):
                for edge in nist_edges.get(nist_id, []):
                    target_id = edge.get("target_control_id")
                    if target_id:
                        chains.append({
                            "source_id": cid,
                            "source_framework": cid_fw,
                            "target_id": target_id,
                            "target_framework": "SPARTA",
                            "method": "nist_2hop",
                            "hops": [{"id": nist_id, "framework": "NIST"}],
                        })

        # Priority 3: Reverse edges (what maps TO this SPARTA control)
        if cid in reverse_edges:
            reverse_sources = reverse_edges[cid]

        results[cid] = {
            "chains": chains,
            "source_framework": cid_fw,
            "reverse_sources": reverse_sources,  # CWEs/CAPECs that map TO this control
        }

    return results


def upsert_qra_batch(db, docs: list[dict]) -> tuple[int, int]:
    """Update lineage field on QRA documents using direct AQL.

    Uses UPDATE (not UPSERT) to only modify existing docs. Falls back to
    individual updates if batch fails (ArangoSearch View or vector index constraints).

    Args:
        db: ArangoDB database connection
        docs: List of documents with _key and lineage (and optionally embedding)

    Returns:
        (success_count, skip_count)
    """
    if not docs:
        return 0, 0

    success = 0
    skipped = 0

    # Separate docs with/without new embeddings
    with_emb = [d for d in docs if d.get("embedding")]
    without_emb = [d for d in docs if not d.get("embedding")]

    # Update docs WITH new embeddings (full update)
    if with_emb:
        try:
            db.aql.execute("""
                FOR doc IN @docs
                    UPDATE {_key: doc._key} WITH {lineage: doc.lineage, embedding: doc.embedding}
                    IN sparta_qra
            """, bind_vars={"docs": with_emb})
            success += len(with_emb)
        except Exception as exc:
            logger.warning("Batch update (with emb) failed, falling back to individual: {}", str(exc)[:100])
            for d in with_emb:
                try:
                    db.aql.execute("""
                        UPDATE {_key: @key} WITH {lineage: @lineage, embedding: @emb} IN sparta_qra
                    """, bind_vars={"key": d["_key"], "lineage": d["lineage"], "emb": d["embedding"]})
                    success += 1
                except Exception as e2:
                    if "vector field not present" in str(e2):
                        skipped += 1
                        logger.debug("Skipped {} (vector index constraint)", d["_key"])
                    else:
                        logger.error("Individual update failed for {}: {}", d["_key"], str(e2)[:100])

    # Update docs WITHOUT embeddings - these need special handling
    # First check if they already have embeddings in the database
    if without_emb:
        keys = [d["_key"] for d in without_emb]
        key_to_doc = {d["_key"]: d for d in without_emb}

        # Check which docs already have embeddings in DB
        try:
            cursor = db.aql.execute("""
                FOR doc IN sparta_qra
                    FILTER doc._key IN @keys
                    RETURN {_key: doc._key, has_emb: doc.embedding != null}
            """, bind_vars={"keys": keys})
            db_state = {r["_key"]: r["has_emb"] for r in cursor}
        except Exception as exc:
            logger.error("Failed to check embedding state: {}", exc)
            db_state = {}

        # Docs with embeddings in DB can be updated
        can_update = [k for k in keys if db_state.get(k, False)]
        cannot_update = [k for k in keys if not db_state.get(k, False)]

        if can_update:
            docs_to_update = [key_to_doc[k] for k in can_update]
            try:
                db.aql.execute("""
                    FOR doc IN @docs
                        UPDATE {_key: doc._key} WITH {lineage: doc.lineage}
                        IN sparta_qra
                """, bind_vars={"docs": docs_to_update})
                success += len(can_update)
            except Exception as exc:
                logger.warning("Batch update (existing emb) failed: {}", str(exc)[:100])
                for d in docs_to_update:
                    try:
                        db.aql.execute("""
                            UPDATE {_key: @key} WITH {lineage: @lineage} IN sparta_qra
                        """, bind_vars={"key": d["_key"], "lineage": d["lineage"]})
                        success += 1
                    except Exception as e2:
                        if "vector field not present" in str(e2):
                            skipped += 1
                        else:
                            logger.error("Individual update failed for {}: {}", d["_key"], str(e2)[:100])

        # Docs without embeddings in DB need embedding generation first
        if cannot_update:
            skipped += len(cannot_update)
            logger.info("Skipped {} docs without embeddings (vector index constraint)", len(cannot_update))

    return success, skipped


def iter_qras_missing_lineage(
    db,
    batch_size: int = 200,
    max_docs: Optional[int] = None,
) -> tuple[int, Any]:
    """Iterate QRAs missing lineage using keyset pagination.

    Uses `FILTER doc._key > @last_key SORT doc._key LIMIT @batch_size`
    which uses the primary index and has no cursor to expire.

    Args:
        db: ArangoDB database connection
        batch_size: Number of documents per batch
        max_docs: Maximum total documents to yield (None = all)

    Yields:
        Batches of documents (list[dict])

    Returns:
        Tuple of (total_count, generator)
    """
    # Get count first (fast indexed count)
    count_cursor = db.aql.execute(
        "RETURN LENGTH(FOR doc IN sparta_qra FILTER doc.lineage == null RETURN 1)"
    )
    total_count = next(count_cursor, 0)

    def _generate():
        last_key = ""
        docs_yielded = 0

        while True:
            cursor = db.aql.execute("""
                FOR doc IN sparta_qra
                    FILTER doc.lineage == null
                    FILTER doc._key > @last_key
                    SORT doc._key
                    LIMIT @batch_size
                    RETURN doc
            """, bind_vars={"last_key": last_key, "batch_size": batch_size})

            batch = list(cursor)
            if not batch:
                break

            yield batch
            docs_yielded += len(batch)
            last_key = batch[-1]["_key"]

            if max_docs and docs_yielded >= max_docs:
                break

    return total_count, _generate()


def _process_batch(
    docs: list[dict],
    db,
    dry_run: bool,
    get_embedding: Callable[[str], Optional[list[float]]],
) -> list[dict[str, Any]]:
    """Process a batch of documents for lineage enrichment.

    Args:
        docs: List of QRA documents
        db: ArangoDB connection
        dry_run: If True, don't persist changes
        get_embedding: Function to get embeddings (text -> vector)

    Returns:
        List of {key, status, embedded, error?}
    """
    results = []
    docs_to_store = []

    # Collect control IDs for bulk query
    control_ids = []
    doc_by_cid: dict[str, list[dict]] = {}
    no_cid_docs = []

    for doc in docs:
        control_id = doc.get("control_id", "")
        if not control_id:
            no_cid_docs.append(doc)
            continue
        control_ids.append(control_id)
        doc_by_cid.setdefault(control_id, []).append(doc)

    # Bulk query for all control IDs
    bulk_results: dict[str, dict] = {}
    if control_ids:
        bulk_results = build_lineage_bulk(db, list(set(control_ids)))

    # Process docs with control IDs
    for control_id, doc_list in doc_by_cid.items():
        lineage_data = bulk_results.get(control_id.upper(), {})
        chains = lineage_data.get("chains", [])

        for doc in doc_list:
            key = doc.get("_key", "")
            question = doc.get("question", doc.get("problem", ""))

            try:
                # Build lineage from bulk results
                entity_ids = [control_id]
                entity_frameworks = [lineage_data.get("source_framework", "")]

                # Forward direction: chains from this control → SPARTA
                for chain in chains:
                    tid = chain.get("target_id")
                    if tid and tid not in entity_ids:
                        entity_ids.append(tid)
                    for hop in chain.get("hops", []):
                        hid = hop.get("id")
                        if hid and hid not in entity_ids:
                            entity_ids.append(hid)
                            fw = hop.get("framework", "")
                            if fw and fw not in entity_frameworks:
                                entity_frameworks.append(fw)

                if chains:
                    entity_frameworks.append("SPARTA")

                # Reverse direction: CWEs/CAPECs that map TO this SPARTA control
                reverse_sources = lineage_data.get("reverse_sources", [])
                for src_id in reverse_sources:
                    if src_id and src_id not in entity_ids:
                        entity_ids.append(src_id)
                        # Infer framework from ID pattern
                        if src_id.startswith("CWE-") and "CWE" not in entity_frameworks:
                            entity_frameworks.append("CWE")
                        elif src_id.startswith("CAPEC-") and "CAPEC" not in entity_frameworks:
                            entity_frameworks.append("CAPEC")

                doc["lineage"] = {
                    "graph_version": "v1.1",  # v1.1 = bidirectional
                    "graph_edge_count": len(chains),
                    "reverse_edge_count": len(reverse_sources),
                    "entity_ids": entity_ids,
                    "entity_frameworks": list(set(entity_frameworks)),
                    "upstream_qra_keys": [],
                    "source_doc_hashes": [],
                    "embedding_model": "gte-large",
                    "extractor_version": "v43-bulk-bidir",
                    "assembled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }

                # Add embedding if missing
                embedded = False
                if not doc.get("embedding") and question and get_embedding:
                    emb = get_embedding(question)
                    if emb:
                        doc["embedding"] = emb
                        embedded = True

                doc.pop("_rev", None)
                doc.pop("_id", None)
                docs_to_store.append(doc)
                results.append({"key": key, "status": "updated", "embedded": embedded})

            except Exception as exc:
                results.append({"key": key, "status": "error", "error": str(exc)[:200], "embedded": False})

    # Process docs without control IDs (minimal lineage)
    for doc in no_cid_docs:
        key = doc.get("_key", "")
        question = doc.get("question", doc.get("problem", ""))

        doc["lineage"] = {
            "graph_version": "v1",
            "graph_edge_count": 0,
            "entity_ids": [],
            "entity_frameworks": [],
            "upstream_qra_keys": [],
            "source_doc_hashes": [],
            "embedding_model": "gte-large",
            "extractor_version": "v43-bulk",
            "assembled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "_no_control_id": True,
        }

        embedded = False
        if not doc.get("embedding") and question and get_embedding:
            emb = get_embedding(question)
            if emb:
                doc["embedding"] = emb
                embedded = True

        doc.pop("_rev", None)
        doc.pop("_id", None)
        docs_to_store.append(doc)
        results.append({"key": key, "status": "updated", "embedded": embedded})

    # Batch store
    if not dry_run and docs_to_store:
        try:
            upsert_qra_batch(db, docs_to_store)
        except Exception as exc:
            logger.error("Batch store failed: {}", exc)

    return results


def backfill_lineage(
    batch_size: int = 200,
    max_docs: Optional[int] = None,
    dry_run: bool = True,
    max_workers: int = 1,
    get_embedding: Optional[Callable[[str], Optional[list[float]]]] = None,
) -> dict[str, Any]:
    """Backfill lineage field for QRAs that don't have it.

    Uses:
    - Keyset pagination (no cursor TTL expiration)
    - Batched AQL with IN + COLLECT (4 queries for N control IDs)
    - Direct AQL UPSERT (no HTTP round-trips)
    - Producer-consumer with ThreadPoolExecutor

    Args:
        batch_size: Number of QRAs per batch
        max_docs: Maximum total QRAs to process (None = all)
        dry_run: If True, don't persist changes
        max_workers: Number of concurrent batch workers
        get_embedding: Optional function to get embeddings (default: localhost:8602)

    Returns:
        {processed, updated, skipped, embedded, errors, error_count, dry_run, elapsed_seconds, rate_per_second}
    """
    from graph_memory.arango_client import get_db

    if get_embedding is None:
        get_embedding = _get_embedding_default

    processed = 0
    updated = 0
    skipped = 0
    embedded = 0
    errors_list = []

    logger.info("Lineage backfill: batch_size={}, max_docs={}, dry_run={}, workers={}",
                batch_size, max_docs, dry_run, max_workers)

    db = get_db()

    batch_queue: Queue = Queue(maxsize=max_workers * 2)
    total_estimate = [None]
    fetch_done = [False]
    batches_fetched = [0]
    lock = Lock()

    start_time = time.time()
    completed_batches = [0]
    all_results = []

    def fetch_batches():
        """Producer: keyset pagination (no streaming cursor, no TTL issues)."""
        try:
            total_count, batch_gen = iter_qras_missing_lineage(db, batch_size, max_docs)
            total_estimate[0] = min(total_count, max_docs) if max_docs else total_count
            logger.info("{} QRAs missing lineage (will process up to {})",
                       total_count, max_docs or "all")

            for batch in batch_gen:
                batch_queue.put(batch)
                batches_fetched[0] += 1

        except Exception as exc:
            logger.error("Keyset pagination failed: {}", exc)

        fetch_done[0] = True

    def process_batch(batch_docs):
        """Process a single batch."""
        results = _process_batch(batch_docs, db, dry_run, get_embedding)
        with lock:
            completed_batches[0] += 1
            elapsed = time.time() - start_time
            docs_done = completed_batches[0] * batch_size
            rate = docs_done / elapsed if elapsed > 0 else 0
            est_total = total_estimate[0] or batches_fetched[0] * batch_size
            eta = (est_total - docs_done) / rate if rate > 0 else 0
            logger.info("Batch {}: {:.1f}/s, ETA {:.0f}s",
                        completed_batches[0], rate, eta)
        return results

    # Start producer thread
    fetch_thread = Thread(target=fetch_batches, daemon=True)
    fetch_thread.start()

    # Process batches as they arrive
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        inflight: dict = {}
        batch_num = 0

        while True:
            # Wait for a slot if at capacity
            while len(inflight) >= max_workers * 2:
                done, _ = wait(list(inflight.keys()), return_when=FIRST_COMPLETED)
                for fut in done:
                    try:
                        all_results.extend(fut.result())
                    except Exception as exc:
                        errors_list.append({"key": f"batch-{inflight[fut]}", "error": str(exc)[:200]})
                    del inflight[fut]

            # Get next batch
            try:
                batch = batch_queue.get(timeout=0.5)
                batch_num += 1
                fut = executor.submit(process_batch, batch)
                inflight[fut] = batch_num
            except Empty:
                if fetch_done[0] and batch_queue.empty():
                    break

        # Wait for remaining futures
        for fut in as_completed(inflight.keys()):
            try:
                all_results.extend(fut.result())
            except Exception as exc:
                errors_list.append({"key": f"batch-{inflight[fut]}", "error": str(exc)[:200]})

    fetch_thread.join(timeout=5.0)

    # Aggregate results
    for result in all_results:
        processed += 1
        status = result.get("status")
        if status == "updated":
            updated += 1
            if result.get("embedded"):
                embedded += 1
        elif status == "skipped":
            skipped += 1
        elif status == "error":
            errors_list.append({"key": result.get("key"), "error": result.get("error")})

    elapsed = time.time() - start_time
    logger.info("Completed {} docs in {:.1f}s ({:.1f}/s)", processed, elapsed, processed/elapsed if elapsed > 0 else 0)

    return {
        "processed": processed,
        "updated": updated,
        "skipped": skipped,
        "embedded": embedded,
        "errors": errors_list,
        "error_count": len(errors_list),
        "dry_run": dry_run,
        "elapsed_seconds": elapsed,
        "rate_per_second": processed / elapsed if elapsed > 0 else 0,
    }


# ---------------------------------------------------------------------------
# Pass 2: Build related_qra_keys
# ---------------------------------------------------------------------------

def _get_sparta_prefixes(db) -> set[str]:
    """Get known SPARTA control prefixes from the database."""
    try:
        cursor = db.aql.execute("""
            FOR doc IN sparta_controls
                FILTER doc.source_framework IN ["SPARTA", "sparta"]
                LET prefix = REGEX_REPLACE(doc.control_id, "[0-9-]+$", "")
                COLLECT p = prefix
                RETURN p
        """)
        prefixes = set(cursor)
        logger.info("SPARTA prefixes from DB: {}", prefixes)
        return prefixes
    except Exception as exc:
        logger.error("Failed to get SPARTA prefixes: {}", exc)
        # Fallback to known prefixes
        return {"IA-", "CM", "DE-", "EX-", "EXF-", "LM-", "PER-", "REC-", "SV-"}


def _is_sparta_entity(entity_id: str, prefixes: set[str]) -> bool:
    """Check if an entity ID is a SPARTA control."""
    upper = entity_id.upper()
    return any(upper.startswith(p.upper()) for p in prefixes)


def build_sparta_inverted_index(db, max_keys_per_entity: int = 500) -> dict[str, list[str]]:
    """Build capped inverted index: SPARTA_technique → [qra_keys].

    Only indexes SPARTA entities (not CWE, CAPEC, ATT&CK) to reduce noise.
    Caps each entity's list at max_keys_per_entity to avoid memory explosion
    (some entities like IA-0006 appear in 131K+ QRAs).

    Args:
        db: ArangoDB database connection
        max_keys_per_entity: Maximum QRA keys per entity (default 500)

    Returns:
        {sparta_control_id: [qra_key1, qra_key2, ...]} (capped at max_keys_per_entity)
    """
    logger.info("Building SPARTA inverted index (capped at {} keys/entity)...",
                max_keys_per_entity)

    # Build capped index via AQL — use COLLECT with LIMIT inside subquery
    # This returns a sample of keys for each entity, not all keys
    try:
        cursor = db.aql.execute("""
            LET entities = (
                FOR doc IN sparta_qra
                    FILTER doc.lineage != null
                    FILTER doc.lineage.entity_ids != null
                    FOR entity_id IN doc.lineage.entity_ids
                        FILTER REGEX_TEST(entity_id, "^(IA-|CM|DE-|EX-|EXF-|LM-|PER-|REC-|SV-)", true)
                        RETURN {entity: UPPER(entity_id), key: doc._key}
            )
            FOR e IN entities
                COLLECT entity = e.entity INTO grouped = e.key
                LET capped_keys = SLICE(grouped, 0, @max_keys)
                RETURN {entity, keys: capped_keys, total: LENGTH(grouped)}
        """, bind_vars={"max_keys": max_keys_per_entity}, batch_size=100)

        index: dict[str, list[str]] = {}
        total_links = 0
        capped_count = 0

        for row in cursor:
            entity = row.get("entity")
            keys = row.get("keys", [])
            total = row.get("total", len(keys))
            if entity and keys:
                index[entity] = keys
                total_links += len(keys)
                if total > max_keys_per_entity:
                    capped_count += 1

        logger.info("Inverted index built: {} SPARTA entities, {} QRA links (capped), {} entities were capped",
                    len(index), total_links, capped_count)
        return index

    except Exception as exc:
        logger.error("Failed to build inverted index: {}", exc)
        return {}


def find_related_qra_keys(
    qra_key: str,
    entity_ids: list[str],
    inverted_index: dict[str, list[str]],
    sparta_prefixes: set[str],
    max_related: int = 50,
) -> tuple[list[str], list[str]]:
    """Find related QRA keys via shared SPARTA techniques.

    Args:
        qra_key: The QRA's own key (to exclude from results)
        entity_ids: The QRA's lineage.entity_ids
        inverted_index: SPARTA entity → [qra_keys] mapping
        sparta_prefixes: Known SPARTA prefixes
        max_related: Maximum related keys to return

    Returns:
        (related_qra_keys, shared_techniques per key)
    """
    # Find SPARTA entities in this QRA's lineage
    sparta_entities = [
        eid.upper() for eid in entity_ids
        if _is_sparta_entity(eid, sparta_prefixes)
    ]

    if not sparta_entities:
        return [], []

    # Collect related keys with their shared technique counts
    key_techniques: dict[str, set[str]] = {}

    for entity in sparta_entities:
        related_keys = inverted_index.get(entity, [])
        for rk in related_keys:
            if rk != qra_key:  # Exclude self
                if rk not in key_techniques:
                    key_techniques[rk] = set()
                key_techniques[rk].add(entity)

    if not key_techniques:
        return [], []

    # Sort by number of shared techniques (descending), then by key (stable)
    sorted_keys = sorted(
        key_techniques.keys(),
        key=lambda k: (-len(key_techniques[k]), k)
    )

    # Cap at max_related
    top_keys = sorted_keys[:max_related]

    # Build shared_techniques list (for each key, which techniques they share)
    shared_per_key = [list(key_techniques[k]) for k in top_keys]

    return top_keys, shared_per_key


def backfill_related_qra_keys(
    batch_size: int = 500,
    max_docs: Optional[int] = None,
    dry_run: bool = True,
    max_related: int = 50,
) -> dict[str, Any]:
    """Pass 2: Build related_qra_keys for QRAs with lineage.

    Algorithm:
    1. Build inverted index: SPARTA_technique → [qra_keys]
    2. For each QRA with lineage:
       a. Get its SPARTA entities from lineage.entity_ids
       b. Lookup related keys from inverted index
       c. Store in lineage.related_qra_keys (capped)
    3. Set lineage.graph_version = "v2"

    The related_qra_keys are PRE-FILTERED by SPARTA technique overlap.
    At inference time, filter_related_qras() does fine-grained filtering.

    Args:
        batch_size: Number of QRAs per batch
        max_docs: Maximum total QRAs to process (None = all)
        dry_run: If True, don't persist changes
        max_related: Maximum related keys per QRA (default 50)

    Returns:
        {processed, updated, skipped, elapsed_seconds, rate_per_second}
    """
    from graph_memory.arango_client import get_db

    logger.info("Pass 2: Building related_qra_keys (batch_size={}, max_docs={}, dry_run={})",
                batch_size, max_docs, dry_run)

    db = get_db()
    start_time = time.time()

    # Step 1: Build inverted index
    inverted_index = build_sparta_inverted_index(db)
    if not inverted_index:
        return {"error": "Failed to build inverted index", "processed": 0}

    sparta_prefixes = _get_sparta_prefixes(db)

    # Step 2: Iterate QRAs that need related_qra_keys
    # Target: QRAs with lineage but without related_qra_keys or with old version
    processed = 0
    updated = 0
    skipped = 0
    errors_list = []

    last_key = ""

    while True:
        # Keyset pagination for QRAs needing Pass 2
        try:
            cursor = db.aql.execute("""
                FOR doc IN sparta_qra
                    FILTER doc.lineage != null
                    FILTER doc.lineage.graph_version < "v2" OR doc.lineage.related_qra_keys == null
                    FILTER doc._key > @last_key
                    SORT doc._key
                    LIMIT @batch_size
                    RETURN {
                        _key: doc._key,
                        entity_ids: doc.lineage.entity_ids,
                        current_version: doc.lineage.graph_version
                    }
            """, bind_vars={"last_key": last_key, "batch_size": batch_size})

            batch = list(cursor)
        except Exception as exc:
            logger.error("Failed to fetch batch: {}", exc)
            errors_list.append({"key": f"fetch-after-{last_key}", "error": str(exc)[:200]})
            break

        if not batch:
            break

        # Process batch
        updates = []
        for doc in batch:
            key = doc.get("_key", "")
            entity_ids = doc.get("entity_ids") or []

            related_keys, shared_techniques = find_related_qra_keys(
                key, entity_ids, inverted_index, sparta_prefixes, max_related
            )

            updates.append({
                "_key": key,
                "related_qra_keys": related_keys,
                "shared_techniques_map": {k: shared_techniques[i] for i, k in enumerate(related_keys)},
            })

        # Batch update
        if not dry_run and updates:
            try:
                db.aql.execute("""
                    FOR u IN @updates
                        UPDATE {_key: u._key} WITH {
                            lineage: MERGE(
                                DOCUMENT(CONCAT("sparta_qra/", u._key)).lineage,
                                {
                                    related_qra_keys: u.related_qra_keys,
                                    shared_techniques_map: u.shared_techniques_map,
                                    graph_version: "v2"
                                }
                            )
                        } IN sparta_qra
                """, bind_vars={"updates": updates})
                updated += len(updates)
            except Exception as exc:
                logger.error("Batch update failed: {}", str(exc)[:200])
                errors_list.append({"key": f"batch-{last_key}", "error": str(exc)[:200]})
                # Fall back to individual updates
                for u in updates:
                    try:
                        db.aql.execute("""
                            LET current = DOCUMENT(CONCAT("sparta_qra/", @key))
                            UPDATE {_key: @key} WITH {
                                lineage: MERGE(current.lineage, {
                                    related_qra_keys: @related,
                                    shared_techniques_map: @techniques_map,
                                    graph_version: "v2"
                                })
                            } IN sparta_qra
                        """, bind_vars={
                            "key": u["_key"],
                            "related": u["related_qra_keys"],
                            "techniques_map": u["shared_techniques_map"],
                        })
                        updated += 1
                    except Exception as e2:
                        errors_list.append({"key": u["_key"], "error": str(e2)[:100]})
        elif dry_run:
            updated += len(updates)

        processed += len(batch)
        last_key = batch[-1]["_key"]

        # Progress logging
        elapsed = time.time() - start_time
        rate = processed / elapsed if elapsed > 0 else 0
        logger.info("Pass 2: processed={}, updated={}, rate={:.1f}/s",
                    processed, updated, rate)

        if max_docs and processed >= max_docs:
            break

    elapsed = time.time() - start_time
    logger.info("Pass 2 complete: {} processed, {} updated in {:.1f}s ({:.1f}/s)",
                processed, updated, elapsed, processed / elapsed if elapsed > 0 else 0)

    return {
        "processed": processed,
        "updated": updated,
        "skipped": skipped,
        "errors": errors_list,
        "error_count": len(errors_list),
        "dry_run": dry_run,
        "elapsed_seconds": elapsed,
        "rate_per_second": processed / elapsed if elapsed > 0 else 0,
        "inverted_index_size": len(inverted_index),
    }
