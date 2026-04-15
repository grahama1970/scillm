"""RecallSource dataclass and builtin source definitions."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional

_ALLOWED_NAME = re.compile(r"^[A-Za-z0-9_./-]+$")


@dataclass
class RecallSource:
    """Declarative description of an additional recall surface."""

    name: str
    kind: str
    view: str
    collection: str
    text_fields: List[str]
    result_fields: List[str]
    limit: int = 5
    weight: float = 1.0
    scope_field: Optional[str] = None
    tags_field: Optional[str] = None
    include_edges: bool = False
    edge_limit: int = 5
    formatter: Optional[str] = None
    global_access: bool = False  # If True, include even when scope is set (shared knowledge)
    extra_edge_collections: Optional[List[str]] = None  # Additional edge collections to traverse
    entity_field: Optional[str] = None  # Field for entity-based filtering (e.g. "control_id")


def _safe(value: str) -> str:
    if not _ALLOWED_NAME.match(value):
        raise ValueError(f"Invalid identifier for recall source: {value}")
    return value


def _default_agent_conversation_limit() -> int:
    return int(os.getenv("RECALL_EPISODE_LIMIT", "6") or "6")


def _default_task_state_limit() -> int:
    return int(os.getenv("RECALL_TASK_STATE_LIMIT", "3") or "3")


def _default_code_symbols_limit() -> int:
    return int(os.getenv("RECALL_CODE_SYMBOLS_LIMIT", "5") or "5")


def builtin_sources() -> List[RecallSource]:
    """Built-in optional supplemental sources."""

    sources: List[RecallSource] = []

    # Task states for plan_state / latent reasoning (anti-repeat, resume)
    if os.getenv("RECALL_INCLUDE_TASK_STATES", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="task_states",
                kind="plan_bundle",
                view="task_states_search",
                collection="task_states",
                text_fields=["goal", "output"],
                result_fields=[
                    "task_key",
                    "goal",
                    "agent",
                    "status",
                    "output",
                    "updated_at",
                    "scope",
                ],
                scope_field="scope",
                tags_field=None,
                limit=_default_task_state_limit(),
                weight=1.0,
                include_edges=False,
                formatter="task_state",
            )
        )

    # Code symbols from treesitter extraction (codebase search)
    if os.getenv("RECALL_INCLUDE_CODE_SYMBOLS", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="code_symbols",
                kind="code_context",
                view="code_symbols_search",
                collection="code_symbols",
                text_fields=["name", "docstring"],
                result_fields=[
                    "name",
                    "kind",
                    "file_path",
                    "docstring",
                    "signature",
                    "scope",
                ],
                scope_field="scope",
                tags_field=None,
                limit=_default_code_symbols_limit(),
                weight=0.8,  # Slightly lower than lessons
                include_edges=False,
                formatter="code_symbol",
            )
        )

    # Agent conversations (episodic turns)
    if os.getenv("RECALL_INCLUDE_AGENT_CONVERSATIONS", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="agent_conversations",
                kind="episode_turn",
                view="agent_conversations_search",
                collection="agent_conversations",
                text_fields=["body", "category"],
                result_fields=[
                    "session_id",
                    "body",
                    "category",
                    "timestamp",
                    "id_from",
                    "id_to",
                    "type",
                ],
                scope_field=None,
                tags_field=None,
                limit=_default_agent_conversation_limit(),
                include_edges=True,
                edge_limit=int(os.getenv("RECALL_EPISODE_EDGE_LIMIT", "5") or "5"),
                formatter="agent_conversation",
            )
        )

    # Session summaries (LLM-assessed session analysis for nightly reflection)
    if os.getenv("RECALL_INCLUDE_SESSION_SUMMARIES", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="session_summaries",
                kind="session_summary",
                view="session_summaries_search",
                collection="session_summaries",
                text_fields=[
                    "assessment.problems_addressed",
                    "assessment.solutions_found",
                    "assessment.key_learnings",
                ],
                result_fields=[
                    "session_id",
                    "source_name",
                    "assessment",
                    "bridge_attributes",
                    "taxonomy",
                    "created_at",
                ],
                scope_field=None,
                tags_field=None,
                limit=int(os.getenv("RECALL_SESSION_SUMMARY_LIMIT", "3") or "3"),
                weight=0.85,
                include_edges=True,
                edge_limit=3,
                formatter="session_summary",
            )
        )

    # Document chunks (ingested docs)
    if os.getenv("RECALL_INCLUDE_DOC_CHUNKS", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="doc_chunks",
                kind="doc_context",
                view="doc_chunks_search",
                collection="doc_chunks",
                text_fields=["content"],
                result_fields=[
                    "content",
                    "source_path",
                    "scope",
                ],
                scope_field="scope",
                tags_field=None,
                limit=3,
                weight=0.7,
                include_edges=False,
                formatter="doc_chunk",
            )
        )

    # Datalake document chunks (extracted PDFs -- sections, tables, figures)
    if os.getenv("RECALL_INCLUDE_DATALAKE_CHUNKS", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="datalake_chunks",
                kind="datalake_content",
                view="datalake_chunks_search",
                collection="datalake_chunks",
                text_fields=["text"],
                result_fields=[
                    "text",
                    "doc_id",
                    "asset_type",
                    "source",
                    "source_meta",
                    "bridge_tags",
                    "taxonomy",
                    "content_type",
                ],
                scope_field=None,
                tags_field="bridge_tags",
                limit=int(os.getenv("RECALL_DATALAKE_CHUNKS_LIMIT", "20") or "20"),
                weight=0.9,
                include_edges=True,
                edge_limit=3,
                formatter="datalake_chunk",
                global_access=True,
                extra_edge_collections=["chunk_control_edges", "requirement_control_edges"],
            )
        )

    # Lean theorems (formal proofs from DeepSeek-Prover datasets)
    if os.getenv("RECALL_INCLUDE_LEAN_THEOREMS", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="lean_theorems",
                kind="formal_proof",
                view="lean_theorems_search",
                collection="lean_theorems",
                text_fields=["formal_statement", "requirement", "goal"],
                result_fields=[
                    "formal_statement",
                    "formal_proof",
                    "header",
                    "tactics",
                    "source",
                    "status",
                ],
                scope_field=None,  # No scope - global dataset
                tags_field="tactics",
                limit=int(os.getenv("RECALL_LEAN_THEOREMS_LIMIT", "5") or "5"),
                weight=0.85,  # High weight - proven working Lean4 code
                include_edges=True,  # Enable graph traversal
                edge_limit=3,
                formatter="lean_theorem",
            )
        )

    # Sanity scripts (executable code examples)
    if os.getenv("RECALL_INCLUDE_SANITY_SCRIPTS", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="sanity_scripts",
                kind="executable_example",
                view="sanity_scripts_search",
                collection="sanity_scripts",
                text_fields=["name", "request", "description", "symbol_names", "tags"],
                result_fields=[
                    "name",
                    "request",
                    "description",
                    "script",
                    "language",
                    "dependencies",
                    "tags",
                    "symbol_names",
                    "scope",
                    "status",
                    "last_run",
                ],
                scope_field="scope",
                tags_field="tags",
                limit=int(os.getenv("RECALL_SANITY_SCRIPTS_LIMIT", "3") or "3"),
                weight=0.9,  # High weight - working code examples are valuable
                include_edges=True,
                edge_limit=3,
                formatter="sanity_script",
            )
        )

    # Lesson texts (full text for lessons)
    if os.getenv("RECALL_INCLUDE_LESSON_TEXTS", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="lesson_texts",
                kind="full_text",
                view="lesson_texts_search",
                collection="lesson_texts",
                text_fields=["text"],
                result_fields=[
                    "text",
                    "lesson_id",
                    "scope",
                ],
                scope_field="scope",
                tags_field=None,
                limit=2,
                weight=0.5,  # Lower weight - supplements main lessons
                include_edges=False,
                formatter="lesson_text",
            )
        )

    # Skill descriptions (capability overlap detection -- prevents building silos)
    if os.getenv("RECALL_INCLUDE_SKILL_DESCRIPTIONS", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="skill_descriptions",
                kind="skill_capability",
                view="skill_descriptions_search",
                collection="skill_descriptions",
                text_fields=["name", "description", "provides", "triggers", "body"],
                result_fields=[
                    "name", "description", "provides", "composes",
                    "triggers", "taxonomy", "skill_path",
                ],
                limit=int(os.getenv("RECALL_SKILL_DESCRIPTIONS_LIMIT", "3") or "3"),
                weight=0.95,
                scope_field=None,
                tags_field="taxonomy",
                include_edges=False,
                formatter="skill_description",
                global_access=True,
            )
        )

    # SPARTA Controls (control metadata: name, description, framework, relationships)
    if os.getenv("RECALL_INCLUDE_SPARTA_CONTROLS", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="sparta_controls",
                kind="control_metadata",
                view="sparta_unified_search",
                collection="sparta_controls",
                text_fields=["name", "description"],
                result_fields=[
                    "control_id", "name", "description", "control_type",
                    "source_framework", "parent_id", "domain",
                ],
                limit=int(os.getenv("RECALL_SPARTA_CONTROLS_LIMIT", "5") or "5"),
                weight=0.9,
                scope_field=None,
                tags_field=None,
                include_edges=True,
                edge_limit=5,
                formatter="sparta_control",
                global_access=True,
                extra_edge_collections=["chunk_control_edges", "requirement_control_edges"],
                entity_field="control_id",
            )
        )

    # Technique knowledge corpus (technique-level ground truth from URL content)
    if os.getenv("RECALL_INCLUDE_TECHNIQUE_KNOWLEDGE", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="technique_knowledge",
                kind="technique_context",
                view="technique_knowledge_search",
                collection="technique_knowledge",
                text_fields=["description", "url_content_text", "control_name"],
                result_fields=[
                    "control_id", "control_name", "description",
                    "url_content_text", "source_framework", "tactic",
                    "control_type", "url_sources", "sentence_count",
                    "has_tables", "content_quality",
                ],
                limit=int(os.getenv("RECALL_TECHNIQUE_KNOWLEDGE_LIMIT", "5") or "5"),
                weight=0.95,  # High weight - primary source material, not LLM summaries
                scope_field=None,
                tags_field=None,
                include_edges=True,
                edge_limit=5,
                formatter="technique_knowledge",
                global_access=True,  # Always include for SPARTA queries
                extra_edge_collections=["technique_hierarchy_edges"],
                entity_field="control_id",
            )
        )

    # SPARTA QRAs (pre-vetted question-reasoning-answer pairs for space cyber)
    if os.getenv("RECALL_INCLUDE_SPARTA_QRA", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="sparta_qra",
                kind="qra_context",
                view="sparta_unified_search",
                collection="sparta_qra",
                text_fields=["question", "answer", "reasoning"],
                result_fields=[
                    "question", "answer", "reasoning", "control_id",
                    "grounding_score", "reasoning_grade",
                    "mind", "conceptual_tags", "tactical_tags",
                ],
                limit=int(os.getenv("RECALL_SPARTA_QRA_LIMIT", "50") or "50"),
                weight=1.0,
                scope_field=None,
                tags_field="mind",
                include_edges=True,
                edge_limit=3,
                formatter="sparta_qra",
                global_access=True,
                entity_field="control_id",
            )
        )

    # SPARTA Relationships (cross-framework edges for disambiguation evidence)
    if os.getenv("RECALL_INCLUDE_SPARTA_RELATIONSHIPS", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="sparta_relationships",
                kind="control_relationship",
                view="sparta_relationships_search",
                collection="sparta_relationships",
                text_fields=["source_control_id", "target_control_id"],
                result_fields=[
                    "source_control_id", "target_control_id",
                    "combined_score", "method",
                ],
                limit=int(os.getenv("RECALL_SPARTA_RELATIONSHIPS_LIMIT", "20") or "20"),
                weight=0.7,
                scope_field=None,
                tags_field=None,
                include_edges=False,
                formatter="sparta_relationship",
                global_access=True,
                entity_field="source_control_id",
            )
        )

    # SPARTA URLs (fetched URL metadata: url, domain, status)
    if os.getenv("RECALL_INCLUDE_SPARTA_URLS", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="sparta_urls",
                kind="url_metadata",
                view="sparta_urls_search",
                collection="sparta_urls",
                text_fields=["url", "domain"],
                result_fields=[
                    "url_id", "url", "domain", "updated_at",
                ],
                limit=int(os.getenv("RECALL_SPARTA_URLS_LIMIT", "20") or "20"),
                weight=0.7,
                scope_field=None,
                tags_field=None,
                include_edges=False,
                formatter="sparta_url",
                global_access=True,
                entity_field="url_id",
            )
        )

    # SPARTA URL Knowledge (extracted knowledge chunks from fetched URLs)
    if os.getenv("RECALL_INCLUDE_SPARTA_URL_KNOWLEDGE", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="sparta_url_knowledge",
                kind="url_knowledge",
                view="sparta_url_knowledge_search",
                collection="sparta_url_knowledge",
                text_fields=["text", "topic"],
                result_fields=[
                    "url_id", "control_id", "control_ids", "text", "topic",
                    "excerpt_type", "source_framework", "source_frameworks",
                    "tactic", "tactics", "excerpt_index",
                ],
                limit=int(os.getenv("RECALL_SPARTA_URL_KNOWLEDGE_LIMIT", "10") or "10"),
                weight=0.85,
                scope_field=None,
                tags_field=None,
                include_edges=False,
                formatter="sparta_url_knowledge",
                global_access=True,
                entity_field="control_id",
            )
        )

    # Lean4 autoformalization pairs (English<->Lean4 for retrieval-augmented proving)
    if os.getenv("RECALL_INCLUDE_LEAN4_AUTOFORMALIZATION", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="lean4_autoformalization",
                kind="autoformalization",
                view="lean4_autoformalization_search",
                collection="lean4_autoformalization",
                text_fields=["english", "lean4_statement"],
                result_fields=[
                    "english",
                    "lean4_statement",
                    "source",
                    "verified",
                    "fidelity_score",
                    "created_at",
                ],
                scope_field=None,  # No scope - global dataset
                tags_field=None,
                limit=int(os.getenv("RECALL_LEAN4_AUTOFORMALIZATION_LIMIT", "5") or "5"),
                weight=0.85,
                include_edges=False,
                formatter="lean4_autoformalization",
            )
        )

    # Lean4 proofs (DeepSeek Prover V2 dataset - few-shot examples for theorem proving)
    if os.getenv("RECALL_INCLUDE_LEAN4_PROOFS", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="lean4_proofs",
                kind="formal_proof",
                view="lean4_proofs_search",
                collection="lean4_proofs",
                text_fields=["problem_description", "theorem_name"],
                result_fields=[
                    "theorem_name",
                    "problem_description",
                    "lean_code",
                    "tactics",
                    "imports",
                    "needs_mathlib",
                ],
                scope_field=None,  # No scope - global dataset
                tags_field="tactics",
                limit=int(os.getenv("RECALL_LEAN4_PROOFS_LIMIT", "3") or "3"),
                weight=0.85,  # High weight - proven working Lean4 code
                include_edges=False,
                formatter="lean4_proof",
            )
        )

    # Skill chains: proven skill compositions with problem/solution/grade
    if os.getenv("RECALL_INCLUDE_SKILL_CHAINS", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="skill_chains",
                kind="skill_chain",
                view="unified_search",
                collection="skill_chains",
                text_fields=["problem", "solution", "task_description"],
                result_fields=[
                    "problem", "solution", "skills", "task_type",
                    "grade", "success_rate", "observation_count",
                    "elegance_grade", "source", "commit",
                    "files_changed", "tags", "scope",
                    "mind", "code",
                ],
                scope_field="scope",
                tags_field="tags",
                limit=int(os.getenv("RECALL_SKILL_CHAINS_LIMIT", "3") or "3"),
                weight=1.0,
                include_edges=True,
                edge_limit=3,
                formatter="default",
                extra_edge_collections=["skill_chain_edges"],
            )
        )

    # Checkpoints: session state snapshots from /checkpoint (resume after /clear)
    if os.getenv("RECALL_INCLUDE_CHECKPOINTS", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="checkpoints",
                kind="session_checkpoint",
                view="unified_search",
                collection="checkpoints",
                text_fields=["topic", "resume", "title", "problem", "playbook"],
                result_fields=[
                    "title", "problem", "playbook", "tags", "scope",
                    "topic", "grade", "outcome", "resume",
                    "skills_used", "timestamp",
                ],
                scope_field="scope",
                tags_field="tags",
                limit=int(os.getenv("RECALL_CHECKPOINTS_LIMIT", "3") or "3"),
                weight=0.95,  # High weight - proven session state
                include_edges=False,
                formatter="default",
            )
        )

    # QuerySpec actions: registered UI actions + Opus-labeled training pairs
    # for voice→QuerySpec pipeline (all Embry OS apps)
    if os.getenv("RECALL_INCLUDE_APP_ACTIONS", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="app_actions",
                kind="queryspec",
                view="app_actions_search",
                collection="app_actions",
                text_fields=["problem", "solution", "label", "description"],
                result_fields=[
                    "problem", "solution", "label", "description",
                    "action", "app", "doc_type", "element_id",
                    "tags", "scope",
                ],
                scope_field="scope",
                tags_field="tags",
                limit=int(os.getenv("RECALL_APP_ACTIONS_LIMIT", "10") or "10"),
                weight=1.0,
                include_edges=False,
                formatter="default",
            )
        )

    # LLM invocations: code-runner round traces with tool call history
    if os.getenv("RECALL_INCLUDE_LLM_INVOCATIONS", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="llm_invocations",
                kind="agent_trace",
                view="unified_search",
                collection="llm_invocations",
                text_fields=["input", "output", "error"],
                result_fields=[
                    "agent", "session_key", "round", "outcome",
                    "model", "score", "error", "tags",
                    "metadata", "timestamp",
                ],
                scope_field="scope",
                tags_field="tags",
                limit=int(os.getenv("RECALL_LLM_INVOCATIONS_LIMIT", "5") or "5"),
                weight=0.75,
                include_edges=False,
                formatter="default",
            )
        )

    # LLM call log: execution telemetry for /learn-timeout and /orchestrate
    if os.getenv("RECALL_INCLUDE_LLM_CALL_LOG", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="llm_call_log",
                kind="execution_telemetry",
                view="llm_call_log_search",
                collection="llm_call_log",
                text_fields=["model_served", "provider", "caller", "error"],
                result_fields=[
                    "model_requested", "model_served", "provider",
                    "duration_ms", "prompt_tokens", "completion_tokens",
                    "total_tokens", "cost_usd", "status", "error",
                    "caller", "date", "ts",
                ],
                scope_field=None,
                tags_field=None,
                limit=int(os.getenv("RECALL_LLM_CALL_LOG_LIMIT", "10") or "10"),
                weight=0.6,  # Lower weight — telemetry, not knowledge
                include_edges=True,
                edge_limit=5,
                formatter="default",
                extra_edge_collections=["llm_call_log_edges"],
            )
        )

    # Dogpile research: /dogpile search results for avoiding redundant research
    if os.getenv("RECALL_INCLUDE_DOGPILE_RESEARCH", "1").lower() not in {"0", "false", "no"}:
        sources.append(
            RecallSource(
                name="dogpile_research",
                kind="research_snapshot",
                view="unified_search",
                collection="dogpile_research",
                text_fields=["query", "synthesis"],
                result_fields=[
                    "query", "synthesis", "sources_searched",
                    "key_urls", "bridges", "topic_domain",
                    "tags", "date", "ts",
                ],
                scope_field=None,
                tags_field="tags",
                limit=int(os.getenv("RECALL_DOGPILE_RESEARCH_LIMIT", "5") or "5"),
                weight=0.9,  # High weight — avoid redundant research
                include_edges=False,
                formatter="default",
                global_access=True,  # Always include for research queries
            )
        )

    return sources
