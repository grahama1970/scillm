"""Skill registry: parse SKILL.md frontmatter and store in ArangoDB for recall.

Provides BM25, semantic, and graph traversal over 220+ skills. Skills are
stored in `skill_descriptions` with embeddings in `skill_description_embeddings`,
and composition/taxonomy relationships in `skill_edges`.

Write points:
  - /skills-ci apply       → ingest_skills(root, skill_filter=[fixed_skill])
  - /monitor-skill-health  → ingest_skills(root)  (nightly full refresh)
  - /skills-broadcast link → ingest_skills(root)  (post-link hook)
  - /skill-lab             → ingest_skills(root, skill_filter=[new_skill])
  - CLI                    → memory-agent ingest-skills [--skill X]
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml
from loguru import logger

from ..arango_client import get_db
from ..embeddings import encode_texts

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SKILLS_COLLECTION = "skill_descriptions"
EMBEDDINGS_COLLECTION = "skill_description_embeddings"
EDGES_COLLECTION = "skill_edges"

# Fields parsed from SKILL.md frontmatter
FRONTMATTER_FIELDS = [
    "name", "description", "triggers", "provides", "composes",
    "taxonomy", "allowed-tools", "metadata",
]


# ---------------------------------------------------------------------------
# SKILL.md frontmatter parsing
# ---------------------------------------------------------------------------

def _extract_frontmatter(skill_md: Path) -> dict | None:
    """Extract YAML frontmatter from a SKILL.md file."""
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # was bare
        return None
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        data = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        logger.warning("YAML parse error in {}: {}", skill_md, exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def _normalize_list(value: Any) -> list[str]:
    """Coerce a frontmatter value to list[str]."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        return [value]
    return []


def _extract_body(skill_md: Path) -> str:
    """Extract the markdown body (after frontmatter) for BM25 indexing."""
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # was bare
        return ""
    if not text.startswith("---"):
        return text
    parts = text.split("---", 2)
    return parts[2].strip() if len(parts) >= 3 else ""


def parse_skill(skill_dir: Path) -> dict | None:
    """Parse a skill directory into a document for storage.

    Returns None if the directory has no valid SKILL.md.
    """
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None

    fm = _extract_frontmatter(skill_md)
    if not fm:
        return None

    name = fm.get("name", skill_dir.name)
    description = fm.get("description", "")
    if isinstance(description, str):
        description = " ".join(description.split())  # collapse folded YAML

    triggers = _normalize_list(fm.get("triggers", []))
    provides = _normalize_list(fm.get("provides", []))
    composes = _normalize_list(fm.get("composes", []))
    taxonomy = _normalize_list(fm.get("taxonomy", []))
    allowed_tools = _normalize_list(fm.get("allowed-tools", []))

    metadata = fm.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}

    body = _extract_body(skill_md)
    has_run_sh = (skill_dir / "run.sh").exists()
    has_sanity_sh = (skill_dir / "sanity.sh").exists()

    return {
        "name": name,
        "description": description,
        "triggers": triggers,
        "provides": provides,
        "composes": composes,
        "taxonomy": taxonomy,
        "allowed_tools": allowed_tools,
        "metadata": metadata,
        "has_run_sh": has_run_sh,
        "has_sanity_sh": has_sanity_sh,
        "body": body[:8000],  # Cap body for storage (BM25 doesn't need more)
        "source_path": str(skill_dir),
    }


# ---------------------------------------------------------------------------
# Collection setup
# ---------------------------------------------------------------------------

def _ensure_collections(db: Any) -> None:
    """Create skill registry collections if they don't exist."""
    if not db.has_collection(SKILLS_COLLECTION):
        db.create_collection(SKILLS_COLLECTION)
    if not db.has_collection(EMBEDDINGS_COLLECTION):
        db.create_collection(EMBEDDINGS_COLLECTION)
    if not db.has_collection(EDGES_COLLECTION):
        db.create_collection(EDGES_COLLECTION, edge=True)

    # Ensure indices (best-effort)
    try:
        col = db.collection(SKILLS_COLLECTION)
        existing = [idx.get("fields", []) for idx in col.indexes()]
        if ["name"] not in existing:
            col.add_hash_index(fields=["name"], unique=True)
    except Exception as exc:
        logger.error("skill_descriptions index setup skipped: {}", exc)

    try:
        col = db.collection(EDGES_COLLECTION)
        existing = [idx.get("fields", []) for idx in col.indexes()]
        if ["edge_type"] not in existing:
            col.add_hash_index(fields=["edge_type"], unique=False)
    except Exception as exc:
        logger.error("skill_edges index setup skipped: {}", exc)


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def _build_embed_text(doc: dict) -> str:
    """Build the text to embed: name + description + triggers."""
    parts = [doc["name"], doc["description"]]
    parts.extend(doc.get("triggers", []))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Upsert logic
# ---------------------------------------------------------------------------

def _upsert_skill(db: Any, doc: dict) -> dict:
    """Insert or update a skill document in ArangoDB."""
    col = db.collection(SKILLS_COLLECTION)
    embed_col = db.collection(EMBEDDINGS_COLLECTION)
    key = f"skill_{doc['name']}"
    now = datetime.now(timezone.utc).isoformat()

    # Content hash for change detection
    content_hash = hashlib.sha256(
        f"{doc['description']}|{'|'.join(doc['triggers'])}|{'|'.join(doc['composes'])}".encode()
    ).hexdigest()[:16]

    # Check if unchanged
    try:
        existing = col.get(key)
        if existing and existing.get("content_hash") == content_hash:
            logger.debug("Skill {} unchanged, skipping", doc["name"])
            return existing
    except Exception as exc:  # was bare
        pass

    # Build document
    stored = {
        "_key": key,
        "name": doc["name"],
        "description": doc["description"],
        "triggers": doc["triggers"],
        "provides": doc["provides"],
        "composes": doc["composes"],
        "taxonomy": doc["taxonomy"],
        "allowed_tools": doc["allowed_tools"],
        "metadata": doc["metadata"],
        "has_run_sh": doc["has_run_sh"],
        "has_sanity_sh": doc["has_sanity_sh"],
        "body": doc["body"],
        "source_path": doc["source_path"],
        "content_hash": content_hash,
        "updated_at": now,
    }

    # Embed at insert time (NON-NEGOTIABLE)
    embed_text = _build_embed_text(doc)
    try:
        vectors = encode_texts([embed_text], normalize=True)
        embedding = vectors[0] if vectors else []
    except Exception as exc:
        logger.warning("Embedding failed for skill {}: {}", doc["name"], exc)
        embedding = []

    stored["_embedding_pending"] = len(embedding) == 0

    # Upsert skill document (overwrite handles both insert and update)
    stored["created_at"] = stored.get("created_at", now)
    try:
        col.insert(stored, overwrite=True)
    except Exception as exc:
        # Fallback: delete by name then insert (handles stale _key conflicts)
        try:
            db.aql.execute(
                "FOR d IN @@col FILTER d.name == @name REMOVE d IN @@col",
                bind_vars={"@col": SKILLS_COLLECTION, "name": doc["name"]},
            )
        except Exception as exc:  # was bare
            pass
        col.insert(stored)

    # Upsert embedding document
    embed_key = f"emb_{doc['name']}"
    embed_doc = {
        "_key": embed_key,
        "skill_key": key,
        "name": doc["name"],
        "embedding": embedding,
        "_embedding_pending": len(embedding) == 0,
        "updated_at": now,
    }
    try:
        embed_col.insert(embed_doc, overwrite=True)
    except Exception as exc:  # was bare
        pass

    return stored


def _upsert_edges(db: Any, doc: dict) -> int:
    """Create COMPOSES and TAXONOMY edges for a skill."""
    col = db.collection(EDGES_COLLECTION)
    skills_col_name = SKILLS_COLLECTION
    skill_key = f"skill_{doc['name']}"
    skill_id = f"{skills_col_name}/{skill_key}"
    edge_count = 0

    # Remove stale edges for this skill (single AQL round-trip)
    try:
        db.aql.execute(
            "FOR e IN @@col FILTER e._from == @sid REMOVE e IN @@col",
            bind_vars={"@col": EDGES_COLLECTION, "sid": skill_id},
        )
    except Exception as exc:
        logger.error("Edge cleanup failed for {}: {}", doc["name"], exc)

    # COMPOSES edges: this skill → composed skill
    for composed in doc.get("composes", []):
        target_key = f"skill_{composed}"
        target_id = f"{skills_col_name}/{target_key}"
        edge_key = f"composes_{doc['name']}_{composed}"
        edge_doc = {
            "_key": edge_key,
            "_from": skill_id,
            "_to": target_id,
            "edge_type": "COMPOSES",
            "source_skill": doc["name"],
            "target_skill": composed,
        }
        try:
            if col.has(edge_key):
                col.update(edge_doc)
            else:
                col.insert(edge_doc)
            edge_count += 1
        except Exception as exc:
            logger.error("Edge insert failed {}->{}: {}", doc["name"], composed, exc)

    # TAXONOMY edges: skill → taxonomy tag (stored as virtual nodes)
    for tag in doc.get("taxonomy", []):
        tag_key = f"tag_{tag}"
        tag_id = f"{skills_col_name}/{tag_key}"
        # Ensure tag node exists
        skills_col = db.collection(skills_col_name)
        if not skills_col.has(tag_key):
            try:
                skills_col.insert({
                    "_key": tag_key,
                    "name": tag,
                    "type": "taxonomy_tag",
                })
            except Exception as exc:
                pass  # Race condition — another process created it

        edge_key = f"taxonomy_{doc['name']}_{tag}"
        edge_doc = {
            "_key": edge_key,
            "_from": skill_id,
            "_to": tag_id,
            "edge_type": "TAXONOMY",
            "source_skill": doc["name"],
            "tag": tag,
        }
        try:
            if col.has(edge_key):
                col.update(edge_doc)
            else:
                col.insert(edge_doc)
            edge_count += 1
        except Exception as exc:
            logger.error("Taxonomy edge failed {}→{}: {}", doc["name"], tag, exc)

    return edge_count


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ingest_skills(
    skills_root: Path,
    skill_filter: list[str] | None = None,
    db: Any = None,
) -> dict:
    """Parse SKILL.md frontmatter and upsert to ArangoDB with embeddings and edges.

    Args:
        skills_root: Path to the skills directory (e.g., .pi/skills/)
        skill_filter: Optional list of skill names to ingest (None = all)
        db: Database handle (defaults to get_db())

    Returns:
        Summary dict with counts of ingested/skipped/failed skills.
    """
    if db is None:
        db = get_db()

    _ensure_collections(db)

    # Discover skill directories
    if skill_filter:
        skill_dirs = [skills_root / name for name in skill_filter if (skills_root / name).is_dir()]
    else:
        skill_dirs = sorted([
            d for d in skills_root.iterdir()
            if d.is_dir() and not d.name.startswith((".", "_"))
            and (d / "SKILL.md").exists()
        ])

    ingested = 0
    skipped = 0
    failed = 0
    edge_total = 0

    for skill_dir in skill_dirs:
        try:
            doc = parse_skill(skill_dir)
            if doc is None:
                skipped += 1
                continue

            result = _upsert_skill(db, doc)
            edges = _upsert_edges(db, doc)
            edge_total += edges

            if result.get("content_hash"):
                ingested += 1
            else:
                skipped += 1

            logger.debug("Ingested skill {} ({} edges)", doc["name"], edges)
        except Exception as exc:
            logger.error("Failed to ingest {}: {}", skill_dir.name, exc)
            failed += 1

    summary = {
        "ingested": ingested,
        "skipped": skipped,
        "failed": failed,
        "edges": edge_total,
        "total_dirs": len(skill_dirs),
    }
    logger.info(
        "Skill ingestion complete: {} ingested, {} skipped, {} failed, {} edges",
        ingested, skipped, failed, edge_total,
    )
    return summary


# ---------------------------------------------------------------------------
# Search / Query API
# ---------------------------------------------------------------------------

def _cosine_sim(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def search_skills(
    query: str = "",
    name: str = "",
    taxonomy: str = "",
    composes: str = "",
    provides: str = "",
    k: int = 20,
    semantic: bool = True,
    db: Any = None,
) -> list[dict]:
    """Search skills via BM25 text match, taxonomy/composes filters, and semantic similarity.

    All filters are combined (AND). An empty query with no filters returns all skills.

    Args:
        query: Free-text search over name, description, triggers, body.
        name: Exact or substring match on skill name.
        taxonomy: Filter by taxonomy tag (e.g., "Precision", "Resilience").
        composes: Filter skills that compose this skill name.
        provides: Filter skills that provide this capability.
        k: Max results to return.
        semantic: Use embedding similarity for ranking when query is provided.
        db: Database handle (defaults to get_db()).

    Returns:
        List of skill dicts sorted by relevance, each with a `_score` field.
    """
    if db is None:
        db = get_db()

    if not db.has_collection(SKILLS_COLLECTION):
        return []

    # Build AQL filter clauses
    filters = ["d.type == null OR d.type != 'taxonomy_tag'"]  # Exclude virtual tag nodes
    bind_vars: dict[str, Any] = {}

    if name:
        # LIKE with LOWER() is acceptable for substring name filters
        filters.append("LIKE(LOWER(d.name), @name_filter)")
        bind_vars["name_filter"] = f"%{name.lower()}%"

    if taxonomy:
        # Find skills connected to this taxonomy tag via edges
        filters.append("""
            d._id IN (
                FOR e IN skill_edges
                    FILTER e.edge_type == 'TAXONOMY' AND LOWER(e.tag) == @tax_filter
                    RETURN e._from
            )
        """)
        bind_vars["tax_filter"] = taxonomy.lower()

    if composes:
        # Find skills that compose a given skill
        filters.append("""
            d._id IN (
                FOR e IN skill_edges
                    FILTER e.edge_type == 'COMPOSES' AND LOWER(e.target_skill) == @comp_filter
                    RETURN e._from
            )
        """)
        bind_vars["comp_filter"] = composes.lower()

    if provides:
        filters.append("@prov_filter IN d.provides")
        bind_vars["prov_filter"] = provides

    filter_clause = " AND ".join(f"({f})" for f in filters)
    bind_vars["limit"] = k * 3 if (query and semantic) else k

    # Use BM25 via ArangoSearch view for free-text queries (stemming + case fold).
    # Fall back to collection scan only when no query text is provided.
    if query:
        bind_vars["query"] = query
        aql = f"""
            FOR d IN skill_descriptions_search
                SEARCH ANALYZER(
                    d.name IN TOKENS(@query, 'text_en') OR
                    d.description IN TOKENS(@query, 'text_en') OR
                    d.triggers IN TOKENS(@query, 'text_en'),
                    'text_en'
                )
                FILTER {filter_clause}
                LET bm25 = BM25(d)
                SORT bm25 DESC
                LIMIT @limit
                RETURN KEEP(d, 'name', 'description', 'triggers', 'provides',
                            'composes', 'taxonomy', 'has_run_sh', 'has_sanity_sh',
                            'source_path')
        """
    else:
        bind_vars["@col"] = SKILLS_COLLECTION
        aql = f"""
            FOR d IN @@col
                FILTER {filter_clause}
                LIMIT @limit
                RETURN KEEP(d, 'name', 'description', 'triggers', 'provides',
                            'composes', 'taxonomy', 'has_run_sh', 'has_sanity_sh',
                            'source_path')
        """

    try:
        cursor = db.aql.execute(aql, bind_vars=bind_vars)
        results = list(cursor)
    except Exception as exc:
        logger.error("Skill search AQL failed: {}", exc)
        return []

    # Semantic re-ranking
    if query and semantic and results:
        try:
            q_vec = encode_texts([query], normalize=True)
            if q_vec and q_vec[0]:
                q_embedding = q_vec[0]
                embed_col = db.collection(EMBEDDINGS_COLLECTION)
                for doc in results:
                    embed_key = f"emb_{doc['name']}"
                    try:
                        emb_doc = embed_col.get(embed_key)
                        if emb_doc and emb_doc.get("embedding"):
                            doc["_score"] = _cosine_sim(q_embedding, emb_doc["embedding"])
                        else:
                            doc["_score"] = 0.1  # Has text match but no embedding
                    except Exception as exc:
                        doc["_score"] = 0.1
                results.sort(key=lambda x: x.get("_score", 0), reverse=True)
        except Exception as exc:
            logger.error("Semantic ranking skipped: {}", exc)

    # If no semantic ranking, score by text match quality
    if not (query and semantic):
        for doc in results:
            doc["_score"] = 1.0

    return results[:k]


def list_taxonomy_tags(db: Any = None) -> list[dict]:
    """List all taxonomy tags with skill counts.

    Returns:
        List of {tag, count} dicts sorted by count descending.
    """
    if db is None:
        db = get_db()

    if not db.has_collection(EDGES_COLLECTION):
        return []

    aql = """
        FOR e IN @@col
            FILTER e.edge_type == 'TAXONOMY'
            COLLECT tag = e.tag WITH COUNT INTO cnt
            SORT cnt DESC
            RETURN {tag: tag, count: cnt}
    """
    try:
        cursor = db.aql.execute(aql, bind_vars={"@col": EDGES_COLLECTION})
        return list(cursor)
    except Exception as exc:
        logger.error("Taxonomy tag list failed: {}", exc)
        return []


def get_skill_graph(skill_name: str, depth: int = 1, db: Any = None) -> dict:
    """Get the composition and taxonomy graph around a skill.

    Args:
        skill_name: Name of the skill to start from.
        depth: How many hops to traverse (1-3).
        db: Database handle.

    Returns:
        Dict with 'skill', 'composes' (outbound), 'composed_by' (inbound),
        'taxonomy' tags, and 'related' skills sharing taxonomy.
    """
    if db is None:
        db = get_db()

    col = db.collection(SKILLS_COLLECTION)
    skill_key = f"skill_{skill_name}"

    try:
        skill_doc = col.get(skill_key)
    except Exception as exc:
        skill_doc = None

    if not skill_doc:
        return {"error": f"Skill '{skill_name}' not found"}

    result = {
        "skill": {
            "name": skill_doc["name"],
            "description": skill_doc.get("description", ""),
            "triggers": skill_doc.get("triggers", []),
            "provides": skill_doc.get("provides", []),
        },
        "composes": [],
        "composed_by": [],
        "taxonomy": [],
        "related": [],
    }

    # Outbound COMPOSES: what this skill uses
    try:
        cursor = db.aql.execute(
            "FOR e IN @@col FILTER e._from == @sid AND e.edge_type == 'COMPOSES' RETURN e.target_skill",
            bind_vars={"@col": EDGES_COLLECTION, "sid": f"{SKILLS_COLLECTION}/{skill_key}"},
        )
        result["composes"] = list(cursor)
    except Exception as exc:  # was bare
        pass

    # Inbound COMPOSES: what uses this skill
    try:
        cursor = db.aql.execute(
            "FOR e IN @@col FILTER e._to == @sid AND e.edge_type == 'COMPOSES' RETURN e.source_skill",
            bind_vars={"@col": EDGES_COLLECTION, "sid": f"{SKILLS_COLLECTION}/{skill_key}"},
        )
        result["composed_by"] = list(cursor)
    except Exception as exc:  # was bare
        pass

    # Taxonomy tags
    try:
        cursor = db.aql.execute(
            "FOR e IN @@col FILTER e._from == @sid AND e.edge_type == 'TAXONOMY' RETURN e.tag",
            bind_vars={"@col": EDGES_COLLECTION, "sid": f"{SKILLS_COLLECTION}/{skill_key}"},
        )
        result["taxonomy"] = list(cursor)
    except Exception as exc:  # was bare
        pass

    # Related skills: share at least one taxonomy tag (depth=1 hop via tag)
    if result["taxonomy"] and depth >= 1:
        try:
            cursor = db.aql.execute(
                """FOR tag IN @tags
                    FOR e IN @@col
                        FILTER e.edge_type == 'TAXONOMY' AND e.tag == tag AND e.source_skill != @name
                        COLLECT skill = e.source_skill
                        RETURN skill""",
                bind_vars={
                    "@col": EDGES_COLLECTION,
                    "tags": result["taxonomy"],
                    "name": skill_name,
                },
            )
            result["related"] = list(cursor)
        except Exception as exc:  # was bare
            pass

    return result
