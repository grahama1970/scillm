"""P5: finalize phase for curate pipeline.

Purpose: Write artifacts, update run record, cleanup, persist to ArangoDB.

Assertions (from 02_SPEC.md):
- run_summary.json written (Under run/artifacts/<run_id>/)
- Run record updated (status=ok/partial/failed_soft)
- Stage statuses recorded (Per-phase status in run record)
- Worktree cleaned up (Temp directory removed)
- No orphan resources (All temp files cleaned)
- Data persisted to ArangoDB collections
"""

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from loguru import logger

from graph_memory.codebase.pipeline.types import (
    PhaseResult,
    PhaseStatus,
    RunContext,
)


def cleanup_worktree(context: RunContext) -> list[str]:
    """Clean up worktree if it exists.

    Returns list of warnings.
    """
    warnings = []

    if not context.worktree_path:
        return warnings

    worktree_path = context.worktree_path

    # First try git worktree remove
    try:
        result = subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=context.code_path,
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            return warnings
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Fall back to direct removal
    if worktree_path.exists():
        try:
            shutil.rmtree(worktree_path)
        except Exception as e:
            warnings.append(f"Could not remove worktree: {e}")

    # Prune worktrees
    try:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=context.code_path,
            capture_output=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return warnings


def determine_run_status(context: RunContext) -> str:
    """Determine overall run status from phase results."""
    statuses = []
    for phase, result in context.phase_results.items():
        statuses.append(result.status)

    if PhaseStatus.FAILED_SOFT in statuses:
        return "failed_soft"
    if PhaseStatus.PARTIAL in statuses:
        return "partial"
    if all(s in {PhaseStatus.OK, PhaseStatus.SKIPPED, PhaseStatus.UNAVAILABLE} for s in statuses):
        return "ok"
    return "partial"


def aggregate_counts(context: RunContext) -> dict[str, int]:
    """Aggregate counts from all phases."""
    counts = {}
    for phase, result in context.phase_results.items():
        for key, value in result.counts.items():
            counts[key] = counts.get(key, 0) + value
    return counts


def aggregate_errors(context: RunContext) -> list[dict[str, Any]]:
    """Aggregate errors from all phases."""
    errors = []
    for phase, result in context.phase_results.items():
        for error in result.errors:
            if isinstance(error, dict):
                errors.append(error)
            else:
                errors.append({
                    "code": "PHASE_ERROR",
                    "message": str(error),
                    "phase": phase,
                })
    return errors


def persist_to_arango(context: RunContext) -> tuple[dict[str, int], list[str]]:
    """Persist extracted data to ArangoDB.

    Returns (counts, errors) tuple.
    """
    counts = {}
    errors = []

    try:
        from graph_memory.arango_client import get_db
        from graph_memory.setup_schema import ensure_collections_and_view

        db = get_db()
        ensure_collections_and_view()
    except Exception as e:
        errors.append(f"Could not connect to ArangoDB: {e}")
        return counts, errors

    run_id = context.run_id
    scope = context.scope
    repo_fp = context.repo_fingerprint

    # P2: code_files
    files_file = context.artifacts_path / "p2_ingest" / "code_files.json"
    if files_file.exists():
        try:
            files = json.loads(files_file.read_text())
            col = db.collection("code_files")
            for file_path in files:
                # Create document with file metadata
                rel_path = file_path.replace(str(context.code_path), "").lstrip("/")
                file_key = f"{repo_fp}_{hash(rel_path) % (10**12)}"[:254]
                doc = {
                    "_key": file_key,
                    "path": file_path,
                    "rel_path": rel_path,
                    "run_id": run_id,
                    "scope": scope,
                    "repo_fingerprint": repo_fp,
                }
                try:
                    col.insert(doc, overwrite=True)
                except Exception as exc:
                    logger.error("Suppressed error in p5_finalize: {}", exc)
            counts["code_files_persisted"] = len(files)
        except Exception as e:
            errors.append(f"Failed to persist code_files: {e}")

    # P2: code_symbols
    symbols_file = context.artifacts_path / "p2_ingest" / "code_symbols.json"
    if symbols_file.exists():
        try:
            symbols = json.loads(symbols_file.read_text())
            col = db.collection("code_symbols")
            for sym in symbols:
                sym["_key"] = f"{repo_fp}_{sym.get('file', '')}_{sym.get('name', '')}_{sym.get('line', 0)}"[:254]
                sym["run_id"] = run_id
                sym["scope"] = scope
                sym["repo_fingerprint"] = repo_fp
                try:
                    col.insert(sym, overwrite=True)
                except Exception as exc:
                    logger.error("Suppressed error in p5_finalize: {}", exc)
            counts["code_symbols_persisted"] = len(symbols)
        except Exception as e:
            errors.append(f"Failed to persist code_symbols: {e}")

    # P2: doc_chunks - build lookup for source linking
    chunks_file = context.artifacts_path / "p2_ingest" / "doc_chunks.json"
    source_to_chunk_key: dict[str, str] = {}  # source_path → chunk _key
    if chunks_file.exists():
        try:
            chunks = json.loads(chunks_file.read_text())
            col = db.collection("doc_chunks")
            for chunk in chunks:
                chunk_hash = chunk.get("hash", str(hash(chunk.get("content", ""))))[:32]
                chunk_key = f"{repo_fp}_{chunk_hash}"[:254]
                chunk["_key"] = chunk_key
                chunk["run_id"] = run_id
                chunk["scope"] = scope
                chunk["repo_fingerprint"] = repo_fp
                # Track source_path → key for linking
                source_path = chunk.get("source_path", "")
                if source_path:
                    source_to_chunk_key[source_path] = chunk_key
                try:
                    col.insert(chunk, overwrite=True)
                except Exception as exc:
                    logger.error("Suppressed error in p5_finalize: {}", exc)
            counts["doc_chunks_persisted"] = len(chunks)
        except Exception as e:
            errors.append(f"Failed to persist doc_chunks: {e}")

    # P3: pdf_refs
    pdf_refs_file = context.artifacts_path / "p3_pdf" / "pdf_refs.json"
    if pdf_refs_file.exists():
        try:
            pdf_refs = json.loads(pdf_refs_file.read_text())
            col = db.collection("pdf_refs")
            for i, ref in enumerate(pdf_refs):
                ref_key = f"{run_id}_{i}"[:254]
                ref["_key"] = ref_key
                ref["run_id"] = run_id
                ref["scope"] = scope
                try:
                    col.insert(ref, overwrite=True)
                except Exception as exc:
                    logger.error("Suppressed error in p5_finalize: {}", exc)
            counts["pdf_refs_persisted"] = len(pdf_refs)
        except Exception as e:
            errors.append(f"Failed to persist pdf_refs: {e}")

    # P3: pdf_docs - build lookup for source linking
    pdf_docs_file = context.artifacts_path / "p3_pdf" / "pdf_docs.json"
    pdf_path_to_key: dict[str, str] = {}  # path → doc _key
    if pdf_docs_file.exists():
        try:
            pdf_docs = json.loads(pdf_docs_file.read_text())
            col = db.collection("pdf_docs")
            for doc in pdf_docs:
                doc_hash = doc.get("hash", "")[:64]
                doc_key = f"{run_id}_{doc_hash}"[:254]
                doc["_key"] = doc_key
                doc["run_id"] = run_id
                doc["scope"] = scope
                # Track path → key for linking
                pdf_path = doc.get("path", "")
                if pdf_path:
                    pdf_path_to_key[pdf_path] = doc_key
                try:
                    col.insert(doc, overwrite=True)
                except Exception as exc:
                    logger.error("Suppressed error in p5_finalize: {}", exc)
            counts["pdf_docs_persisted"] = len(pdf_docs)
        except Exception as e:
            errors.append(f"Failed to persist pdf_docs: {e}")

    # P3: equation_candidates
    equations_file = context.artifacts_path / "p3_pdf" / "equation_candidates.json"
    if equations_file.exists():
        try:
            equations = json.loads(equations_file.read_text())
            col = db.collection("equation_candidates")
            for i, eq in enumerate(equations):
                eq["_key"] = f"{run_id}_{i}"[:254]
                eq["run_id"] = run_id
                eq["scope"] = scope
                try:
                    col.insert(eq, overwrite=True)
                except Exception as exc:
                    logger.error("Suppressed error in p5_finalize: {}", exc)
            counts["equations_persisted"] = len(equations)
        except Exception as e:
            errors.append(f"Failed to persist equation_candidates: {e}")

    # P4: lean_candidates - build lookup for source linking
    candidates_file = context.artifacts_path / "p4_lean" / "lean_candidates.json"
    candidate_lookup: dict[str, dict] = {}
    if candidates_file.exists():
        try:
            candidates = json.loads(candidates_file.read_text())
            col = db.collection("lean_candidates")
            for cand in candidates:
                cand_hash = cand.get("hash", str(hash(cand.get("text", ""))))[:254]
                cand["_key"] = cand_hash
                cand["run_id"] = run_id
                cand["scope"] = scope
                candidate_lookup[cand_hash] = cand  # For source linking
                try:
                    col.insert(cand, overwrite=True)
                except Exception as exc:
                    logger.error("Suppressed error in p5_finalize: {}", exc)
            counts["lean_candidates_persisted"] = len(candidates)
        except Exception as e:
            errors.append(f"Failed to persist lean_candidates: {e}")

    # P4: lean_theorems - link to candidates AND back to sources
    theorems_file = context.artifacts_path / "p4_lean" / "lean_theorems.json"
    if theorems_file.exists():
        try:
            theorems = json.loads(theorems_file.read_text())
            col = db.collection("lean_theorems")
            edge_col = db.collection("curate_edges")
            proved_count = 0
            source_edges_count = 0
            for thm in theorems:
                cand_hash = thm.get("candidate_hash", "")
                thm_key = f"{run_id}_{cand_hash}"[:254]
                thm["_key"] = thm_key
                thm["run_id"] = run_id
                thm["scope"] = scope

                # Copy source info from candidate to theorem for easier querying
                if cand_hash in candidate_lookup:
                    cand = candidate_lookup[cand_hash]
                    thm["source_type"] = cand.get("source_type")
                    thm["source_ref"] = cand.get("source_ref")
                    thm["original_text"] = cand.get("text")
                    thm["kind"] = cand.get("kind")

                try:
                    col.insert(thm, overwrite=True)
                    if thm.get("status") == "ok":
                        proved_count += 1
                        # Edge: theorem → candidate
                        edge = {
                            "_key": f"proves_{thm_key}"[:254],
                            "_from": f"lean_theorems/{thm_key}",
                            "_to": f"lean_candidates/{cand_hash}",
                            "type": "proves",
                            "run_id": run_id,
                        }
                        try:
                            edge_col.insert(edge, overwrite=True)
                        except Exception as exc:
                            logger.error("Suppressed error in p5_finalize: {}", exc)

                        # Edge: theorem → source (derived_from)
                        if cand_hash in candidate_lookup:
                            cand = candidate_lookup[cand_hash]
                            source_type = cand.get("source_type", "")
                            source_ref = cand.get("source_ref", "")

                            if source_type == "doc_chunk" and source_ref:
                                # Find matching doc_chunk by source path
                                chunk_key = source_to_chunk_key.get(source_ref)
                                if chunk_key:
                                    source_edge = {
                                        "_key": f"derived_{thm_key}"[:254],
                                        "_from": f"lean_theorems/{thm_key}",
                                        "_to": f"doc_chunks/{chunk_key}",
                                        "type": "derived_from",
                                        "source_type": "doc_chunk",
                                        "source_path": source_ref,
                                        "run_id": run_id,
                                    }
                                    try:
                                        edge_col.insert(source_edge, overwrite=True)
                                        source_edges_count += 1
                                    except Exception as exc:
                                        logger.error("Suppressed error in p5_finalize: {}", exc)
                            elif source_type in ("pdf", "equation") and source_ref:
                                # Link to pdf_docs by source path (PDF file)
                                pdf_key = pdf_path_to_key.get(source_ref)
                                if pdf_key:
                                    source_edge = {
                                        "_key": f"derived_{thm_key}"[:254],
                                        "_from": f"lean_theorems/{thm_key}",
                                        "_to": f"pdf_docs/{pdf_key}",
                                        "type": "derived_from",
                                        "source_type": "pdf",
                                        "source_path": source_ref,
                                        "run_id": run_id,
                                    }
                                    try:
                                        edge_col.insert(source_edge, overwrite=True)
                                        source_edges_count += 1
                                    except Exception as exc:
                                        logger.error("Suppressed error in p5_finalize: {}", exc)
                except Exception as exc:
                    logger.error("Suppressed error in p5_finalize: {}", exc)
            counts["lean_theorems_persisted"] = len(theorems)
            counts["lean_theorems_proved"] = proved_count
            counts["source_edges_created"] = source_edges_count
        except Exception as e:
            errors.append(f"Failed to persist lean_theorems: {e}")

    # Promote proven theorems to lessons (searchable knowledge)
    if theorems_file.exists():
        try:
            theorems = json.loads(theorems_file.read_text())
            lessons_col = db.collection("lessons")
            lesson_edges_col = db.collection("lesson_edges")
            promoted_count = 0

            for thm in theorems:
                if thm.get("status") != "ok":
                    continue

                cand_hash = thm.get("candidate_hash", "")
                cand = candidate_lookup.get(cand_hash, {})

                # Create lesson from proven theorem
                lesson_key = f"theorem_{run_id}_{cand_hash}"[:254]
                original_text = cand.get("text", "")
                lean_code = thm.get("lean_code", "")
                source_ref = cand.get("source_ref", "")

                lesson = {
                    "_key": lesson_key,
                    "title": f"Proven: {original_text[:80]}",
                    "problem": original_text,
                    "playbook": lean_code,
                    "scope": scope,
                    "tags": ["proven", "lean4", cand.get("kind", "theorem")],
                    "keywords": [cand.get("kind", "theorem"), "formal_proof"],
                    "proof_status": "proved",
                    "source_type": cand.get("source_type", ""),
                    "source_path": source_ref,
                    "run_id": run_id,
                    "repo_fingerprint": repo_fp,
                    "lean4_code": lean_code,
                    "theorem_key": f"lean_theorems/{run_id}_{cand_hash}"[:254],
                }

                try:
                    from ...lessons.store import store_lesson
                    store_lesson(db, lesson)
                    promoted_count += 1

                    # Create edge from promoted lesson to source doc_chunk (if exists)
                    source_type = cand.get("source_type", "")
                    if source_type == "doc_chunk" and source_ref:
                        chunk_key = source_to_chunk_key.get(source_ref)
                        if chunk_key:
                            edge = {
                                "_key": f"from_doc_{lesson_key}"[:254],
                                "_from": f"lessons/{lesson_key}",
                                "_to": f"doc_chunks/{chunk_key}",
                                "type": "derived_from",
                                "weight": 1.0,
                                "provenance": "curate",
                                "run_id": run_id,
                            }
                            try:
                                lesson_edges_col.insert(edge, overwrite=True)
                            except Exception as exc:
                                logger.error("Suppressed error in p5_finalize: {}", exc)
                except Exception as exc:
                    logger.error("Suppressed error in p5_finalize: {}", exc)

            counts["lessons_promoted"] = promoted_count
        except Exception as e:
            errors.append(f"Failed to promote theorems to lessons: {e}")

    return counts, errors


def run_p5_finalize(context: RunContext) -> PhaseResult:
    """Execute P5: finalize phase.

    Args:
        context: RunContext with phase results from P1-P4

    Returns:
        PhaseResult
    """
    warnings: list[str] = []
    arango_counts: dict[str, int] = {}

    finished_at = datetime.now(timezone.utc)

    # Persist to ArangoDB
    arango_counts, arango_errors = persist_to_arango(context)
    if arango_errors:
        for err in arango_errors:
            warnings.append(f"Arango: {err}")

    # Cleanup worktree
    cleanup_warnings = cleanup_worktree(context)
    warnings.extend(cleanup_warnings)

    # Determine overall status
    run_status = determine_run_status(context)

    # Build phase status map
    phase_status = {}
    for phase in ["init", "ingest", "pdf_pipeline", "lean_pipeline", "finalize"]:
        if phase in context.phase_results:
            phase_status[phase] = context.phase_results[phase].status.value
        elif phase == "finalize":
            phase_status[phase] = "ok"  # We're running now
        else:
            phase_status[phase] = "skipped"

    # Aggregate counts and errors
    counts = aggregate_counts(context)
    counts.update(arango_counts)  # Add Arango persistence counts
    errors = aggregate_errors(context)

    # Build run summary
    duration_ms = int((finished_at - context.started_at).total_seconds() * 1000)

    summary = {
        "meta": {
            "run_id": context.run_id,
            "scope": context.scope,
            "started_at": context.started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_ms": duration_ms,
            "repo_fingerprint": context.repo_fingerprint,
            "worktree_path": str(context.worktree_path) if context.worktree_path else None,
            "config": context.config.to_dict(),
            "status": run_status,
            "phase_status": phase_status,
            "counts": counts,
            "errors_count": len(errors),
        },
        "items": [],  # Could be populated with key results
        "errors": errors,
    }

    # Write run_summary.json to artifacts root (not in p5 subfolder)
    summary_file = context.artifacts_path / "run_summary.json"
    summary_file.write_text(json.dumps(summary, indent=2))

    # Create P5 finalize result
    result = PhaseResult(
        status=PhaseStatus.OK,
        counts={
            "total_errors": len(errors),
            "duration_ms": duration_ms,
        },
        warnings=warnings,
        artifacts={
            "run_summary": str(summary_file),
        },
    )

    context.phase_results["finalize"] = result

    return result
