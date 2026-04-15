"""Formatter functions for RecallSource result rows."""

from __future__ import annotations

from typing import Any, Dict

from loguru import logger

from ._declarations import RecallSource

# ---------------------------------------------------------------------------
# Cached control_id → source_framework map (loaded once from ArangoDB)
# ---------------------------------------------------------------------------
_framework_map_cache: dict[str, str] | None = None


def _get_framework_map() -> dict[str, str]:
    """Cache UPPER(control_id) → source_framework from sparta_controls."""
    global _framework_map_cache
    if _framework_map_cache is not None:
        return _framework_map_cache
    try:
        from ..arango_client import get_db

        db = get_db()
        cursor = db.aql.execute(
            "FOR c IN sparta_controls "
            "RETURN {cid: UPPER(c.control_id), fw: c.source_framework}"
        )
        _framework_map_cache = {r["cid"]: r["fw"] for r in cursor if r["cid"] and r["fw"]}
    except Exception as exc:
        logger.error("framework_map cache load failed: %s", exc)
        _framework_map_cache = {}
    return _framework_map_cache

FORMATTERS: Dict[str, Any] = {}


def formatter(name):
    def _wrap(func):
        FORMATTERS[name] = func
        return func

    return _wrap


@formatter("task_state")
def _format_task_state(source: RecallSource, row: Dict[str, Any]) -> Dict[str, Any]:
    """Format task_state for recall results (plan_bundle type)."""
    out = {
        "_id": row.get("_id"),
        "_key": row.get("_key"),
        "type": source.kind,
        "task_key": row.get("task_key"),
        "goal": row.get("goal"),
        "agent": row.get("agent"),
        "status": row.get("status"),
        "output": row.get("output", "")[:500],  # Truncate for display
        "updated_at": row.get("updated_at"),
        "scope": row.get("scope"),
        "scores": row.get("scores", {}),
        "source": source.name,
    }
    return out


@formatter("code_symbol")
def _format_code_symbol(source: RecallSource, row: Dict[str, Any]) -> Dict[str, Any]:
    """Format code_symbol for recall results (code_context type)."""
    out = {
        "_id": row.get("_id"),
        "_key": row.get("_key"),
        "type": source.kind,
        "name": row.get("name"),
        "kind": row.get("kind"),  # function, class, method, etc.
        "file_path": row.get("file_path"),
        "signature": row.get("signature"),
        "docstring": row.get("docstring", "")[:300],  # Truncate
        "scope": row.get("scope"),
        "scores": row.get("scores", {}),
        "source": source.name,
    }
    return out


@formatter("agent_conversation")
def _format_agent_conversation(source: RecallSource, row: Dict[str, Any]) -> Dict[str, Any]:
    out = {
        "_id": row.get("_id"),
        "_key": row.get("_key"),
        "type": source.kind,
        "session_id": row.get("session_id"),
        "body": row.get("body"),
        "category": row.get("category"),
        "timestamp": row.get("timestamp"),
        "from": row.get("id_from"),
        "to": row.get("id_to"),
        "scores": row.get("scores", {}),
        "source": source.name,
    }
    edges = row.get("edges")
    if edges:
        out["edges"] = edges
    return out


@formatter("session_summary")
def _format_session_summary(source: RecallSource, row: Dict[str, Any]) -> Dict[str, Any]:
    """Format session_summary for recall results."""
    assessment = row.get("assessment") or {}
    return {
        "_id": row.get("_id"),
        "_key": row.get("_key"),
        "type": source.kind,
        "session_id": row.get("session_id"),
        "source_name": row.get("source_name"),
        "resolution_status": assessment.get("resolution_status"),
        "quality_score": assessment.get("quality_score"),
        "problems": assessment.get("problems_addressed", [])[:3],
        "solutions": assessment.get("solutions_found", [])[:3],
        "learnings": assessment.get("key_learnings", [])[:3],
        "unresolved": assessment.get("unresolved", [])[:3],
        "bridge_attributes": row.get("bridge_attributes", []),
        "created_at": row.get("created_at"),
        "scores": row.get("scores", {}),
        "source": source.name,
    }


@formatter("doc_chunk")
def _format_doc_chunk(source: RecallSource, row: Dict[str, Any]) -> Dict[str, Any]:
    """Format doc_chunk for recall results."""
    return {
        "_id": row.get("_id"),
        "_key": row.get("_key"),
        "type": source.kind,
        "content": row.get("content", "")[:500],
        "source_path": row.get("source_path"),
        "scope": row.get("scope"),
        "scores": row.get("scores", {}),
        "source": source.name,
    }


@formatter("lean_theorem")
def _format_lean_theorem(source: RecallSource, row: Dict[str, Any]) -> Dict[str, Any]:
    """Format lean_theorem for recall results (DeepSeek-Prover dataset)."""
    # Combine header + statement + proof for full code
    header = row.get("header", "")
    statement = row.get("formal_statement", "")
    proof = row.get("formal_proof", "")
    full_code = f"{header}\n\n{statement}\n{proof}".strip()

    return {
        "_id": row.get("_id"),
        "_key": row.get("_key"),
        "type": source.kind,
        "formal_statement": statement[:300],
        "formal_proof": proof[:200],
        "full_code": full_code[:600],  # Truncated for display
        "tactics": row.get("tactics", []),
        "dataset_source": row.get("source"),  # deepseek-prover-v1, v2, agent-generated
        "status": row.get("status"),
        "scores": row.get("scores", {}),
        "source": source.name,
    }


@formatter("lean4_autoformalization")
def _format_lean4_autoformalization(source: RecallSource, row: Dict[str, Any]) -> Dict[str, Any]:
    """Format lean4_autoformalization pair for recall results."""
    english = row.get("english", "")
    lean4 = row.get("lean4_statement", "")
    return {
        "_id": row.get("_id"),
        "_key": row.get("_key"),
        "type": source.kind,
        "english": english[:300],
        "lean4_statement": lean4[:500],
        "source": row.get("source"),
        "verified": row.get("verified"),
        "fidelity_score": row.get("fidelity_score"),
        "scores": row.get("scores", {}),
    }


@formatter("lean4_proof")
def _format_lean4_proof(source: RecallSource, row: Dict[str, Any]) -> Dict[str, Any]:
    """Format lean4_proof for recall results (DeepSeek Prover V2 dataset)."""
    return {
        "_id": row.get("_id"),
        "_key": row.get("_key"),
        "type": source.kind,
        "theorem_name": row.get("theorem_name"),
        "problem_description": row.get("problem_description", "")[:300],
        "lean_code": row.get("lean_code", "")[:500],
        "tactics": row.get("tactics", []),
        "imports": row.get("imports", []),
        "needs_mathlib": row.get("needs_mathlib"),
        "scores": row.get("scores", {}),
        "source": source.name,
    }


@formatter("lesson_text")
def _format_lesson_text(source: RecallSource, row: Dict[str, Any]) -> Dict[str, Any]:
    """Format lesson_text for recall results."""
    return {
        "_id": row.get("_id"),
        "_key": row.get("_key"),
        "type": source.kind,
        "text": row.get("text", "")[:500],
        "lesson_id": row.get("lesson_id"),
        "scope": row.get("scope"),
        "scores": row.get("scores", {}),
        "source": source.name,
    }


@formatter("datalake_chunk")
def _format_datalake_chunk(source: RecallSource, row: Dict[str, Any]) -> Dict[str, Any]:
    """Format datalake_chunk for recall results (extracted document content)."""
    source_meta = row.get("source_meta") or {}
    out = {
        "_id": row.get("_id"),
        "_key": row.get("_key"),
        "type": source.kind,
        "text": row.get("text", "")[:800],
        "doc_id": row.get("doc_id"),
        "asset_type": row.get("asset_type"),
        "section_id": source_meta.get("section_id"),
        "page": source_meta.get("page"),
        "bridge_tags": row.get("bridge_tags", []),
        "taxonomy": row.get("taxonomy", {}),
        "scores": row.get("scores", {}),
        "source": source.name,
    }
    # Extract control references and implementations from edges
    edges = row.get("edges", [])
    control_refs = [e for e in edges if e.get("edge_collection") == "chunk_control_edges"]
    impl_refs = [e for e in edges if e.get("edge_collection") == "requirement_control_edges"]
    if control_refs:
        out["referenced_controls"] = [
            {
                "control_id": e.get("control_id"),
                "framework": e.get("framework"),
                "confidence": e.get("confidence"),
                "name": (e.get("target") or {}).get("name", ""),
            }
            for e in control_refs
        ]
    if impl_refs:
        out["implements_controls"] = [
            {
                "control_id": e.get("control_id"),
                "framework": e.get("framework"),
                "lean4_status": e.get("lean4_status", "pending"),
                "name": (e.get("target") or {}).get("name", ""),
            }
            for e in impl_refs
        ]
    return out


@formatter("sanity_script")
def _format_sanity_script(source: RecallSource, row: Dict[str, Any]) -> Dict[str, Any]:
    """Format sanity_script for recall results (executable code examples)."""
    last_run = row.get("last_run") or {}
    return {
        "_id": row.get("_id"),
        "_key": row.get("_key"),
        "type": source.kind,
        "name": row.get("name"),
        "request": row.get("request", "")[:300],
        "description": row.get("description", "")[:200],
        "script": row.get("script", "")[:500],  # Truncate for display
        "language": row.get("language"),
        "tags": row.get("tags") or [],
        "symbol_names": row.get("symbol_names") or [],
        "scope": row.get("scope"),
        "status": row.get("status"),
        "last_run_passed": last_run.get("passed") if last_run else None,
        "last_run_at": last_run.get("at") if last_run else None,
        "scores": row.get("scores", {}),
        "source": source.name,
    }


@formatter("skill_description")
def _format_skill_description(source: RecallSource, row: Dict[str, Any]) -> Dict[str, Any]:
    """Format skill description for recall results (capability overlap detection)."""
    return {
        "_id": row.get("_id"),
        "_key": row.get("_key"),
        "type": source.kind,
        "name": row.get("name"),
        "description": row.get("description", "")[:500],
        "provides": row.get("provides", []),
        "composes": row.get("composes", []),
        "triggers": row.get("triggers", [])[:10],
        "taxonomy": row.get("taxonomy", []),
        "skill_path": row.get("skill_path"),
        "scores": row.get("scores", {}),
        "source": source.name,
    }


@formatter("sparta_control")
def _format_sparta_control(source: RecallSource, row: Dict[str, Any]) -> Dict[str, Any]:
    """Format SPARTA control metadata for recall results."""
    out = {
        "_id": row.get("_id"),
        "_key": row.get("_key"),
        "type": source.kind,
        "control_id": row.get("control_id"),
        "name": row.get("name") or row.get("title", ""),
        "description": row.get("description") or row.get("definition", ""),
        "control_type": row.get("control_type") or row.get("category", ""),
        "source_framework": row.get("source_framework", ""),
        "parent_id": row.get("parent_id"),
        "domain": row.get("domain", ""),
        "scores": row.get("scores", {}),
        "source": source.name,
    }
    # Include document chunks and requirements linked to this control
    edges = row.get("edges", [])
    chunk_refs = [e for e in edges if e.get("edge_collection") == "chunk_control_edges"]
    req_refs = [e for e in edges if e.get("edge_collection") == "requirement_control_edges"]
    if chunk_refs:
        out["referencing_chunks"] = [
            {
                "chunk_key": (e.get("target") or {}).get("_key", ""),
                "text_preview": (e.get("target") or {}).get("text", "")[:200],
                "confidence": e.get("confidence"),
            }
            for e in chunk_refs
        ]
    if req_refs:
        out["implementing_requirements"] = [
            {
                "chunk_key": (e.get("target") or {}).get("_key", ""),
                "text_preview": (e.get("target") or {}).get("text", "")[:200],
                "lean4_status": e.get("lean4_status", "pending"),
            }
            for e in req_refs
        ]
    return out


@formatter("technique_knowledge")
def _format_technique_knowledge(source: RecallSource, row: Dict[str, Any]) -> Dict[str, Any]:
    """Format technique_knowledge for recall results (technique-level ground truth)."""
    out = {
        "_id": row.get("_id"),
        "_key": row.get("_key"),
        "type": source.kind,
        "control_id": row.get("control_id"),
        "control_name": row.get("control_name"),
        "source_framework": row.get("source_framework", ""),
        "tactic": row.get("tactic"),
        "control_type": row.get("control_type"),
        "description": row.get("description", "")[:300],
        "url_content_text": row.get("url_content_text", "")[:500],
        "url_sources": row.get("url_sources", []),
        "content_quality": row.get("content_quality"),
        "scores": row.get("scores", {}),
        "source": source.name,
    }
    # Include hierarchy edges (tactic→technique→countermeasure)
    edges = row.get("edges", [])
    hierarchy_refs = [e for e in edges if e.get("edge_collection") == "technique_hierarchy_edges"]
    if hierarchy_refs:
        out["hierarchy"] = [
            {
                "target_id": (e.get("target") or {}).get("control_id", ""),
                "target_name": (e.get("target") or {}).get("control_name", ""),
                "relationship": e.get("relationship", ""),
            }
            for e in hierarchy_refs
        ]
    return out


@formatter("sparta_qra")
def _format_sparta_qra(source: RecallSource, row: Dict[str, Any]) -> Dict[str, Any]:
    """Format SPARTA QRA for recall results."""
    from ..store import get_mind_tags
    return {
        "_id": row.get("_id"),
        "_key": row.get("_key"),
        "type": source.kind,
        "control_id": row.get("control_id"),
        "question": row.get("question", "")[:300],
        "reasoning": row.get("reasoning", "")[:500],
        "answer": row.get("answer", "")[:500],
        "grounding_score": row.get("grounding_score"),
        "reasoning_grade": row.get("reasoning_grade"),
        "mind": get_mind_tags(row),
        "scores": row.get("scores", {}),
        "source": source.name,
    }


def _classify_framework(cid: str) -> str:
    """Resolve control_id to its source_framework using the DB cache.

    Prefers the authoritative ``source_framework`` field stored on
    ``sparta_controls``.  Falls back to a lightweight prefix heuristic
    (returning **lowercase** values that match the DB convention) only
    when the DB cache is empty or the control_id is unknown.
    """
    if not cid:
        return "other"
    fw_map = _get_framework_map()
    fw = fw_map.get(cid.upper())
    if fw:
        return fw
    # Fallback: lowercase values matching DB convention
    upper = cid.upper()
    if upper.startswith("D3-"):
        return "d3fend"
    if upper.startswith("CWE-"):
        return "cwe"
    if upper.startswith("CAPEC-"):
        return "capec"
    if upper.startswith(("SV-", "RD-", "REC-", "EX-", "PNT-", "DE-", "LM-", "ARFS-", "PER-")):
        return "sparta"
    if upper.startswith("CM") and len(upper) > 2 and upper[2:3].isdigit():
        return "sparta"
    if upper.startswith("A."):
        return "iso"
    if upper.startswith("T") and len(upper) > 1 and upper[1:].replace(".", "").isdigit():
        return "attack"
    if len(upper) >= 4 and upper[:2].isalpha() and upper[2] == "-" and upper[3:4].isdigit():
        return "nist"
    return "other"


@formatter("sparta_relationship")
def _format_sparta_relationship(source: RecallSource, row: Dict[str, Any]) -> Dict[str, Any]:
    """Format SPARTA cross-framework relationship for recall results.

    Uses ``source_framework`` from the DB-backed cache when available;
    falls back to ID-prefix heuristic with lowercase values matching DB
    convention.
    """
    src = row.get("source_control_id", "")
    tgt = row.get("target_control_id", "")

    return {
        "_id": row.get("_id"),
        "_key": row.get("_key"),
        "type": source.kind,
        "source_control_id": src,
        "target_control_id": tgt,
        "source_framework": _classify_framework(src),
        "target_framework": _classify_framework(tgt),
        "combined_score": row.get("combined_score"),
        "method": row.get("method"),
        "scores": row.get("scores", {}),
        "source": source.name,
    }


def apply_formatter(source: RecallSource, row: Dict[str, Any]) -> Dict[str, Any]:
    if source.formatter and source.formatter in FORMATTERS:
        return FORMATTERS[source.formatter](source, row)
    base = dict(row)
    base["source"] = source.name
    base["type"] = source.kind
    return base
