"""Schema setup: collection creation and Theory-of-Mind indices."""
from __future__ import annotations

from loguru import logger


def ensure_collections(db) -> None:
    """Create all required collections (idempotent)."""
    if not db.has_collection("lessons"):
        db.create_collection("lessons")
    # New memory types
    if not db.has_collection("entities"):
        db.create_collection("entities")
    # facts collection removed - unused
    if not db.has_collection("episodes"):
        db.create_collection("episodes")
    # Per-episode step logs (optional, for GraphWorld runs)
    if not db.has_collection("episode_steps"):
        db.create_collection("episode_steps")
    if not db.has_collection("reranker_cache"):
        db.create_collection("reranker_cache")
    if not db.has_collection("relation_class_cache"):
        db.create_collection("relation_class_cache")
    # research_jobs collection removed - unused
    if not db.has_collection("memory_events"):
        db.create_collection("memory_events")
    # incidents collection removed - unused
    if not db.has_collection("lesson_edges"):
        db.create_collection("lesson_edges", edge=True)
    if not db.has_collection("rejected_pairs"):
        db.create_collection("rejected_pairs")
    # Agent-to-agent requests inbox/outbox
    if not db.has_collection("agent_requests"):
        db.create_collection("agent_requests")
    # Agent-to-agent conversations (broadcast + direct)
    if not db.has_collection("agent_conversations"):
        db.create_collection("agent_conversations")
    # issues and issue_edges collections removed - routing system never completed
    # Revision history collections (append-only snapshots)
    if not db.has_collection("edge_revisions"):
        db.create_collection("edge_revisions")
    if not db.has_collection("lesson_revisions"):
        db.create_collection("lesson_revisions")
    if not db.has_collection("lesson_embeddings"):
        db.create_collection("lesson_embeddings")
    # Anchor score cache (TTL)
    if not db.has_collection("anchor_score_cache"):
        db.create_collection("anchor_score_cache")
    # QA cache (LLM pairs)
    if not db.has_collection("qa_cache"):
        db.create_collection("qa_cache")
    # Persistent Q/A pairs store (operator-reviewed or auto-generated)
    # Some code paths (e.g., dataset ingest) create this lazily; create proactively to avoid 404s
    try:
        if not db.has_collection("qa_pairs"):
            db.create_collection("qa_pairs")
    except Exception as exc:
        logger.error("qa_pairs collection setup skipped: {}", exc)
        # best-effort; older Arango drivers may raise on has_collection
        try:
            db.create_collection("qa_pairs")
        except Exception as exc:
            logger.error("collection 'qa_pairs' setup skipped: {}", exc)
    # Optional edge annotations (shadow writes; operator review)
    if not db.has_collection("edge_annotations"):
        db.create_collection("edge_annotations")
    # Optional large text store for extracted sources (kept separate from lessons)
    if not db.has_collection("lesson_texts"):
        db.create_collection("lesson_texts")
    # youtube_transcripts collection removed - feature never completed
    if not db.has_collection("symbol_snapshots"):
        db.create_collection("symbol_snapshots")
    # Gold pairs: query -> expected lesson ids (per scope)
    if not db.has_collection("gold_pairs"):
        db.create_collection("gold_pairs")
    # Proof jobs queue (for Lean4 proof attempts)
    if not db.has_collection("proof_jobs"):
        db.create_collection("proof_jobs")
    # Lean4 proofs (links back to episodes/lessons via source_id)
    if not db.has_collection("proofs"):
        db.create_collection("proofs")
    # Task states for plan_state retrieval (latent reasoning / anti-repeat)
    if not db.has_collection("task_states"):
        db.create_collection("task_states")
    if not db.has_collection("nightly_assessments"):
        db.create_collection("nightly_assessments")

    # Checkpoints: session state snapshots (separate from lessons — different lifecycle)
    if not db.has_collection("checkpoints"):
        db.create_collection("checkpoints")

    # SPARTA metrics bridge (DuckDB -> ArangoDB for lock-free reads)
    if not db.has_collection("sparta_metrics"):
        db.create_collection("sparta_metrics")

    # SPARTA QRA storage (replaces DuckDB qra table for lock-free access)
    if not db.has_collection("sparta_qra"):
        db.create_collection("sparta_qra")
    # SPARTA QRA candidates (WARN-grade awaiting human review)
    if not db.has_collection("sparta_qra_candidates"):
        db.create_collection("sparta_qra_candidates")
    # Rejected QRAs (FAIL-grade, kept for GRPO training data)
    if not db.has_collection("rejected_qras"):
        db.create_collection("rejected_qras")
    # SPARTA QRA embeddings (dense vectors for hybrid search)
    if not db.has_collection("sparta_qra_embeddings"):
        db.create_collection("sparta_qra_embeddings")
    # SPARTA QRA edges (cross-persona graph relationships)
    if not db.has_collection("sparta_qra_edges"):
        db.create_collection("sparta_qra_edges", edge=True)
    # Skill description embeddings (dense vectors for capability overlap detection)
    if not db.has_collection("skill_description_embeddings"):
        db.create_collection("skill_description_embeddings")

    # Skill chains (closed-loop composition learning)
    if not db.has_collection("skill_chains"):
        db.create_collection("skill_chains")

    # Project knowledge (shared human/agent context per project)
    if not db.has_collection("project_knowledge"):
        db.create_collection("project_knowledge")

    # SPARTA pipeline data collections (replaces DuckDB tables for lock-free access)
    for _sparta_coll in [
        "sparta_controls", "sparta_urls", "sparta_control_urls",
        "sparta_url_content", "sparta_url_knowledge",
        "sparta_knowledge_anchors", "sparta_relationships",
        "sparta_url_extraction_log", "sparta_qra_gen_log",
        "sparta_brandon_reviews",
    ]:
        if not db.has_collection(_sparta_coll):
            db.create_collection(_sparta_coll)

    # Taxonomy vocabulary convergence (dynamic vocabulary for QRA assessor)
    if not db.has_collection("taxonomy_vocabulary"):
        db.create_collection("taxonomy_vocabulary")
    if not db.has_collection("taxonomy_vocabulary_proposals"):
        db.create_collection("taxonomy_vocabulary_proposals")

    # Domain terms: known non-control terms, framework names, OOD markers, keywords
    # Replaces hardcoded frozensets/dicts in Python -- ArangoDB is the source of truth.
    if not db.has_collection("domain_terms"):
        db.create_collection("domain_terms")

    # Session summaries: LLM-assessed session analysis for nightly reflection
    if not db.has_collection("session_summaries"):
        db.create_collection("session_summaries")

    # Skill descriptions (parsed from SKILL.md frontmatter for capability recall)
    if not db.has_collection("skill_descriptions"):
        db.create_collection("skill_descriptions")
    # Skill edges (COMPOSES and TAXONOMY relationships between skills)
    if not db.has_collection("skill_edges"):
        db.create_collection("skill_edges", edge=True)

    # Curate pipeline collections (code/doc/pdf extraction)
    if not db.has_collection("code_files"):
        db.create_collection("code_files")
    if not db.has_collection("code_symbols"):
        db.create_collection("code_symbols")
    if not db.has_collection("doc_chunks"):
        db.create_collection("doc_chunks")
    if not db.has_collection("pdf_refs"):
        db.create_collection("pdf_refs")
    if not db.has_collection("pdf_docs"):
        db.create_collection("pdf_docs")
    if not db.has_collection("equation_candidates"):
        db.create_collection("equation_candidates")
    if not db.has_collection("lean_candidates"):
        db.create_collection("lean_candidates")
    if not db.has_collection("lean_theorems"):
        db.create_collection("lean_theorems")
    # Lean4 autoformalization pairs (English<->Lean4 for lab training)
    if not db.has_collection("lean4_autoformalization"):
        db.create_collection("lean4_autoformalization")
    # curate_edges collection removed - unused

    # Sanity scripts: executable code examples for agent reference
    if not db.has_collection("sanity_scripts"):
        db.create_collection("sanity_scripts")
    # Embeddings for sanity scripts (semantic search)
    if not db.has_collection("script_embeddings"):
        db.create_collection("script_embeddings")

    # -------------------------------------------------------------------------
    # Theory of Mind (ToM) collections for persona agents
    # -------------------------------------------------------------------------
    # Users: profiles of humans the agent interacts with
    if not db.has_collection("users"):
        db.create_collection("users")

    # Persona states: psychological state of agents (drives, mood, defense mechanisms)
    if not db.has_collection("persona_states"):
        db.create_collection("persona_states")

    # Persona state history: time-series snapshots for trend analysis
    if not db.has_collection("persona_state_history"):
        db.create_collection("persona_state_history")
        # Add TTL index (30 days = 2592000 seconds)
        try:
            coll = db.collection("persona_state_history")
            coll.add_ttl_index(fields=["timestamp"], expiry_time=2592000)
        except Exception as exc:
            # TTL index may already exist or not be supported
            logger.error("collection 'persona_state_history' access skipped: {}", exc)

    # User-agent relationships: edge collection tracking trust/respect evolution
    if not db.has_collection("user_agent_relationships"):
        db.create_collection("user_agent_relationships", edge=True)

    # User lessons: insights Horus has learned about specific users
    if not db.has_collection("user_lessons"):
        db.create_collection("user_lessons")

    # ToM edges: connections between users, lessons, and persona states
    if not db.has_collection("tom_edges"):
        db.create_collection("tom_edges", edge=True)

    # -------------------------------------------------------------------------
    # Steering: Per-user conversation steering priors
    # -------------------------------------------------------------------------
    if not db.has_collection("steering_priors"):
        db.create_collection("steering_priors")

    if not db.has_collection("user_steering"):
        db.create_collection("user_steering", edge=True)

    # -------------------------------------------------------------------------
    # Control extraction pipeline: chunk->control, requirement->control, proof->requirement edges
    # -------------------------------------------------------------------------
    if not db.has_collection("chunk_control_edges"):
        db.create_collection("chunk_control_edges", edge=True)
    if not db.has_collection("requirement_control_edges"):
        db.create_collection("requirement_control_edges", edge=True)
    if not db.has_collection("proof_requirement_edges"):
        db.create_collection("proof_requirement_edges", edge=True)

    # -------------------------------------------------------------------------
    # Binary Explorer: ELF feature graph for /analyze-elf
    # Nodes: RPC methods, events, schemas, state machines, CLI commands,
    #        namespaces, parameters (from treesitter + Zod schema fields)
    # Edges: contains, payload, emits, triggers, has_parameter
    # -------------------------------------------------------------------------
    if not db.has_collection("binary_features"):
        db.create_collection("binary_features")
    if not db.has_collection("binary_feature_edges"):
        db.create_collection("binary_feature_edges", edge=True)

    # -------------------------------------------------------------------------
    # TODOs - Actionable items separate from lessons (not polluting recall)
    # -------------------------------------------------------------------------
    if not db.has_collection("todos"):
        db.create_collection("todos")

    # Trace cache: provenance trace results with TTL
    if not db.has_collection("trace_cache"):
        db.create_collection("trace_cache")

    # -------------------------------------------------------------------------
    # QuerySpec: voice/NL → deterministic UI action pipeline
    # Actions registered by React elements (_key = DOM element id)
    # Training pairs labeled by Opus for eventual 32B model shadow
    # /taxonomy Intent tags create edges in app_action_edges for multi-hop
    # -------------------------------------------------------------------------
    if not db.has_collection("app_actions"):
        db.create_collection("app_actions")
    if not db.has_collection("app_action_edges"):
        db.create_collection("app_action_edges", edge=True)


def ensure_tom_indices(db) -> None:
    """Create Theory-of-Mind and steering indices (best-effort)."""
    try:
        # Users: hash index on user_id for fast lookup
        users_coll = db.collection("users")
        existing_indices = [idx.get("fields", []) for idx in users_coll.indexes()]
        if ["user_id"] not in existing_indices:
            users_coll.add_hash_index(fields=["user_id"], unique=True)
    except Exception as exc:
        # Index may already exist
        logger.error("collection 'users' access skipped: {}", exc)

    try:
        # Persona states: hash index on agent_id for fast lookup
        persona_coll = db.collection("persona_states")
        existing_indices = [idx.get("fields", []) for idx in persona_coll.indexes()]
        if ["agent_id"] not in existing_indices:
            persona_coll.add_hash_index(fields=["agent_id"], unique=True)
    except Exception as exc:
        # Index may already exist
        logger.error("collection 'persona_states' access skipped: {}", exc)

    try:
        # Relationships: hash index on user_id + agent_id for fast lookup
        rel_coll = db.collection("user_agent_relationships")
        existing_indices = [idx.get("fields", []) for idx in rel_coll.indexes()]
        if ["user_id", "agent_id"] not in existing_indices:
            rel_coll.add_hash_index(fields=["user_id", "agent_id"], unique=True)
    except Exception as exc:
        # Index may already exist
        logger.error("collection 'user_agent_relationships' access skipped: {}", exc)

    try:
        # User lessons: index on user_id/agent_id and category
        lessons_coll = db.collection("user_lessons")
        existing_indices = [idx.get("fields", []) for idx in lessons_coll.indexes()]
        if ["user_id", "agent_id"] not in existing_indices:
            lessons_coll.add_hash_index(fields=["user_id", "agent_id"], unique=False)
        if ["category"] not in existing_indices:
            lessons_coll.add_hash_index(fields=["category"], unique=False)
    except Exception as exc:
        logger.error("collection 'user_lessons' access skipped: {}", exc)

    try:
        # ToM edges: standard edge indices
        tom_coll = db.collection("tom_edges")
        existing_indices = [idx.get("fields", []) for idx in tom_coll.indexes()]
        if ["agent_id"] not in existing_indices:
            tom_coll.add_hash_index(fields=["agent_id"], unique=False)
        if ["type"] not in existing_indices:
            tom_coll.add_hash_index(fields=["type"], unique=False)
    except Exception as exc:
        logger.error("collection 'tom_edges' access skipped: {}", exc)

    try:
        # Steering priors: index on user_id for fast lookup
        sp_coll = db.collection("steering_priors")
        existing_indices = [idx.get("fields", []) for idx in sp_coll.indexes()]
        if ["user_id"] not in existing_indices:
            sp_coll.add_hash_index(fields=["user_id"], unique=True)
    except Exception as exc:
        logger.error("collection 'steering_priors' access skipped: {}", exc)

    try:
        # app_actions: indices for app scope and action type filtering
        aa_coll = db.collection("app_actions")
        existing_indices = [idx.get("fields", []) for idx in aa_coll.indexes()]
        if ["app"] not in existing_indices:
            aa_coll.add_hash_index(fields=["app"], unique=False)
        if ["action"] not in existing_indices:
            aa_coll.add_hash_index(fields=["action"], unique=False)
        if ["doc_type"] not in existing_indices:
            aa_coll.add_hash_index(fields=["doc_type"], unique=False)
    except Exception as exc:
        logger.error("collection 'app_actions' index setup skipped: {}", exc)

    try:
        # app_action_edges: intent-based edges for multi-hop traversal
        aae_coll = db.collection("app_action_edges")
        existing_indices = [idx.get("fields", []) for idx in aae_coll.indexes()]
        if ["intent_tag"] not in existing_indices:
            aae_coll.add_hash_index(fields=["intent_tag"], unique=False)
    except Exception as exc:
        logger.error("collection 'app_action_edges' index setup skipped: {}", exc)

    try:
        # TODOs: indices for filtering and lookup
        todos_coll = db.collection("todos")
        existing_indices = [idx.get("fields", []) for idx in todos_coll.indexes()]
        if ["status"] not in existing_indices:
            todos_coll.add_hash_index(fields=["status"], unique=False)
        if ["scope"] not in existing_indices:
            todos_coll.add_hash_index(fields=["scope"], unique=False)
        if ["priority"] not in existing_indices:
            todos_coll.add_hash_index(fields=["priority"], unique=False)
    except Exception as exc:
        logger.error("collection 'todos' access skipped: {}", exc)
