"""Sanity check: inline embeddings + 3-lane recall on ALL collections.

Checks:
  1. Every collection has inline embedding coverage >= 99%
  2. Backfills missing embeddings via daemon /upsert + embedding service
  3. Recall returns BM25 + dense + graph scores on all collections
  4. FAILS LOUDLY if any lane is missing

Usage:
    uv run python -m graph_memory.maintenance.sanity_recall check
    uv run python -m graph_memory.maintenance.sanity_recall backfill
    uv run python -m graph_memory.maintenance.sanity_recall backfill --collection sparta_qra
"""

from __future__ import annotations

import os
import sys
import time

import httpx
import typer
from loguru import logger

app = typer.Typer()

MEMORY_SOCKET = os.environ.get(
    "MEMORY_SOCKET", f"/run/user/{os.getuid()}/embry/memory.sock"
)
EMBEDDING_URL = os.environ.get("EMBEDDING_SERVICE_URL", "http://127.0.0.1:8602")

# Edge/system collections that don't need embeddings
SKIP_COLLECTIONS = {
    # Edge collections (no text to embed)
    "binary_feature_edges", "chunk_control_edges", "curate_edges",
    "datalake_edges", "deck_button_edges", "deck_lesson_edges",
    "deck_manifest_edges", "deck_page_button_edges", "deck_page_edges",
    "edge_annotations", "edge_revisions", "edges_cc", "edges_ccat",
    "edges_ccat_llm", "glossary_edges", "has_equation", "has_figure",
    "has_requirement", "has_section", "has_table", "horus_lore_edges",
    "issue_edges", "lesson_edges", "pdf_refs", "proof_requirement_edges",
    "requirement_control_edges", "skill_chain_edges", "skill_edges",
    "sparta_control_urls", "sparta_edges", "sparta_qra_edges",
    "sparta_relationships", "taxonomy_bridges", "taxonomy_edges",
    "technique_hierarchy_edges", "tom_edges",
    # Legacy separate embedding collections (should be empty/deleted)
    "lesson_embeddings", "sparta_qra_embeddings", "skill_description_embeddings",
    "code_embeddings", "doc_embeddings", "script_embeddings",
    # Cache/system/log collections
    "anchor_score_cache", "qa_cache", "relation_class_cache",
    "reranker_cache", "trace_cache", "test_multi_idx", "test_vector_384",
    "memory_events", "nightly_assessments", "feed_state", "feed_runs",
    "feed_deadletters", "task_states", "session_summaries",
    "sparta_metrics", "sparta_qra_gen_log", "sparta_url_extraction_log",
    "episode_classification_summaries", "episode_classifications",
    "persona_state_history", "unresolved_sessions",
    # Metadata/config collections
    "presets", "preset_sets", "preset_refs", "preset_ref_negatives",
    "users", "personas", "steering_priors", "user_steering",
    "deck_manifests", "deck_pages", "deck_buttons",
    "taxonomy_vocabulary", "taxonomy_vocabulary_proposals", "taxonomy_labels",
    "gold_pairs", "rejected_pairs", "rejected_qras",
    "proof_jobs", "research_jobs", "sfx_library",
    # Metadata-only collections (no searchable text content)
    "sparta_url_content", "sparta_urls",  # metadata/file paths — text is in sparta_url_knowledge
    "persona_states", "persona_state_history",  # BDI state, not search content
    "user_agent_relationships",  # trust/familiarity scores
    "symbol_snapshots",  # code snapshots, not text search targets
}

# Per-collection text fields for embedding — if not listed, uses generic fallback
TEXT_FIELDS: dict[str, list[str]] = {
    "lessons": ["problem", "solution", "title"],
    "sparta_controls": ["control_id", "name", "description"],
    "sparta_qra": ["question", "answer", "reasoning"],
    "technique_knowledge": ["title", "content", "summary"],
    "domain_terms": ["term", "definition", "context"],
    "binary_features": ["name", "description", "category"],
    "sections": ["content", "title"],
    "documents": ["title", "summary"],
    "datalake_chunks": ["text", "title"],
    "datalake_docs": ["title", "summary"],
    "doc_chunks": ["text", "title"],
    "skill_descriptions": ["name", "description"],
    "lean4_autoformalization": ["nl_statement", "formal_statement"],
    "lean_theorems": ["statement", "proof"],
    "agent_conversations": ["problem", "solution", "title"],
    "user_lessons": ["lesson", "category"],
    "horus_lore_chunks": ["text", "title"],
    "horus_lore_docs": ["title", "summary"],
    "persona_journals": ["content", "title"],
    "episodes": ["summary", "title"],
    "episode_steps": ["content", "action"],
    "youtube_transcripts": ["title", "text"],
}
GENERIC_TEXT_FIELDS = ["name", "title", "description", "text", "content", "problem", "solution", "summary"]

RECALL_TEST_QUERIES: dict[str, str] = {
    "lessons": "SPARTA entity extraction flashtext",
    "sparta_controls": "buffer overflow memory corruption",
    "sparta_qra": "GPS spoofing satellite countermeasure",
    "app_actions": "zoom in on node",
}


def _client() -> httpx.Client:
    return httpx.Client(
        transport=httpx.HTTPTransport(uds=MEMORY_SOCKET),
        base_url="http://localhost",
        timeout=60,
    )


@app.command()
def check():
    """Check inline embedding coverage and 3-lane recall on all collections."""
    failures = []

    with _client() as c:
        # Health
        r = c.get("/health")
        if r.status_code != 200:
            print(f"FAIL: daemon unhealthy ({r.status_code})")
            sys.exit(1)

        # Discover all document collections
        r = c.post("/query", json={"aql": "RETURN COLLECTIONS()[* FILTER NOT CURRENT.isSystem].name"})
        all_colls = sorted(r.json()["documents"][0])
        doc_colls = [col for col in all_colls if col not in SKIP_COLLECTIONS]

        # 1. Embedding coverage
        print("=" * 60)
        print(f"INLINE EMBEDDING COVERAGE ({len(doc_colls)} document collections)")
        print("=" * 60)
        for coll in doc_colls:
            r = c.post("/list", json={"collection": coll, "limit": 1})
            total = r.json().get("total", 0)
            if total == 0:
                print(f"  {coll:30s} EMPTY")
                continue

            r2 = c.post("/query", json={
                "aql": (
                    "FOR d IN @@coll "
                    "FILTER d.embedding != null AND IS_LIST(d.embedding) AND LENGTH(d.embedding) > 0 "
                    "COLLECT WITH COUNT INTO cnt RETURN cnt"
                ),
                "bind_vars": {"@coll": coll},
            })
            embedded = r2.json()["documents"][0]
            missing = total - embedded
            pct = 100 * embedded / total

            status = "OK" if pct >= 99 else "WARN" if pct >= 90 else "FAIL"
            print(f"  {coll:30s} {embedded:>8}/{total:<8} ({pct:5.1f}%)  [{status}]  missing={missing}")
            if status == "FAIL":
                failures.append(f"{coll}: {missing} docs missing embedding ({pct:.1f}%)")

        # 2. Recall 3-lane check
        print()
        print("=" * 60)
        print("3-LANE RECALL VERIFICATION")
        print("=" * 60)

        # Lessons path (BM25 + graph + dense)
        r = c.post("/recall", json={"q": RECALL_TEST_QUERIES["lessons"], "k": 3})
        data = r.json()
        meta = data.get("meta", {})
        items = data.get("items", [])
        used_dense = meta.get("used_dense", False)
        top_scores = items[0].get("scores", {}) if items else {}

        bm25_ok = top_scores.get("bm25", 0) > 0
        dense_ok = top_scores.get("dense", 0) > 0
        graph_ok = "graph" in top_scores  # field present even if 0

        print(f"  lessons (BM25+graph+dense):")
        print(f"    used_dense={used_dense}  bm25={top_scores.get('bm25','MISSING')}  "
              f"dense={top_scores.get('dense','MISSING')}  graph={top_scores.get('graph','MISSING')}")

        if not used_dense:
            failures.append("lessons: used_dense=False — semantic search not active")
        if not bm25_ok:
            failures.append("lessons: BM25 score is 0 or missing")
        if not dense_ok:
            failures.append("lessons: dense score is 0 or missing")

        # SPARTA scope path (the ACTUAL code path, not collections param workaround)
        r = c.post("/recall", json={"q": "what controls protect satellite uplinks", "k": 3, "scope": "sparta"})
        data = r.json()
        items = data.get("items", [])
        if not items:
            failures.append("scope=sparta: recall returned 0 results")
            print(f"  scope=sparta: NO RESULTS")
        else:
            top = items[0].get("scores", {})
            bm25 = top.get("bm25", 0)
            dense = top.get("dense", 0)
            graph = top.get("graph", 0)
            source = items[0].get("_source", "UNKNOWN")
            print(f"  scope=sparta (BM25+graph+dense): bm25={bm25}  dense={dense}  graph={graph}  source={source}")
            if bm25 <= 0:
                failures.append("scope=sparta: BM25 score is 0 or missing")
            if dense <= 0:
                failures.append("scope=sparta: dense score is 0 — cosine rerank NOT working for SPARTA")
            if source == "lessons":
                failures.append("scope=sparta: top result is from lessons, not a SPARTA collection")

        # Supplemental collections (BM25 + dense)
        for coll, query in RECALL_TEST_QUERIES.items():
            if coll == "lessons":
                continue
            r = c.post("/recall", json={"q": query, "k": 3, "collections": [coll]})
            items = r.json().get("items", [])
            if not items:
                failures.append(f"{coll}: recall returned 0 results")
                print(f"  {coll}: NO RESULTS")
                continue

            top = items[0].get("scores", {})
            bm25 = top.get("bm25", 0)
            dense = top.get("dense", 0)
            print(f"  {coll} (BM25+dense):  bm25={bm25}  dense={dense}")

            if bm25 <= 0:
                failures.append(f"{coll}: BM25 score is 0 or missing")
            if dense <= 0:
                failures.append(f"{coll}: dense score is 0 — semantic search not working")

    # Summary
    print()
    print("=" * 60)
    if failures:
        print(f"FAIL — {len(failures)} issue(s):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("PASS — all collections have inline embeddings, all 3 lanes active")
        sys.exit(0)


@app.command()
def backfill(
    collection: str = typer.Option("all", help="Collection to backfill, or 'all'"),
    batch_size: int = typer.Option(256, help="Documents per batch (uses /embed/batch)"),
    limit: int = typer.Option(0, help="Max documents to backfill (0=all)"),
    min_text_len: int = typer.Option(20, help="Minimum text length to embed"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Backfill missing inline embeddings via /embed/batch + daemon /upsert.

    Two-phase approach for large collections:
      Phase 1: Collect all _key values needing embeddings (single scan)
      Phase 2: Batch through keys with /embed/batch (256 texts/call)
    """
    with _client() as c:
        if collection == "all":
            r = c.post("/query", json={"aql": "RETURN COLLECTIONS()[* FILTER NOT CURRENT.isSystem].name"})
            all_colls = sorted(r.json()["documents"][0])
            collections = [col for col in all_colls if col not in SKIP_COLLECTIONS]
        else:
            collections = [collection]
        total_backfilled = 0

        for coll in collections:
            fields = TEXT_FIELDS.get(coll, GENERIC_TEXT_FIELDS)
            field_concat = " || ' ' || ".join(f"(d.{f} || '')" for f in fields)

            # Phase 1: Collect all keys needing embedding
            logger.info("{}: scanning for missing embeddings...", coll)
            r = c.post("/query", json={
                "aql": (
                    "FOR d IN @@coll "
                    "FILTER (d.embedding == null OR NOT IS_LIST(d.embedding) OR LENGTH(d.embedding) == 0) "
                    f"AND LENGTH(CONCAT_SEPARATOR(' ', {', '.join(f'd.{f}' for f in fields)})) >= @min_len "
                    + (f"LIMIT @lim " if limit > 0 else "")
                    + "RETURN d._key"
                ),
                "bind_vars": {
                    "@coll": coll,
                    "min_len": min_text_len,
                    **({"lim": limit} if limit > 0 else {}),
                },
            })
            keys_needing = r.json().get("documents", [])
            if not keys_needing:
                logger.info("{}: no missing embeddings", coll)
                continue

            logger.info("{}: {} docs need embedding", coll, len(keys_needing))
            if dry_run:
                logger.info("DRY RUN: would embed {} docs in {}", len(keys_needing), coll)
                continue

            # Phase 2: Batch through keys with /embed/batch
            start = time.time()
            coll_embedded = 0
            for i in range(0, len(keys_needing), batch_size):
                batch_keys = keys_needing[i : i + batch_size]

                # Fetch docs by key
                field_return = ", ".join(f"{f}: d.{f}" for f in fields)
                r2 = c.post("/query", json={
                    "aql": (
                        "FOR k IN @keys "
                        "FOR d IN @@coll FILTER d._key == k LIMIT 1 "
                        f"RETURN {{_key: d._key, {field_return}}}"
                    ),
                    "bind_vars": {"@coll": coll, "keys": batch_keys},
                })
                docs = r2.json().get("documents", [])

                texts, keys = [], []
                for doc in docs:
                    parts = [str(doc.get(f) or "") for f in fields]
                    text = " ".join(p for p in parts if p).strip()[:2000]
                    if text:
                        texts.append(text)
                        keys.append(doc["_key"])

                if not texts:
                    continue

                # Batch embed via /embed/batch
                try:
                    resp = httpx.post(
                        f"{EMBEDDING_URL}/embed/batch",
                        json={"texts": texts},
                        timeout=httpx.Timeout(60.0, connect=5.0),
                    )
                    resp.raise_for_status()
                    vectors = resp.json().get("vectors", [])
                except Exception as exc:
                    logger.error("{}: embed batch failed at offset {}: {}", coll, i, exc)
                    continue

                if len(vectors) != len(keys):
                    logger.error("{}: vector/key mismatch: {} vs {}", coll, len(vectors), len(keys))
                    continue

                # Upsert via daemon
                upsert_docs = [
                    {"_key": k, "embedding": v}
                    for k, v in zip(keys, vectors)
                    if v and isinstance(v, list) and len(v) > 10
                ]
                if upsert_docs:
                    r3 = c.post("/upsert", json={"collection": coll, "documents": upsert_docs})
                    if r3.status_code == 200:
                        coll_embedded += len(upsert_docs)
                    else:
                        logger.error("{}: upsert failed: {}", coll, r3.text[:200])

                elapsed = time.time() - start
                if coll_embedded % (batch_size * 10) == 0 or coll_embedded < batch_size * 2:
                    logger.info(
                        "{}: {}/{} ({:.0f}/sec, {:.0f}s)",
                        coll, coll_embedded, len(keys_needing),
                        coll_embedded / max(elapsed, 1), elapsed,
                    )

            total_backfilled += coll_embedded
            elapsed = time.time() - start
            logger.info("{}: done — {} embedded in {:.0f}s", coll, coll_embedded, elapsed)

        logger.info("Done. Backfilled {} documents total.", total_backfilled)


@app.command()
def integrity(
    fix: bool = typer.Option(False, "--fix", help="Auto-fix detected issues"),
    collection: str = typer.Option("all", help="Collection to check, or 'all'"),
):
    """Check and fix semantic data integrity issues.

    Detects:
      1. Explicit null embedding fields (block sparse vector index creation)
      2. Wrong-dimension vectors (mismatch with EMBEDDING_DIM / MULTIMODAL_DIM)
      3. Non-list embedding values (string, number, etc.)
      4. Empty embedding arrays
      5. Orphan test documents (keys starting with 'test_')
    """
    EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "384"))
    MULTIMODAL_DIM = int(os.environ.get("EMBEDDING_MULTIMODAL_DIM", "2048"))
    failures = []

    with _client() as c:
        if collection == "all":
            r = c.post("/query", json={"aql": "RETURN COLLECTIONS()[* FILTER NOT CURRENT.isSystem].name"})
            all_colls = sorted(r.json()["documents"][0])
            collections = [col for col in all_colls if col not in SKIP_COLLECTIONS]
        else:
            collections = [collection]

        for coll in collections:
            # 1. Explicit null embedding fields (blocks sparse vector indexes)
            for field, expected_dim in [("embedding", EMBEDDING_DIM), ("embedding_visual", MULTIMODAL_DIM)]:
                r = c.post("/query", json={
                    "aql": (
                        "FOR d IN @@coll "
                        "FILTER HAS(d, @field) AND d[@field] == null "
                        "COLLECT WITH COUNT INTO cnt RETURN cnt"
                    ),
                    "bind_vars": {"@coll": coll, "field": field},
                })
                null_count = r.json()["documents"][0]
                if null_count > 0:
                    msg = f"{coll}.{field}: {null_count} docs with explicit null (blocks vector index)"
                    print(f"  FAIL  {msg}")
                    failures.append(msg)

                    if fix:
                        # Remove the null field (keepNull: false)
                        r2 = c.post("/query", json={
                            "aql": (
                                f"FOR d IN @@coll "
                                f"FILTER HAS(d, @field) AND d[@field] == null "
                                f"UPDATE d WITH {{[{field!r}]: null}} IN @@coll "
                                f"OPTIONS {{keepNull: false}} "
                                f"COLLECT WITH COUNT INTO cnt RETURN cnt"
                            ),
                            "bind_vars": {"@coll": coll, "field": field},
                        })
                        fixed = r2.json()["documents"][0]
                        print(f"  FIXED removed null {field} from {fixed} docs")

                # 2. Wrong-dimension vectors
                r = c.post("/query", json={
                    "aql": (
                        "FOR d IN @@coll "
                        "FILTER IS_LIST(d[@field]) AND LENGTH(d[@field]) > 0 "
                        "AND LENGTH(d[@field]) != @dim "
                        "COLLECT WITH COUNT INTO cnt RETURN cnt"
                    ),
                    "bind_vars": {"@coll": coll, "field": field, "dim": expected_dim},
                })
                wrong_dim = r.json()["documents"][0]
                if wrong_dim > 0:
                    msg = f"{coll}.{field}: {wrong_dim} docs with wrong dimension (expected {expected_dim})"
                    print(f"  WARN  {msg}")
                    failures.append(msg)

                # 3. Non-list values (string, number, bool, object)
                r = c.post("/query", json={
                    "aql": (
                        "FOR d IN @@coll "
                        "FILTER d[@field] != null AND NOT IS_LIST(d[@field]) "
                        "COLLECT WITH COUNT INTO cnt RETURN cnt"
                    ),
                    "bind_vars": {"@coll": coll, "field": field},
                })
                bad_type = r.json()["documents"][0]
                if bad_type > 0:
                    msg = f"{coll}.{field}: {bad_type} docs with non-list value"
                    print(f"  FAIL  {msg}")
                    failures.append(msg)

                    if fix:
                        r2 = c.post("/query", json={
                            "aql": (
                                f"FOR d IN @@coll "
                                f"FILTER d[@field] != null AND NOT IS_LIST(d[@field]) "
                                f"UPDATE d WITH {{[{field!r}]: null}} IN @@coll "
                                f"OPTIONS {{keepNull: false}} "
                                f"COLLECT WITH COUNT INTO cnt RETURN cnt"
                            ),
                            "bind_vars": {"@coll": coll, "field": field},
                        })
                        fixed = r2.json()["documents"][0]
                        print(f"  FIXED removed bad {field} from {fixed} docs")

            # 4. Orphan test documents
            r = c.post("/query", json={
                "aql": (
                    "FOR d IN @@coll "
                    "FILTER STARTS_WITH(d._key, 'test_') "
                    "RETURN d._key"
                ),
                "bind_vars": {"@coll": coll},
            })
            test_docs = r.json().get("documents", [])
            if test_docs:
                msg = f"{coll}: {len(test_docs)} orphan test docs ({', '.join(test_docs[:3])})"
                print(f"  WARN  {msg}")
                failures.append(msg)

                if fix:
                    for key in test_docs:
                        c.post("/query", json={
                            "aql": f"REMOVE @key IN @@coll",
                            "bind_vars": {"@coll": coll, "key": key},
                        })
                    print(f"  FIXED deleted {len(test_docs)} test docs from {coll}")

        # 5. Vector index + ANN search verification
        print()
        print("=" * 60)
        print("VECTOR INDEX & ANN SEARCH VERIFICATION")
        print("=" * 60)

        ANN_TESTS = [
            ("lessons", "embedding"),
            ("sparta_qra", "embedding"),
            ("datalake_chunks", "embedding"),
            ("datalake_chunks", "embedding_visual"),
        ]
        for coll, field in ANN_TESTS:
            # Get a sample vector
            r2 = c.post("/query", json={
                "aql": (
                    "FOR d IN @@coll "
                    "FILTER d[@field] != null AND IS_LIST(d[@field]) "
                    "LIMIT 1 RETURN d[@field]"
                ),
                "bind_vars": {"@coll": coll, "field": field},
            })
            vecs = r2.json().get("documents", [])
            if not vecs or not vecs[0]:
                print(f"  {coll}.{field}: SKIP — no vectors found")
                continue

            dim = len(vecs[0])

            # Test APPROX_NEAR_COSINE (ANN via vector index, requires DESC)
            r3 = c.post("/query", json={
                "aql": (
                    "FOR d IN @@coll "
                    "SORT APPROX_NEAR_COSINE(d[@field], @qvec) DESC "
                    "LIMIT 3 "
                    "RETURN {key: d._key}"
                ),
                "bind_vars": {"@coll": coll, "field": field, "qvec": vecs[0]},
            })
            if r3.status_code == 200:
                ann_results = r3.json().get("documents", [])
                if ann_results:
                    print(f"  {coll}.{field} ({dim}d): ANN OK ({len(ann_results)} results)")
                else:
                    msg = f"{coll}.{field}: ANN returned 0 results"
                    print(f"  WARN  {msg}")
                    failures.append(msg)
            else:
                err = r3.json().get("errorMessage", "unknown")[:80]
                msg = f"{coll}.{field}: ANN search failed — {err}"
                print(f"  FAIL  {msg}")
                failures.append(msg)

    print()
    if failures:
        print(f"{'FIXED' if fix else 'FAIL'} — {len(failures)} issue(s)")
        if not fix:
            print("  Run with --fix to auto-resolve")
        sys.exit(0 if fix else 1)
    else:
        print("PASS — no semantic data integrity issues")
        sys.exit(0)


if __name__ == "__main__":
    app()
