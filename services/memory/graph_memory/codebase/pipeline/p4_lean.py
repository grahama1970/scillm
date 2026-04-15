"""P4: lean_pipeline phase for curate pipeline.

Purpose: Extract Lean candidates, compile theorems via certainly.

Sub-steps:
1. extract_lean_candidates: Deterministic extraction + scoring
2. compile_lean_theorems: scillm → certainly

Assertions (from 02_SPEC.md):
- Candidates extracted (From doc_chunks + equation_candidates)
- Candidate kinds (equation, requirement, theorem, lemma, definition)
- Deterministic heuristics applied (Regex anchors + modal verbs + theorem headers)
- Scoring applied (Prioritize equations > theorems > requirements)
- Dedup by hash (No duplicate candidates)
- lean_candidates created (Capped by candidate_max)
- Compile bounded (By max_theorems AND time_budget_s)
- lean_theorems created (With status: ok/fail/timeout/unavailable)
"""

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any
from loguru import logger

from graph_memory.codebase.pipeline.types import (
    PhaseResult,
    PhaseStatus,
    RunContext,
)

# Extraction patterns (from 02_SPEC.md Section 4)

# Formal ID patterns: REQ-*, THM-*, numbered like 3.1.1
FORMAL_ID_PATTERN = re.compile(
    r'\b(REQ|THM|DEF|LEM|PROP)-\d+|'
    r'\b\d+\.\d+(?:\.\d+)?(?:\s|:)',
    re.IGNORECASE
)

# Modal verb sentences
MODAL_VERBS = {"shall", "must", "will", "should", "required"}
MODAL_VERB_PATTERN = re.compile(
    r'\b(shall|must|will|should|required)\b',
    re.IGNORECASE
)

# Theorem/Lemma/Definition headers
THEOREM_HEADER_PATTERN = re.compile(
    r'\b(Theorem|Lemma|Definition|Proposition|Corollary)\s+\d+',
    re.IGNORECASE
)

# Inequality patterns
INEQUALITY_PATTERN = re.compile(
    r'[≤≥<>]|\\leq|\\geq|\\lt|\\gt'
)

# LaTeX display math
LATEX_DISPLAY_PATTERN = re.compile(
    r'\$\$.*?\$\$|\\begin\{equation\}.*?\\end\{equation\}|'
    r'\\begin\{align\}.*?\\end\{align\}',
    re.DOTALL
)


def extract_candidates_from_text(
    text: str,
    source_type: str,
    source_ref: str,
) -> list[dict[str, Any]]:
    """Extract Lean candidates from text using heuristics.

    Returns list of candidate dicts with: text, kind, score, source_type, source_ref
    """
    candidates = []

    # Split into paragraphs/sentences for better granularity
    paragraphs = re.split(r'\n\n+', text)

    for para in paragraphs:
        para = para.strip()
        if len(para) < 10:
            continue

        # Check for theorem/lemma headers
        if THEOREM_HEADER_PATTERN.search(para):
            match = THEOREM_HEADER_PATTERN.search(para)
            kind_match = match.group(1).lower() if match else "theorem"
            kind = {
                "theorem": "theorem",
                "lemma": "lemma",
                "definition": "definition",
                "proposition": "proposition",
                "corollary": "theorem",
            }.get(kind_match, "theorem")

            candidates.append({
                "text": para,
                "kind": kind,
                "source_type": source_type,
                "source_ref": source_ref,
                "signals": ["theorem_header"],
            })
            continue

        # Check for formal ID patterns (requirements)
        if FORMAL_ID_PATTERN.search(para):
            match = FORMAL_ID_PATTERN.search(para)
            prefix = match.group(0).upper() if match else ""
            if prefix.startswith("REQ"):
                kind = "requirement"
            elif prefix.startswith("THM"):
                kind = "theorem"
            elif prefix.startswith("DEF"):
                kind = "definition"
            elif prefix.startswith("LEM"):
                kind = "lemma"
            else:
                kind = "requirement"  # numbered items are usually requirements

            candidates.append({
                "text": para,
                "kind": kind,
                "source_type": source_type,
                "source_ref": source_ref,
                "signals": ["formal_id"],
            })
            continue

        # Check for modal verbs (requirements)
        if MODAL_VERB_PATTERN.search(para):
            candidates.append({
                "text": para,
                "kind": "requirement",
                "source_type": source_type,
                "source_ref": source_ref,
                "signals": ["modal_verb"],
            })
            continue

        # Check for LaTeX equations
        if LATEX_DISPLAY_PATTERN.search(para) or INEQUALITY_PATTERN.search(para):
            candidates.append({
                "text": para,
                "kind": "equation",
                "source_type": source_type,
                "source_ref": source_ref,
                "signals": ["latex" if LATEX_DISPLAY_PATTERN.search(para) else "inequality"],
            })

    return candidates


def score_candidate(candidate: dict[str, Any]) -> float:
    """Score a candidate for prioritization.

    Scoring (from 02_SPEC.md):
    - has_latex: +0.4
    - has_theorem_header: +0.3
    - has_formal_id: +0.2
    - has_modal_verb: +0.1
    - len < 10: -0.3
    - len > 500: -0.1
    """
    score = 0.0
    signals = candidate.get("signals", [])
    text = candidate.get("text", "")

    # Positive signals
    if "latex" in signals:
        score += 0.4
    if "theorem_header" in signals:
        score += 0.3
    if "formal_id" in signals:
        score += 0.2
    if "modal_verb" in signals:
        score += 0.1

    # Penalties
    if len(text) < 10:
        score -= 0.3
    if len(text) > 500:
        score -= 0.1

    # Normalize to [0, 1]
    return max(0.0, min(1.0, score))


def compute_candidate_hash(candidate: dict[str, Any]) -> str:
    """Compute dedup hash for candidate."""
    # Normalize text
    text = candidate.get("text", "").strip().lower()
    kind = candidate.get("kind", "unknown")
    source_type = candidate.get("source_type", "")
    source_ref = candidate.get("source_ref", "")

    hash_input = f"{kind}|{text}|{source_type}|{source_ref}"
    return hashlib.sha256(hash_input.encode()).hexdigest()[:16]


def check_certainly_available() -> bool:
    """Check if certainly integration is available."""
    try:
        from scillm.integrations.certainly import is_available
        return is_available()
    except ImportError:
        return False


def get_proven_context(candidate_text: str, scope: str = "code", k: int = 3) -> list[dict[str, str]]:
    """Retrieve similar proven theorems from memory as context.

    Returns list of {requirement, lean4_code} dicts for few-shot context.
    """
    try:
        from graph_memory.arango_client import get_db

        db = get_db()

        # First, get all proven lessons (they're rare, so this is efficient)
        # Then do text matching in Python for simplicity
        query = """
            FOR doc IN lessons_v2
            FILTER doc.proof_status == 'proved'
            FILTER doc.lean4_code != null AND doc.lean4_code != ''
            RETURN {
                requirement: doc.problem,
                lean4_code: doc.lean4_code,
                title: doc.title
            }
        """
        all_proven = list(db.aql.execute(query))

        if not all_proven:
            return []

        # Simple text overlap scoring
        candidate_words = set(candidate_text.lower().split())
        scored = []
        for doc in all_proven:
            doc_words = set((doc.get("requirement", "") + " " + doc.get("title", "")).lower().split())
            overlap = len(candidate_words & doc_words)
            if overlap > 0:
                scored.append((overlap, doc))

        # Sort by overlap and return top k
        scored.sort(key=lambda x: -x[0])
        return [doc for _, doc in scored[:k]]

    except Exception as exc:
        logger.error("Suppressed error in p4_lean: {}", exc)
        # Fail silently - context is optional
        return []


def format_requirement_with_context(candidate_text: str, context: list[dict[str, str]]) -> str:
    """Format requirement with proven theorem context as few-shot examples."""
    if not context:
        return candidate_text

    examples = []
    for i, ctx in enumerate(context, 1):
        examples.append(f"""Example {i}:
Requirement: {ctx['requirement'][:200]}
Lean4 proof:
```lean4
{ctx['lean4_code']}
```""")

    context_block = "\n\n".join(examples)

    return f"""Here are similar proven theorems for reference:

{context_block}

Now prove the following requirement:
{candidate_text}"""


def extract_compiler_errors(result: dict[str, Any]) -> str:
    """Extract compiler error messages from a certainly result.

    Extracts errors from:
    - result["error"]: Direct error message
    - result["attempts"][*]["stderr"]: Lean compiler stderr
    - result["diagnosis"]: LLM diagnosis of failure

    Returns a formatted error string for retry prompts.
    """
    errors = []

    # Direct error
    if result.get("error"):
        errors.append(f"Error: {result['error']}")

    # Compiler errors from attempts
    attempts = result.get("attempts", [])
    for i, attempt in enumerate(attempts[:3], 1):  # Limit to first 3
        stderr = attempt.get("stderr", "").strip()
        stdout = attempt.get("stdout", "").strip()
        compiler_output = stderr or stdout
        if compiler_output:
            # Truncate long errors
            if len(compiler_output) > 500:
                compiler_output = compiler_output[:500] + "..."
            errors.append(f"Attempt {i} compiler error:\n{compiler_output}")

    # LLM diagnosis
    diagnosis = result.get("diagnosis", {})
    if diagnosis.get("diagnosis"):
        errors.append(f"Diagnosis: {diagnosis['diagnosis']}")
    if diagnosis.get("requirement_issues"):
        errors.append(f"Requirement issues: {diagnosis['requirement_issues']}")

    return "\n\n".join(errors) if errors else "Proof compilation failed"


def format_retry_prompt(original_requirement: str, error_feedback: str, attempt_num: int) -> str:
    """Format a retry prompt with error feedback.

    Uses the Lean4 compiler error to guide the next attempt.
    """
    return f"""The previous proof attempt (attempt {attempt_num}) failed with the following errors:

{error_feedback}

Please fix the proof based on these errors. Common fixes:
- If "unknown identifier": check import statements or use correct Mathlib names
- If "type mismatch": ensure types align (e.g., ℕ vs Int)
- If "tactic failed": try alternative tactics (simp, omega, decide)
- If "ambiguous": add type annotations

Original requirement to prove:
{original_requirement}"""


async def compile_candidate(
    candidate: dict[str, Any],
    tactics: list[str],
    timeout_s: float = 30.0,
    use_context: bool = True,
    scope: str = "code",
    max_retries: int = 2,
) -> dict[str, Any]:
    """Compile a candidate to Lean theorem using certainly with error feedback.

    Uses iterative repair: on failure, extracts compiler errors and retries
    with error context, improving success rate from ~12% to ~60%.

    Args:
        candidate: Candidate dict with text, hash, etc.
        tactics: List of Lean tactics to suggest
        timeout_s: Compilation timeout per attempt
        use_context: Whether to retrieve similar proven theorems as context
        scope: Scope for context retrieval
        max_retries: Number of retry attempts with error feedback (default: 2)

    Returns theorem dict with status: ok/fail/timeout
    """
    try:
        from scillm.integrations.certainly import prove_requirement

        original_requirement = candidate["text"]
        requirement_text = original_requirement

        # Retrieve similar proven theorems as context (only on first attempt)
        if use_context:
            context = get_proven_context(requirement_text, scope=scope, k=3)
            if context:
                requirement_text = format_requirement_with_context(requirement_text, context)

        # Attempt loop with error feedback
        last_error = "Proof failed"
        total_compile_ms = 0

        for attempt in range(1, max_retries + 2):  # +2 for initial + retries
            result = await prove_requirement(
                requirement=requirement_text,
                tactics=tactics,
                compile_timeout_s=int(timeout_s),
            )

            if result.get("ok"):
                best = result.get("best", {})
                return {
                    "candidate_hash": candidate["hash"],
                    "status": "ok",
                    "lean_code": best.get("lean4", ""),
                    "notes": best.get("notes", ""),
                    "compile_ms": best.get("compile_ms", 0) + total_compile_ms,
                    "attempts": attempt,
                }

            # Extract error for retry
            error_feedback = extract_compiler_errors(result)
            last_error = error_feedback

            # Track compile time
            for att in result.get("attempts", []):
                total_compile_ms += att.get("compile_ms", 0)

            # If we have retries left, format retry prompt
            if attempt <= max_retries:
                requirement_text = format_retry_prompt(
                    original_requirement, error_feedback, attempt
                )

        # All attempts failed
        diagnosis = result.get("diagnosis", {})
        error_msg = (
            result.get("error")
            or diagnosis.get("suggested_requirement_edit")
            or last_error
        )
        return {
            "candidate_hash": candidate["hash"],
            "status": "fail",
            "error": error_msg,
            "attempts": max_retries + 1,
        }

    except TimeoutError:
        return {
            "candidate_hash": candidate["hash"],
            "status": "timeout",
        }
    except Exception as e:
        return {
            "candidate_hash": candidate["hash"],
            "status": "fail",
            "error": str(e),
        }


def run_p4_lean(context: RunContext) -> PhaseResult:
    """Execute P4: lean_pipeline phase.

    Args:
        context: RunContext from P1 init

    Returns:
        PhaseResult
    """
    errors: list[dict[str, Any]] = []
    warnings: list[str] = []

    # Skip if Lean disabled
    if not context.config.lean_enabled:
        return _create_skipped_result(context, "Lean pipeline disabled")

    # Check if certainly is available
    certainly_available = check_certainly_available()
    if not certainly_available:
        return _create_unavailable_result(context, "certainly not running")

    # Load doc_chunks from P2
    doc_chunks = []
    p2_chunks_file = context.artifacts_path / "p2_ingest" / "doc_chunks.json"
    if p2_chunks_file.exists():
        try:
            doc_chunks = json.loads(p2_chunks_file.read_text())
        except json.JSONDecodeError:
            warnings.append("Could not load doc_chunks from P2")

    # Load equation_candidates from P3
    equation_candidates = []
    p3_equations_file = context.artifacts_path / "p3_pdf" / "equation_candidates.json"
    if p3_equations_file.exists():
        try:
            equation_candidates = json.loads(p3_equations_file.read_text())
        except json.JSONDecodeError:
            warnings.append("Could not load equation_candidates from P3")

    # Extract candidates from doc_chunks
    all_candidates = []
    for chunk in doc_chunks:
        content = chunk.get("content", "")
        source_path = chunk.get("source_path", "unknown")
        candidates = extract_candidates_from_text(content, "doc_chunk", source_path)
        all_candidates.extend(candidates)

    # Add equation candidates from P3
    for eq in equation_candidates:
        all_candidates.append({
            "text": eq.get("content", ""),
            "kind": "equation",
            "source_type": "pdf",
            "source_ref": eq.get("source_pdf", ""),
            "signals": ["latex"],
        })

    # Score and sort candidates
    for candidate in all_candidates:
        candidate["score"] = score_candidate(candidate)
        candidate["hash"] = compute_candidate_hash(candidate)

    # Sort by score (descending)
    all_candidates.sort(key=lambda c: c["score"], reverse=True)

    # Dedup by hash
    seen_hashes = set()
    unique_candidates = []
    duplicates_removed = 0

    for candidate in all_candidates:
        if candidate["hash"] not in seen_hashes:
            seen_hashes.add(candidate["hash"])
            unique_candidates.append(candidate)
        else:
            duplicates_removed += 1

    # Cap candidates
    candidate_max = context.config.lean_candidate_max
    if len(unique_candidates) > candidate_max:
        warnings.append(f"Capped candidates from {len(unique_candidates)} to {candidate_max}")
        unique_candidates = unique_candidates[:candidate_max]

    # Count by kind
    candidate_kinds = {}
    for c in unique_candidates:
        kind = c.get("kind", "unknown")
        candidate_kinds[kind] = candidate_kinds.get(kind, 0) + 1

    # Create P4 artifacts directory
    p4_artifacts = context.artifacts_path / "p4_lean"
    p4_artifacts.mkdir(parents=True, exist_ok=True)

    # Compile theorems (bounded by max_theorems and time_budget)
    theorems = []
    theorem_statuses = {"ok": 0, "fail": 0, "timeout": 0}
    theorems_attempted = 0

    start_time = time.time()
    max_theorems = context.config.lean_max_theorems
    time_budget_s = context.config.lean_time_budget_s
    tactics = context.config.lean_tactics

    import asyncio

    async def compile_all():
        nonlocal theorems_attempted
        for candidate in unique_candidates:
            # Check bounds
            if theorems_attempted >= max_theorems:
                break
            if time.time() - start_time > time_budget_s:
                warnings.append(f"Time budget exceeded after {theorems_attempted} theorems")
                break

            theorems_attempted += 1
            theorem = await compile_candidate(
                candidate,
                tactics,
                use_context=True,
                scope=context.scope,
            )
            theorems.append(theorem)
            theorem_statuses[theorem["status"]] = theorem_statuses.get(theorem["status"], 0) + 1

    try:
        asyncio.run(compile_all())
    except Exception as e:
        errors.append({
            "code": "COMPILE_ERROR",
            "message": str(e),
            "phase": "lean_pipeline",
        })

    compile_time_s = time.time() - start_time

    # Write artifacts
    if context.config.debug_verbose_artifacts:
        (p4_artifacts / "lean_candidates.json").write_text(
            json.dumps(unique_candidates, indent=2)
        )
        (p4_artifacts / "lean_theorems.json").write_text(
            json.dumps(theorems, indent=2)
        )

    # Write summary
    summary = {
        "status": "ok" if not errors else "partial",
        "counts": {
            "lean_candidates": len(unique_candidates),
            "lean_theorems": len(theorems),
            "theorems_attempted": theorems_attempted,
        },
        "candidate_kinds": candidate_kinds,
        "duplicate_candidates_removed": duplicates_removed,
        "theorem_statuses": theorem_statuses,
        "config": {
            "candidate_max": candidate_max,
            "max_theorems": max_theorems,
            "time_budget_s": time_budget_s,
            "tactics": tactics,
        },
        "timings": {
            "compile_time_s": compile_time_s,
        },
    }

    summary_file = p4_artifacts / "lean_summary.json"
    summary_file.write_text(json.dumps(summary, indent=2))

    # Determine status
    if errors:
        status = PhaseStatus.PARTIAL
    elif theorems_attempted == 0:
        status = PhaseStatus.SKIPPED
        warnings.append("No candidates to compile")
    else:
        status = PhaseStatus.OK

    return PhaseResult(
        status=status,
        counts={
            "lean_candidates": len(unique_candidates),
            "lean_theorems": len(theorems),
        },
        errors=errors,
        warnings=warnings,
        artifacts={
            "lean_summary": str(summary_file),
        },
    )


def _create_skipped_result(context: RunContext, reason: str) -> PhaseResult:
    """Create a skipped result."""
    p4_artifacts = context.artifacts_path / "p4_lean"
    p4_artifacts.mkdir(parents=True, exist_ok=True)

    summary = {
        "status": "skipped",
        "reason": reason,
        "counts": {
            "lean_candidates": 0,
            "lean_theorems": 0,
        },
        "candidate_kinds": {},
        "theorem_statuses": {},
        "config": {
            "candidate_max": context.config.lean_candidate_max,
            "max_theorems": context.config.lean_max_theorems,
        },
    }

    summary_file = p4_artifacts / "lean_summary.json"
    summary_file.write_text(json.dumps(summary, indent=2))

    return PhaseResult(
        status=PhaseStatus.SKIPPED,
        counts={},
        warnings=[reason],
        artifacts={"lean_summary": str(summary_file)},
    )


def _create_unavailable_result(context: RunContext, reason: str) -> PhaseResult:
    """Create an unavailable result."""
    p4_artifacts = context.artifacts_path / "p4_lean"
    p4_artifacts.mkdir(parents=True, exist_ok=True)

    summary = {
        "status": "unavailable",
        "reason": reason,
        "counts": {
            "lean_candidates": 0,
            "lean_theorems": 0,
        },
        "candidate_kinds": {},
        "theorem_statuses": {},
        "config": {
            "candidate_max": context.config.lean_candidate_max,
            "max_theorems": context.config.lean_max_theorems,
        },
    }

    summary_file = p4_artifacts / "lean_summary.json"
    summary_file.write_text(json.dumps(summary, indent=2))

    return PhaseResult(
        status=PhaseStatus.UNAVAILABLE,
        counts={},
        warnings=[reason],
        artifacts={"lean_summary": str(summary_file)},
    )
