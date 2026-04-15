"""Schema setup: ArangoSearch view definitions (BM25)."""
from __future__ import annotations

from loguru import logger


def ensure_views(db) -> None:
    """Create or update all ArangoSearch views (idempotent, best-effort)."""
    try:
        existing_views = [v.get("name") for v in db.views()]
    except Exception as exc:
        logger.error("listing views failed: {}", exc)
        existing_views = []

    _ensure_curate_views(db, existing_views)
    _ensure_domain_views(db, existing_views)
    _ensure_unified_search(db, existing_views)
    _ensure_lessons_views(db, existing_views)
    _ensure_binary_features_view(db, existing_views)
    _ensure_llm_call_log_view(db, existing_views)


def _ensure_curate_views(db, existing_views: list) -> None:
    """Views for curate pipeline collections."""
    # code_symbols_search view
    if "code_symbols_search" not in existing_views and db.has_collection("code_symbols"):
        try:
            db.create_arangosearch_view(
                "code_symbols_search",
                properties={
                    "links": {
                        "code_symbols": {
                            "includeAllFields": False,
                            "analyzers": ["text_en"],
                            "fields": {
                                "name": {"analyzers": ["text_en", "identity"]},
                                "docstring": {"analyzers": ["text_en"]},
                                "kind": {"analyzers": ["identity"]},
                                "file_path": {"analyzers": ["identity"]},
                                "scope": {"analyzers": ["identity"]},
                            },
                        }
                    }
                },
            )
        except Exception as exc:
            logger.error("view 'code_symbols_search' creation skipped: {}", exc)

    # doc_chunks_search view
    if "doc_chunks_search" not in existing_views and db.has_collection("doc_chunks"):
        try:
            db.create_arangosearch_view(
                "doc_chunks_search",
                properties={
                    "links": {
                        "doc_chunks": {
                            "includeAllFields": False,
                            "analyzers": ["text_en"],
                            "fields": {
                                "content": {"analyzers": ["text_en"]},  # P2 ingest uses 'content'
                                "source_path": {"analyzers": ["identity"]},  # P2 uses 'source_path'
                                "scope": {"analyzers": ["identity"]},
                            },
                        }
                    }
                },
            )
        except Exception as exc:
            logger.error("view 'doc_chunks_search' creation skipped: {}", exc)

    # lean_theorems_search view
    if "lean_theorems_search" not in existing_views and db.has_collection("lean_theorems"):
        try:
            db.create_arangosearch_view(
                "lean_theorems_search",
                properties={
                    "links": {
                        "lean_theorems": {
                            "includeAllFields": False,
                            "analyzers": ["text_en"],
                            "fields": {
                                "lean_code": {"analyzers": ["text_en"]},
                                "notes": {"analyzers": ["text_en"]},
                                "status": {"analyzers": ["identity"]},
                                "scope": {"analyzers": ["identity"]},
                            },
                        }
                    }
                },
            )
        except Exception as exc:
            logger.error("view 'lean_theorems_search' creation skipped: {}", exc)

    # lean4_autoformalization_search view (English<->Lean4 pairs for lab)
    if "lean4_autoformalization_search" not in existing_views and db.has_collection("lean4_autoformalization"):
        try:
            db.create_arangosearch_view(
                "lean4_autoformalization_search",
                properties={
                    "links": {
                        "lean4_autoformalization": {
                            "includeAllFields": False,
                            "analyzers": ["text_en"],
                            "fields": {
                                "english": {"analyzers": ["text_en"]},
                                "lean4_statement": {"analyzers": ["text_en"]},
                                "lean4_proof": {"analyzers": ["text_en"]},
                                "source": {"analyzers": ["identity"]},
                                "verified": {"analyzers": ["identity"]},
                                "tactics": {"analyzers": ["identity"]},
                            },
                        }
                    }
                },
            )
        except Exception as exc:
            logger.error("view 'lean4_autoformalization_search' creation skipped: {}", exc)

    # lean4_proofs_search view (DeepSeek Prover V2 dataset for theorem proving)
    if "lean4_proofs_search" not in existing_views and db.has_collection("lean4_proofs"):
        try:
            db.create_arangosearch_view(
                "lean4_proofs_search",
                properties={
                    "links": {
                        "lean4_proofs": {
                            "includeAllFields": False,
                            "analyzers": ["text_en"],
                            "fields": {
                                "theorem_name": {"analyzers": ["text_en", "identity"]},
                                "problem_description": {"analyzers": ["text_en"]},
                                "lean_code": {"analyzers": ["text_en"]},
                                "tactics": {"analyzers": ["identity"]},
                                "imports": {"analyzers": ["identity"]},
                            },
                        }
                    }
                },
            )
        except Exception as exc:
            logger.error("view 'lean4_proofs_search' creation skipped: {}", exc)


def _ensure_domain_views(db, existing_views: list) -> None:
    """Views for domain-specific collections (SPARTA, agents, tasks, etc.)."""
    # task_states_search view (for plan_state / latent reasoning recall)
    if "task_states_search" not in existing_views and db.has_collection("task_states"):
        try:
            db.create_arangosearch_view(
                "task_states_search",
                properties={
                    "links": {
                        "task_states": {
                            "includeAllFields": False,
                            "analyzers": ["text_en"],
                            "fields": {
                                "goal": {"analyzers": ["text_en"]},
                                "output": {"analyzers": ["text_en"]},
                                "agent": {"analyzers": ["identity"]},
                                "status": {"analyzers": ["identity"]},
                                "task_key": {"analyzers": ["identity"]},
                                "scope": {"analyzers": ["identity"]},
                            },
                        }
                    }
                },
            )
        except Exception as exc:
            logger.error("view 'task_states_search' creation skipped: {}", exc)

    # agent_conversations_search view
    if "agent_conversations_search" not in existing_views and db.has_collection("agent_conversations"):
        try:
            db.create_arangosearch_view(
                "agent_conversations_search",
                properties={
                    "links": {
                        "agent_conversations": {
                            "includeAllFields": False,
                            "analyzers": ["text_en"],
                            "fields": {
                                "body": {"analyzers": ["text_en"]},
                                "category": {"analyzers": ["text_en", "identity"]},
                                "session_id": {"analyzers": ["identity"]},
                                "id_from": {"analyzers": ["identity"]},
                                "id_to": {"analyzers": ["identity"]},
                                "type": {"analyzers": ["identity"]},
                            },
                        }
                    }
                },
            )
        except Exception as exc:
            logger.error("view 'agent_conversations_search' creation skipped: {}", exc)

    # sanity_scripts_search view (executable code examples for agent reference)
    if "sanity_scripts_search" not in existing_views and db.has_collection("sanity_scripts"):
        try:
            db.create_arangosearch_view(
                "sanity_scripts_search",
                properties={
                    "links": {
                        "sanity_scripts": {
                            "includeAllFields": False,
                            "analyzers": ["text_en"],
                            "fields": {
                                "name": {"analyzers": ["text_en"]},
                                "request": {"analyzers": ["text_en"]},
                                "description": {"analyzers": ["text_en"]},
                                "tags": {"analyzers": ["text_en", "identity"]},
                                "symbol_names": {"analyzers": ["text_en", "identity"]},
                                "language": {"analyzers": ["identity"]},
                                "scope": {"analyzers": ["identity"]},
                                "status": {"analyzers": ["identity"]},
                            },
                        }
                    }
                },
            )
        except Exception as exc:
            logger.error("view 'sanity_scripts_search' creation skipped: {}", exc)

    # skill_descriptions_search view (capability overlap detection for anti-silo)
    if "skill_descriptions_search" not in existing_views and db.has_collection("skill_descriptions"):
        try:
            db.create_arangosearch_view(
                "skill_descriptions_search",
                properties={
                    "links": {
                        "skill_descriptions": {
                            "includeAllFields": False,
                            "analyzers": ["text_en"],
                            "fields": {
                                "name": {"analyzers": ["text_en", "identity"]},
                                "description": {"analyzers": ["text_en"]},
                                "provides": {"analyzers": ["text_en", "identity"]},
                                "composes": {"analyzers": ["text_en", "identity"]},
                                "triggers": {"analyzers": ["text_en"]},
                                "taxonomy": {"analyzers": ["text_en", "identity"]},
                                "body": {"analyzers": ["text_en"]},
                            },
                        }
                    }
                },
            )
        except Exception as exc:
            logger.error("view 'skill_descriptions_search' creation skipped: {}", exc)

    _ensure_aerospace_terms_view(db, existing_views)
    _ensure_sparta_views(db, existing_views)
    _ensure_qa_and_tom_views(db, existing_views)


def _ensure_aerospace_terms_view(db, existing_views: list) -> None:
    """Aerospace/NIST/MITRE vocabulary view for entity grounding.

    Standalone view (not in sparta_unified_search) because this is a
    glossary/vocabulary collection for entity grounding, not security
    controls/QRAs.  Mixing them would pollute BM25 scores.
    """
    view_name = "aerospace_terms_search"
    _links = {
        "aerospace_terms": {
            "includeAllFields": False,
            "analyzers": ["text_en", "identity"],
            "fields": {
                "term": {},  # inherits both analyzers
                "definition": {"analyzers": ["text_en"]},
                "full_name": {"analyzers": ["text_en"]},
                "domain": {"analyzers": ["identity"]},
                "category": {"analyzers": ["identity"]},
                "source": {"analyzers": ["identity"]},
                "in_sparta_corpus": {},
            },
        }
    }
    if not db.has_collection("aerospace_terms"):
        return
    if view_name not in existing_views:
        try:
            db.create_arangosearch_view(view_name, properties={"links": _links})
        except Exception as exc:
            logger.error("view '{}' creation skipped: {}", view_name, exc)
    else:
        try:
            db.update_arangosearch_view(view_name, properties={"links": _links})
        except Exception as exc:
            logger.error("view '{}' update skipped: {}", view_name, exc)


def _ensure_sparta_views(db, existing_views: list) -> None:
    """SPARTA-specific ArangoSearch views."""
    _sparta_qra_fields = {
        "question": {"analyzers": ["text_en"]},
        "answer": {"analyzers": ["text_en"]},
        "reasoning": {"analyzers": ["text_en"]},
        "control_id": {"analyzers": ["identity"]},
        "reasoning_grade": {"analyzers": ["identity"]},
        "domain": {"analyzers": ["identity"]},
        "persona_scope": {"analyzers": ["identity"]},
        "scope": {"analyzers": ["identity"]},
        # Wave 4 Heart/Mind migration — tactical tags on sparta_qra
        # tactical_tags kept for compat window (Wave 4 Task 15 removes it)
        "tactical_tags": {"analyzers": ["identity"]},
        "mind": {"analyzers": ["identity"]},
    }

    # sparta_unified_search view — spans sparta_qra (218K), sparta_controls (10.5K),
    # and sparta_url_knowledge (42K).  Replaces the old per-collection views
    # (sparta_qra_search, sparta_controls_search).
    _unified_links: dict = {}
    if db.has_collection("sparta_qra"):
        _unified_links["sparta_qra"] = {
            "includeAllFields": False,
            "analyzers": ["text_en"],
            "fields": _sparta_qra_fields,
        }
    if db.has_collection("sparta_controls"):
        _unified_links["sparta_controls"] = {
            "includeAllFields": False,
            "analyzers": ["text_en"],
            "fields": {
                "name": {"analyzers": ["text_en"]},
                "description": {"analyzers": ["text_en"]},
                "control_id": {"analyzers": ["identity"]},
                "control_type": {"analyzers": ["identity"]},
                "source_framework": {"analyzers": ["identity"]},
                "parent_id": {"analyzers": ["identity"]},
                "domain": {"analyzers": ["identity"]},
            },
        }
    if db.has_collection("sparta_url_knowledge"):
        _unified_links["sparta_url_knowledge"] = {
            "includeAllFields": False,
            "analyzers": ["text_en"],
            "fields": {
                "text": {"analyzers": ["text_en"]},
                "topic": {"analyzers": ["text_en"]},
                "name": {"analyzers": ["text_en"]},
                "description": {"analyzers": ["text_en"]},
                "control_id": {"analyzers": ["identity"]},
                "source_framework": {"analyzers": ["identity"]},
                "url_id": {"analyzers": ["identity"]},
                "domain": {"analyzers": ["identity"]},
                "scope": {"analyzers": ["identity"]},
            },
        }
    if db.has_collection("controls"):
        _unified_links["controls"] = {
            "includeAllFields": False,
            "analyzers": ["text_en"],
            "fields": {
                "title": {"analyzers": ["text_en"]},
                "definition": {"analyzers": ["text_en"]},
                "control_id": {"analyzers": ["identity"]},
                "category": {"analyzers": ["identity"]},
            },
        }

    if _unified_links:
        if "sparta_unified_search" not in existing_views:
            try:
                db.create_arangosearch_view(
                    "sparta_unified_search",
                    properties={"links": _unified_links},
                )
            except Exception as exc:
                logger.error("view 'sparta_unified_search' creation skipped: {}", exc)
        else:
            try:
                db.update_arangosearch_view(
                    "sparta_unified_search",
                    properties={"links": _unified_links},
                )
            except Exception as exc:
                logger.error("view 'sparta_unified_search' update skipped: {}", exc)

    # sparta_relationships_search view (cross-framework control relationships for disambiguation)
    if "sparta_relationships_search" not in existing_views and db.has_collection("sparta_relationships"):
        try:
            db.create_arangosearch_view(
                "sparta_relationships_search",
                properties={
                    "links": {
                        "sparta_relationships": {
                            "includeAllFields": False,
                            "analyzers": ["identity"],
                            "fields": {
                                "source_control_id": {"analyzers": ["identity"]},
                                "target_control_id": {"analyzers": ["identity"]},
                                "method": {"analyzers": ["identity"]},
                            },
                        }
                    }
                },
            )
        except Exception as exc:
            logger.error("view 'sparta_relationships_search' creation skipped: {}", exc)

    # chunk_control_edges_search view (chunk->control edge traversal for /memory recall)
    if "chunk_control_edges_search" not in existing_views and db.has_collection("chunk_control_edges"):
        try:
            db.create_arangosearch_view(
                "chunk_control_edges_search",
                properties={
                    "links": {
                        "chunk_control_edges": {
                            "includeAllFields": False,
                            "analyzers": ["text_en"],
                            "fields": {
                                "control_id": {"analyzers": ["identity"]},
                                "framework": {"analyzers": ["identity"]},
                                "edge_type": {"analyzers": ["identity"]},
                                "context_window": {"analyzers": ["text_en"]},
                                "extraction_method": {"analyzers": ["identity"]},
                            },
                        }
                    }
                },
            )
        except Exception as exc:
            logger.error("view 'chunk_control_edges_search' creation skipped: {}", exc)


def _ensure_qa_and_tom_views(db, existing_views: list) -> None:
    """Views for QA pairs, TODOs, Theory of Mind, and session summaries."""
    # qa_pairs_search view (QRA training pairs from LLM extraction)
    if "qa_pairs_search" not in existing_views and db.has_collection("qa_pairs"):
        try:
            db.create_arangosearch_view(
                "qa_pairs_search",
                properties={
                    "links": {
                        "qa_pairs": {
                            "includeAllFields": False,
                            "analyzers": ["text_en"],
                            "fields": {
                                "question": {"analyzers": ["text_en"]},
                                "reasoning": {"analyzers": ["text_en"]},
                                "answer": {"analyzers": ["text_en"]},
                                "source_title": {"analyzers": ["text_en"]},
                                "source_id": {"analyzers": ["identity"]},
                                "source_db": {"analyzers": ["identity"]},
                                "scope": {"analyzers": ["identity"]},
                                "tags": {"analyzers": ["text_en", "identity"]},
                                "extractor": {"analyzers": ["identity"]},
                                # Edge provenance fields
                                "edge_id": {"analyzers": ["identity"]},
                                "edge_type": {"analyzers": ["identity"]},
                                "llm_model": {"analyzers": ["identity"]},
                            },
                        }
                    }
                },
            )
        except Exception as exc:
            logger.error("view 'qa_pairs_search' creation skipped: {}", exc)

    # todos_search view (actionable items separate from lessons)
    if "todos_search" not in existing_views and db.has_collection("todos"):
        try:
            db.create_arangosearch_view(
                "todos_search",
                properties={
                    "links": {
                        "todos": {
                            "includeAllFields": False,
                            "analyzers": ["text_en"],
                            "fields": {
                                "title": {"analyzers": ["text_en"]},
                                "description": {"analyzers": ["text_en"]},
                                "scope": {"analyzers": ["identity"]},
                                "status": {"analyzers": ["identity"]},
                                "priority": {"analyzers": ["identity"]},
                                "tags": {"analyzers": ["text_en", "identity"]},
                            },
                        }
                    }
                },
            )
        except Exception as exc:
            logger.error("view 'todos_search' creation skipped: {}", exc)

    # users_search view (Theory of Mind - user profile lookup)
    if "users_search" not in existing_views and db.has_collection("users"):
        try:
            db.create_arangosearch_view(
                "users_search",
                properties={
                    "links": {
                        "users": {
                            "includeAllFields": False,
                            "analyzers": ["text_en"],
                            "fields": {
                                "user_id": {"analyzers": ["identity"]},
                                "display_name": {"analyzers": ["text_en"]},
                                "topics": {"analyzers": ["text_en", "identity"]},
                                "skill_level": {"analyzers": ["identity"]},
                                "scope": {"analyzers": ["identity"]},
                                "notes": {"analyzers": ["text_en"]},
                            },
                        }
                    }
                },
            )
        except Exception as exc:
            logger.error("view 'users_search' creation skipped: {}", exc)

    # user_lessons_search view (Theory of Mind - learned insights lookup)
    if "user_lessons_search" not in existing_views and db.has_collection("user_lessons"):
        try:
            db.create_arangosearch_view(
                "user_lessons_search",
                properties={
                    "links": {
                        "user_lessons": {
                            "includeAllFields": False,
                            "analyzers": ["text_en"],
                            "fields": {
                                "user_id": {"analyzers": ["identity"]},
                                "agent_id": {"analyzers": ["identity"]},
                                "lesson": {"analyzers": ["text_en"]},
                                "category": {"analyzers": ["identity"]},
                            },
                        }
                    }
                },
            )
        except Exception as exc:
            logger.error("view 'user_lessons_search' creation skipped: {}", exc)

    # session_summaries_search view (LLM-assessed session analysis for recall)
    if "session_summaries_search" not in existing_views and db.has_collection("session_summaries"):
        try:
            db.create_arangosearch_view(
                "session_summaries_search",
                properties={
                    "links": {
                        "session_summaries": {
                            "includeAllFields": False,
                            "analyzers": ["text_en"],
                            "fields": {
                                "assessment": {
                                    "fields": {
                                        "problems_addressed": {"analyzers": ["text_en"]},
                                        "solutions_found": {"analyzers": ["text_en"]},
                                        "key_learnings": {"analyzers": ["text_en"]},
                                        "unresolved": {"analyzers": ["text_en"]},
                                    }
                                },
                                "session_id": {"analyzers": ["identity"]},
                                "source_name": {"analyzers": ["identity"]},
                                "assessment.resolution_status": {"analyzers": ["identity"]},
                            },
                        }
                    }
                },
            )
        except Exception as exc:
            logger.error("view 'session_summaries_search' creation skipped: {}", exc)

    # app_actions_search view (voice→QuerySpec: action registry + training pairs)
    if "app_actions_search" not in existing_views and db.has_collection("app_actions"):
        try:
            db.create_arangosearch_view(
                "app_actions_search",
                properties={
                    "links": {
                        "app_actions": {
                            "includeAllFields": False,
                            "analyzers": ["text_en"],
                            "fields": {
                                "problem": {"analyzers": ["text_en"]},
                                "solution": {"analyzers": ["text_en"]},
                                "label": {"analyzers": ["text_en"]},
                                "description": {"analyzers": ["text_en"]},
                                "action": {"analyzers": ["identity"]},
                                "app": {"analyzers": ["identity"]},
                                "doc_type": {"analyzers": ["identity"]},
                                "tags": {"analyzers": ["text_en", "identity"]},
                                "scope": {"analyzers": ["identity"]},
                            },
                        }
                    }
                },
            )
        except Exception as exc:
            logger.error("view 'app_actions_search' creation skipped: {}", exc)


def _ensure_unified_search(db, existing_views: list) -> None:
    """unified_search — single view spanning lessons_v2, checkpoints, skill_chains.

    Replaces the old lessons_v2_search and checkpoints_search views.
    All AQL queries use 'FOR d IN unified_search' and PARSE_IDENTIFIER(d)
    to distinguish source collection.
    """
    view_name = "unified_search"
    _links: dict = {}

    if db.has_collection("lessons_v2"):
        _links["lessons_v2"] = {
            "includeAllFields": False,
            "analyzers": ["text_en"],
            "fields": {
                "title": {"analyzers": ["text_en"]},
                "problem": {"analyzers": ["text_en"]},
                "playbook": {"analyzers": ["text_en"]},
                "chunks": {"analyzers": ["text_en"]},
                "algorithms": {"analyzers": ["text_en"]},
                "code_snippets": {"analyzers": ["text_en"]},
                "equations": {"analyzers": ["text_en"]},
                "tables": {"analyzers": ["text_en"]},
                "tags": {"analyzers": ["text_en", "identity"]},
                "keywords": {"analyzers": ["text_en", "identity"]},
                "scope": {"analyzers": ["identity"]},
                "bridge_attributes": {"analyzers": ["identity"]},
                "mind": {"analyzers": ["identity"]},
            },
        }

    if db.has_collection("checkpoints"):
        _links["checkpoints"] = {
            "includeAllFields": False,
            "analyzers": ["text_en"],
            "fields": {
                "topic": {"analyzers": ["text_en"]},
                "resume": {"analyzers": ["text_en"]},
                "grade": {"analyzers": ["identity"]},
                "outcome": {"analyzers": ["identity"]},
                "scope": {"analyzers": ["identity"]},
                "tags": {"analyzers": ["text_en", "identity"]},
                "branch": {"analyzers": ["identity"]},
            },
        }

    if db.has_collection("skill_chains"):
        _links["skill_chains"] = {
            "includeAllFields": False,
            "analyzers": ["text_en"],
            "fields": {
                "problem": {"analyzers": ["text_en"]},
                "solution": {"analyzers": ["text_en"]},
                "task_description": {"analyzers": ["text_en"]},
                "tags": {"analyzers": ["text_en", "identity"]},
                "scope": {"analyzers": ["identity"]},
                "grade": {"analyzers": ["identity"]},
                "source": {"analyzers": ["identity"]},
            },
        }

    if db.has_collection("llm_invocations"):
        _links["llm_invocations"] = {
            "includeAllFields": False,
            "analyzers": ["text_en"],
            "fields": {
                "input": {"analyzers": ["text_en"]},
                "output": {"analyzers": ["text_en"]},
                "error": {"analyzers": ["text_en"]},
                "agent": {"analyzers": ["text_en", "identity"]},
                "tags": {"analyzers": ["text_en", "identity"]},
                "scope": {"analyzers": ["identity"]},
                "session_key": {"analyzers": ["identity"]},
                "outcome": {"analyzers": ["identity"]},
            },
        }

    if db.has_collection("dogpile_research"):
        _links["dogpile_research"] = {
            "includeAllFields": False,
            "analyzers": ["text_en"],
            "fields": {
                "query": {"analyzers": ["text_en"]},
                "synthesis": {"analyzers": ["text_en"]},
                "tags": {"analyzers": ["text_en", "identity"]},
                "topic_domain": {"analyzers": ["identity"]},
                "bridges": {"analyzers": ["identity"]},
                "date": {"analyzers": ["identity"]},
            },
        }

    if db.has_collection("walkthroughs"):
        _links["walkthroughs"] = {
            "includeAllFields": False,
            "analyzers": ["text_en"],
            "fields": {
                "skill_name": {"analyzers": ["text_en", "identity"]},
                "summary": {"analyzers": ["text_en"]},
                "pipeline_flow": {"analyzers": ["text_en"]},
                "gate_logic": {"analyzers": ["text_en"]},
                "expert_reviewer": {"analyzers": ["text_en", "identity"]},
                "tags": {"analyzers": ["text_en", "identity"]},
                "standards_alignment": {"analyzers": ["identity"]},
                "version": {"analyzers": ["identity"]},
                "date": {"analyzers": ["identity"]},
            },
        }

    if db.has_collection("project_knowledge"):
        _links["project_knowledge"] = {
            "includeAllFields": False,
            "analyzers": ["text_en"],
            "fields": {
                "project": {"analyzers": ["text_en", "identity"]},
                "scope": {"analyzers": ["identity"]},
                "understanding": {"analyzers": ["text_en"]},
                "decisions": {"analyzers": ["text_en"]},
                "key_files": {"analyzers": ["text_en"]},
                "open_questions": {"analyzers": ["text_en"]},
                "tags": {"analyzers": ["text_en", "identity"]},
            },
        }

    if not _links:
        return

    if view_name not in existing_views:
        try:
            db.create_arangosearch_view(view_name, properties={"links": _links})
        except Exception as exc:
            logger.error("view '{}' creation skipped: {}", view_name, exc)
    else:
        try:
            db.update_arangosearch_view(view_name, properties={"links": _links})
        except Exception as exc:
            logger.error("view '{}' update skipped: {}", view_name, exc)


def _ensure_llm_call_log_view(db, existing_views: list) -> None:
    """ArangoSearch view for llm_call_log (execution telemetry for /learn-timeout and /orchestrate)."""
    view_name = "llm_call_log_search"
    _links = {
        "llm_call_log": {
            "includeAllFields": False,
            "analyzers": ["text_en"],
            "fields": {
                "model_served": {"analyzers": ["text_en", "identity"]},
                "provider": {"analyzers": ["identity"]},
                "caller": {"analyzers": ["text_en", "identity"]},
                "error": {"analyzers": ["text_en"]},
                "status": {"analyzers": ["identity"]},
                "date": {"analyzers": ["identity"]},
            },
        }
    }
    if not db.has_collection("llm_call_log"):
        return
    if view_name not in existing_views:
        try:
            db.create_arangosearch_view(view_name, properties={"links": _links})
        except Exception as exc:
            logger.error("view '{}' creation skipped: {}", view_name, exc)
    else:
        try:
            db.update_arangosearch_view(view_name, properties={"links": _links})
        except Exception as exc:
            logger.error("view '{}' update skipped: {}", view_name, exc)


def _ensure_lessons_views(db, existing_views: list) -> None:
    """Views for lessons and lesson_texts collections."""
    _lessons_fields = {
        "title": {"analyzers": ["text_en"]},
        "problem": {"analyzers": ["text_en"]},
        "playbook": {"analyzers": ["text_en"]},
        "chunks": {"analyzers": ["text_en"]},
        "algorithms": {"analyzers": ["text_en"]},
        "code_snippets": {"analyzers": ["text_en"]},
        "equations": {"analyzers": ["text_en"]},
        "tables": {"analyzers": ["text_en"]},
        "tags": {"analyzers": ["text_en", "identity"]},
        "keywords": {"analyzers": ["text_en", "identity"]},
        "scope": {"analyzers": ["identity"]},
        # Wave 4 Heart/Mind migration — BDI tags on lessons
        # bridge_attributes kept for compat window (Wave 4 Task 15 removes it)
        "bridge_attributes": {"analyzers": ["identity"]},
        "mind": {"analyzers": ["identity"]},
    }

    view_name = "lessons_search"
    try:
        existing = [v.get("name") for v in db.views()]
    except Exception as exc:
        logger.error("listing views failed: {}", exc)
        existing = []
    if view_name not in existing:
        db.create_arangosearch_view(
            view_name,
            properties={
                "links": {
                    "lessons": {
                        "includeAllFields": False,
                        "analyzers": ["text_en"],
                        "fields": _lessons_fields,
                    }
                }
            },
        )
    else:
        # Best-effort update to ensure 'chunks' field is indexed
        try:
            db.update_arangosearch_view(
                view_name,
                properties={
                    "links": {
                        "lessons": {
                            "includeAllFields": False,
                            "analyzers": ["text_en"],
                            "fields": _lessons_fields,
                        }
                    }
                },
            )
        except Exception as exc:
            logger.error("view 'lessons_search' update skipped: {}", exc)

    # View for lesson_texts (full text / chunks) -- separate from lessons
    try:
        txt_view = "lesson_texts_search"
        existing = [v.get("name") for v in db.views()]
        if txt_view not in existing:
            db.create_arangosearch_view(
                txt_view,
                properties={
                    "links": {
                        "lesson_texts": {
                            "includeAllFields": False,
                            "analyzers": ["text_en"],
                            "fields": {
                                "text": {"analyzers": ["text_en"]},
                                "scope": {"analyzers": ["identity"]},
                            },
                        }
                    }
                },
            )
    except Exception as exc:
        logger.error("lesson_texts_search view setup skipped: {}", exc)


def _ensure_binary_features_view(db, existing_views: list) -> None:
    """ArangoSearch view for binary_features collection (binary-explorer scope)."""
    view_name = "binary_features_search"
    _links = {
        "binary_features": {
            "includeAllFields": False,
            "analyzers": ["text_en"],
            "fields": {
                "label": {"analyzers": ["text_en"]},
                "name": {"analyzers": ["text_en"]},
                "description": {"analyzers": ["text_en"]},
                "category": {"analyzers": ["text_en", "identity"]},
                "tags": {"analyzers": ["text_en", "identity"]},
                "nodeType": {"analyzers": ["identity"]},
                "cluster": {"analyzers": ["identity"]},
                "tier": {"analyzers": ["identity"]},
            },
        }
    }
    if not db.has_collection("binary_features"):
        return
    if view_name not in existing_views:
        try:
            db.create_arangosearch_view(view_name, properties={"links": _links})
        except Exception as exc:
            logger.error("view '{}' creation skipped: {}", view_name, exc)
    else:
        try:
            db.update_arangosearch_view(view_name, properties={"links": _links})
        except Exception as exc:
            logger.error("view '{}' update skipped: {}", view_name, exc)
