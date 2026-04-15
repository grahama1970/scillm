"""P3: pdf_pipeline phase for curate pipeline.

Purpose: Discover PDF URLs, fetch remote PDFs, extract content.

Sub-steps:
1. discover_pdfs: Local PDFs + URLs from doc_chunks
2. fetch_pdfs_remote: Download via fetcher (capped)
3. extract_pdfs: marker-pdf extraction

Assertions (from 02_SPEC.md):
- Local PDFs discovered (All **/*.pdf under code_path minus ignores)
- Remote URLs extracted (From doc_chunks)
- pdf_refs populated (URL + decision: download/skip/cap_reached)
- Remote cap enforced (max pdf_max_remote downloaded)
- Domain allowlist enforced (Only allowed domains unless allow_any_domain)
- pdf_docs created (For successfully fetched PDFs)
- equation_candidates extracted (Best-effort from marker-pdf)
"""

import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from loguru import logger

from graph_memory.codebase.pipeline.types import (
    PhaseResult,
    PhaseStatus,
    RunContext,
)

# URL pattern for PDFs
PDF_URL_PATTERN = re.compile(
    r'https?://[^\s<>"{}|\\^`\[\]]+\.pdf(?:\?[^\s<>"{}|\\^`\[\]]*)?',
    re.IGNORECASE
)

# arXiv patterns
ARXIV_PATTERN = re.compile(
    r'https?://arxiv\.org/(?:abs|pdf)/(\d+\.\d+)',
    re.IGNORECASE
)


def discover_local_pdfs(code_path: Path, ignore_patterns: set[str]) -> list[Path]:
    """Discover local PDF files."""
    pdfs = []
    for pdf in code_path.rglob("*.pdf"):
        # Simple ignore check
        rel_path = pdf.relative_to(code_path)
        skip = False
        for part in rel_path.parts:
            if part in ignore_patterns:
                skip = True
                break
        if not skip:
            pdfs.append(pdf)
    return pdfs


def extract_pdf_urls(doc_chunks: list[dict[str, Any]]) -> list[str]:
    """Extract PDF URLs from doc chunks."""
    urls = set()

    for chunk in doc_chunks:
        content = chunk.get("content", "")

        # Find direct PDF URLs
        for match in PDF_URL_PATTERN.finditer(content):
            urls.add(match.group(0))

        # Find arXiv URLs and convert to PDF URLs
        for match in ARXIV_PATTERN.finditer(content):
            arxiv_id = match.group(1)
            urls.add(f"https://arxiv.org/pdf/{arxiv_id}.pdf")

    return list(urls)


def check_domain_allowed(url: str, allowed_domains: list[str], allow_any: bool) -> bool:
    """Check if URL domain is in allowlist."""
    if allow_any:
        return True

    parsed = urlparse(url)
    domain = parsed.netloc.lower()

    for allowed in allowed_domains:
        if domain == allowed or domain.endswith(f".{allowed}"):
            return True

    return False


def fetch_pdf(url: str, dest_path: Path, timeout: int = 90) -> bool:
    """Fetch a PDF from URL using urllib (no subprocess)."""
    import urllib.request
    import ssl

    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            data = resp.read()
            if len(data) > 0:
                dest_path.write_bytes(data)
                return True
    except Exception as exc:
        logger.error("Suppressed error in p3_pdf: {}", exc)

    return False


def extract_pdf_content(pdf_path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Extract content from PDF using marker-pdf via extractor.

    Returns:
        (extracted_blocks, errors) - all text blocks with metadata
    """
    extracted = []
    errors = []

    try:
        from extractor.pipeline.steps.s02_marker_extractor import extract_blocks

        blocks, _predictors = extract_blocks(pdf_path)

        # blocks are dicts with keys: block_type, page_idx, page, text, first_span_font, bbox, etc.
        for block in blocks:
            text = block.get("text", "").strip()
            if not text or len(text) < 5:
                continue

            block_type = block.get("block_type", "Text")
            font_info = block.get("first_span_font", {})

            extracted.append({
                "content": text,
                "source_pdf": str(pdf_path),
                "block_type": block_type,
                "page": block.get("page", 0),
                "font_name": font_info.get("name", ""),
                "font_size": font_info.get("size", 0),
                "is_bold": font_info.get("bold", False),
            })

    except ImportError:
        errors.append("extractor/marker not available")
    except Exception as e:
        errors.append(f"Error extracting from {pdf_path}: {e}")

    return extracted, errors


def compute_pdf_hash(pdf_path: Path) -> str:
    """Compute SHA256 hash of PDF file."""
    sha256 = hashlib.sha256()
    with open(pdf_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def run_p3_pdf(context: RunContext) -> PhaseResult:
    """Execute P3: pdf_pipeline phase.

    Args:
        context: RunContext from P1 init

    Returns:
        PhaseResult
    """
    errors: list[dict[str, Any]] = []
    warnings: list[str] = []
    domain_violations: list[str] = []

    # Skip if PDF disabled
    if not context.config.pdf_enabled:
        return _create_skipped_result(context, "PDF pipeline disabled")

    # Use worktree or code_path
    scan_path = context.worktree_path or context.code_path

    # Load ignore patterns (reuse from P2 if available)
    ignore_patterns: set[str] = {".git", "__pycache__", "node_modules", ".venv"}

    # Discover local PDFs
    local_pdfs = discover_local_pdfs(scan_path, ignore_patterns)

    # Load doc_chunks from P2 if available
    doc_chunks = []
    p2_chunks_file = context.artifacts_path / "p2_ingest" / "doc_chunks.json"
    if p2_chunks_file.exists():
        try:
            doc_chunks = json.loads(p2_chunks_file.read_text())
        except json.JSONDecodeError:
            warnings.append("Could not load doc_chunks from P2")

    # Extract PDF URLs from doc chunks
    pdf_urls = extract_pdf_urls(doc_chunks)

    # Create pdf_refs
    pdf_refs = []
    for pdf_path in local_pdfs:
        pdf_refs.append({
            "type": "local",
            "path": str(pdf_path),
            "decision": "process",
        })

    remote_downloaded = 0
    for url in pdf_urls:
        # Check domain allowlist
        if not check_domain_allowed(
            url, context.config.pdf_remote_domains, context.config.pdf_allow_any_domain
        ):
            domain_violations.append(url)
            pdf_refs.append({
                "type": "remote",
                "url": url,
                "decision": "skip_domain",
            })
            continue

        # Check cap
        if remote_downloaded >= context.config.pdf_max_remote:
            pdf_refs.append({
                "type": "remote",
                "url": url,
                "decision": "cap_reached",
            })
            continue

        # Mark for download
        pdf_refs.append({
            "type": "remote",
            "url": url,
            "decision": "download",
        })

    # Create P3 artifacts directory
    p3_artifacts = context.artifacts_path / "p3_pdf"
    p3_artifacts.mkdir(parents=True, exist_ok=True)
    pdfs_dir = p3_artifacts / "pdfs"
    pdfs_dir.mkdir(exist_ok=True)

    # Process PDFs
    pdf_docs = []
    all_equations = []
    extraction_errors = []

    # Process local PDFs
    for pdf_path in local_pdfs:
        try:
            pdf_hash = compute_pdf_hash(pdf_path)
            pdf_docs.append({
                "hash": pdf_hash,
                "source": "local",
                "path": str(pdf_path),
            })

            # Extract content
            equations, eq_errors = extract_pdf_content(pdf_path)
            all_equations.extend(equations)
            extraction_errors.extend(eq_errors)
        except Exception as e:
            extraction_errors.append(f"Error processing {pdf_path}: {e}")

    # Fetch and process remote PDFs
    if context.config.pdf_remote:
        for ref in pdf_refs:
            if ref.get("decision") != "download":
                continue

            url = ref["url"]
            temp_pdf = pdfs_dir / f"remote_{remote_downloaded}.pdf"

            if fetch_pdf(url, temp_pdf, context.config.pdf_timeout_s):
                remote_downloaded += 1
                try:
                    pdf_hash = compute_pdf_hash(temp_pdf)
                    pdf_docs.append({
                        "hash": pdf_hash,
                        "source": "remote",
                        "url": url,
                        "local_path": str(temp_pdf),
                    })

                    # Extract content
                    equations, eq_errors = extract_pdf_content(temp_pdf)
                    all_equations.extend(equations)
                    extraction_errors.extend(eq_errors)
                except Exception as e:
                    extraction_errors.append(f"Error processing {url}: {e}")
            else:
                warnings.append(f"Failed to fetch {url}")

    # Check if we have anything to process
    if not local_pdfs and not pdf_urls:
        return _create_skipped_result(context, "No PDFs to process")

    # Write artifacts
    if context.config.debug_verbose_artifacts:
        (p3_artifacts / "pdf_refs.json").write_text(json.dumps(pdf_refs, indent=2))
        (p3_artifacts / "pdf_docs.json").write_text(json.dumps(pdf_docs, indent=2))
        (p3_artifacts / "equation_candidates.json").write_text(json.dumps(all_equations, indent=2))

    # Write summary
    summary = {
        "status": "ok" if not errors else "partial",
        "counts": {
            "pdf_refs": len(pdf_refs),
            "local_pdfs": len(local_pdfs),
            "remote_urls": len(pdf_urls),
            "remote_downloaded": remote_downloaded,
            "pdf_docs": len(pdf_docs),
            "equation_candidates": len(all_equations),
        },
        "config": {
            "pdf_max_remote": context.config.pdf_max_remote,
            "allow_any_domain": context.config.pdf_allow_any_domain,
            "remote_domains": context.config.pdf_remote_domains,
        },
        "domain_violations": domain_violations,
        "extraction_errors": extraction_errors,
    }

    summary_file = p3_artifacts / "pdf_summary.json"
    summary_file.write_text(json.dumps(summary, indent=2))

    # Determine status
    if extraction_errors:
        status = PhaseStatus.PARTIAL
    else:
        status = PhaseStatus.OK

    return PhaseResult(
        status=status,
        counts={
            "pdf_refs": len(pdf_refs),
            "pdf_docs": len(pdf_docs),
            "equation_candidates": len(all_equations),
        },
        errors=errors,
        warnings=warnings + extraction_errors,
        artifacts={
            "pdf_summary": str(summary_file),
        },
    )


def _create_skipped_result(context: RunContext, reason: str) -> PhaseResult:
    """Create a skipped result."""
    p3_artifacts = context.artifacts_path / "p3_pdf"
    p3_artifacts.mkdir(parents=True, exist_ok=True)

    summary = {
        "status": "skipped",
        "reason": reason,
        "counts": {
            "pdf_refs": 0,
            "local_pdfs": 0,
            "remote_downloaded": 0,
            "pdf_docs": 0,
            "equation_candidates": 0,
        },
        "config": {
            "pdf_max_remote": context.config.pdf_max_remote,
            "allow_any_domain": context.config.pdf_allow_any_domain,
        },
        "domain_violations": [],
    }

    summary_file = p3_artifacts / "pdf_summary.json"
    summary_file.write_text(json.dumps(summary, indent=2))

    return PhaseResult(
        status=PhaseStatus.SKIPPED,
        counts={},
        warnings=[reason],
        artifacts={"pdf_summary": str(summary_file)},
    )
