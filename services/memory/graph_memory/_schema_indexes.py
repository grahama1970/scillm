"""Vector index creation for dual embedding fields.

Each collection gets up to two sparse vector indexes:
  - embedding (384d text, MiniLM)
  - embedding_visual (2048d multimodal, Qwen3-VL)

Sparse indexes skip documents where the field is missing or null.
Introduced in ArangoDB 3.12.6. Documents with explicit null values
for the embedding field WILL cause index creation to fail — those
must be cleaned up first (the field should be absent, not null).

Requires --vector-index startup flag (set in docker-compose).
"""
from __future__ import annotations

import os

from loguru import logger


def ensure_indexes(db) -> None:
    """Create sparse vector indexes for text and multimodal embeddings."""
    EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "384"))
    MULTIMODAL_DIM = int(os.environ.get("EMBEDDING_MULTIMODAL_DIM", "2048"))

    index_specs = [
        # (collection, field, dimension, index_name)
        ("datalake_chunks", "embedding", EMBEDDING_DIM, "datalake_chunks_vec_text"),
        ("datalake_chunks", "embedding_visual", MULTIMODAL_DIM, "datalake_chunks_vec_visual"),
        ("lessons", "embedding", EMBEDDING_DIM, "lessons_vec_text"),
        ("lessons", "embedding_visual", MULTIMODAL_DIM, "lessons_vec_visual"),
        ("sparta_qra", "embedding", EMBEDDING_DIM, "sparta_qra_vec_text"),
        ("sparta_qra", "embedding_visual", MULTIMODAL_DIM, "sparta_qra_vec_visual"),
    ]

    for coll_name, field, dim, idx_name in index_specs:
        if not db.has_collection(coll_name):
            continue

        coll = db.collection(coll_name)
        existing = {idx["name"] for idx in coll.indexes()}
        if idx_name in existing:
            logger.debug("Vector index {} already exists", idx_name)
            continue

        # Count docs with vectors to set nLists and skip empty fields
        populated = list(db.aql.execute(
            "FOR d IN @@coll "
            "FILTER d.@field != null AND IS_LIST(d.@field) AND LENGTH(d.@field) > 0 "
            "COLLECT WITH COUNT INTO cnt RETURN cnt",
            bind_vars={"@coll": coll_name, "field": field},
        ))
        count = populated[0] if populated else 0
        if count == 0:
            logger.info("Skipping vector index {} — no docs have {} yet", idx_name, field)
            continue

        # nLists: sqrt(N) heuristic, clamped [1, 1000]
        n_lists = max(1, min(1000, int(count ** 0.5)))

        try:
            result = coll.add_index({
                "type": "vector",
                "fields": [field],
                "sparse": True,
                "params": {
                    "metric": "cosine",
                    "dimension": dim,
                    "nLists": n_lists,
                },
                "name": idx_name,
                "inBackground": True,
            })
            logger.info(
                "Created sparse vector index {} ({}d cosine, nLists={}, {} docs)",
                idx_name, dim, n_lists, count,
            )
        except Exception as exc:
            logger.error("Vector index {} creation error: {}", idx_name, exc)
