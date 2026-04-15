from __future__ import annotations
import time
import json
import re
import io
import urllib.request
import urllib.parse
from typing import List, Dict, Any, Tuple
import asyncio as _asyncio
import typer
from loguru import logger

from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view
from .arxiv_ingest import _fetch_arxiv
from ..events import log_event

app = typer.Typer(add_completion=False)


def _tokenize(s: str) -> List[str]:
    return [t.lower() for t in re.findall(r"[A-Za-z0-9_\-]+", s or "")]


def _keyword_score(q: str, title: str, summary: str) -> float:
    qt = set(_tokenize(q))
    tt = set(_tokenize(title))
    st = set(_tokenize(summary))
    if not qt:
        return 0.0
    overlap = len(qt & (tt | st)) / max(1, len(qt))
    # titles weigh more
    t_overlap = len(qt & tt) / max(1, len(qt))
    return 0.6 * t_overlap + 0.4 * overlap


def _maybe_embed_score(q: str, texts: List[str]) -> List[float] | None:
    try:
        from sentence_transformers import SentenceTransformer
        import numpy as np
    except Exception as exc:
        logger.error("sentence-transformers/numpy not available: {}", exc)
        return None
    try:
        model_id = 'all-MiniLM-L6-v2'
        model = SentenceTransformer(model_id)
        arr = model.encode([q] + texts, convert_to_numpy=True, normalize_embeddings=True)
        qv = arr[0]
        vs = arr[1:]
        sims = (vs @ qv)
        return sims.tolist()
    except Exception as exc:
        logger.error("Embedding score computation failed: {}", exc)
        return None


PRESTIGE_HINTS = [
    'stanford', 'mit', 'massachusetts institute of technology', 'berkeley', 'uc berkeley',
    'carnegie mellon', 'cmu', 'harvard', 'oxford', 'cambridge', 'princeton', 'eth zurich',
    'tsinghua', 'pku', 'peking university', 'google', 'deepmind', 'meta', 'microsoft', 'openai'
]


def _prestige_bonus(authors: List[Dict[str, Any]]) -> float:
    txt = ' '.join([(a.get('affiliation') or '') + ' ' + (a.get('name') or '') for a in authors]).lower()
    for hint in PRESTIGE_HINTS:
        if hint in txt:
            return 0.15
    if 'alabama state' in txt:
        return -0.05
    return 0.0


def _rank_papers(q: str, papers: List[Dict[str, Any]]) -> List[Tuple[float, Dict[str, Any]]]:
    # Combine keyword score and optional embedding score
    kw = [(_keyword_score(q, p.get('title') or '', p.get('summary') or ''), i) for i, p in enumerate(papers)]
    texts = [ (p.get('title') or '') + '\n' + (p.get('summary') or '') for p in papers ]
    es = _maybe_embed_score(q, texts)
    ranked: List[Tuple[float, Dict[str, Any]]] = []
    for idx, p in enumerate(papers):
        s_kw = kw[idx][0]
        s_emb = es[idx] if es is not None and idx < len(es) else 0.0
        bonus = _prestige_bonus(p.get('authors') or [])
        cross = 0.05 if len(p.get('categories') or []) > 1 else 0.0
        score = 0.6 * s_kw + 0.35 * s_emb + bonus + cross
        ranked.append((float(score), p))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return ranked


def _derive_pdf_url(arxiv_id: str) -> str:
    """Normalize id/URL and derive PDF URL.
    Accepts full URLs (abs/pdf/html) or bare ids with or without version.
    """
    try:
        s = (arxiv_id or '').strip()
        tail = s
        if s.startswith('http://') or s.startswith('https://'):
            # strip trailing slash and .pdf
            tail = s.rstrip('/').split('/')[-1]
            if tail.endswith('.pdf'):
                tail = tail[:-4]
        if not tail:
            return ''
        return f"https://arxiv.org/pdf/{tail}.pdf"
    except Exception as exc:
        logger.error("Failed to derive PDF URL from {!r}: {}", arxiv_id, exc)
        return ''


def _download_pdf(url: str, timeout: int = 20) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


def _run_cmd_sync(args: List[str], *, timeout: int = 60, env: Dict[str, str] | None = None, check: bool = False) -> tuple[int, str, str]:
    async def _run():
        proc = await _asyncio.create_subprocess_exec(
            args[0], *args[1:],
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
            env={**os.environ, **(env or {})},
        )
        try:
            stdout, stderr = await _asyncio.wait_for(proc.communicate(), timeout=timeout)
        except _asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception as exc:
                logger.error("Failed to kill timed-out subprocess: {}", exc)
            return 124, "", "timeout"
        rc = proc.returncode or 0
        return rc, stdout.decode(errors='ignore'), stderr.decode(errors='ignore')

    try:
        rc, out, err = _asyncio.run(_run())
    except RuntimeError:
        loop = _asyncio.new_event_loop()
        try:
            _asyncio.set_event_loop(loop)
            rc, out, err = loop.run_until_complete(_run())
        finally:
            loop.close()
    if check and rc != 0:
        raise RuntimeError(f"cmd failed rc={rc}: {err[:200] or out[:200]}")
    return rc, out, err


def _extract_ideas_from_pdf(data: bytes, max_pages: int = 3) -> List[str]:
    try:
        import fitz  # PyMuPDF
    except Exception as exc:
        logger.error("PyMuPDF not available for idea extraction: {}", exc)
        return []
    try:
        doc = fitz.open(stream=io.BytesIO(data).read(), filetype='pdf')
        text_chunks: List[str] = []
        for i in range(min(max_pages, doc.page_count)):
            page = doc.load_page(i)
            text_chunks.append(page.get_text('text'))
        text = '\n'.join(text_chunks)
    except Exception as exc:
        logger.error("PDF text extraction failed in _extract_ideas_from_pdf: {}", exc)
        return []
    # Simple heuristic extraction: sentences containing key verbs/nouns
    ideas: List[str] = []
    for line in re.split(r"(?<=[.!?])\s+", text):
        low = line.lower()
        if any(k in low for k in ("we propose", "we present", "we introduce", "our method", "graph", "agent", "retrieval", "memory")):
            ideas.append(line.strip())
        if len(ideas) >= 6:
            break
    return ideas


def _extract_pdf_info(data: bytes, max_pages: int = 3) -> Dict[str, Any]:
    """Return lightweight info from a PDF for storage as knowledge chunks.

    Returns
    - ideas: list[str] key statements
    - page_count: int
    - text_excerpt: short excerpt (first ~1000 chars) for debugging/transparency
    """
    info: Dict[str, Any] = {
        'ideas': [],
        'page_count': 0,
        'text_excerpt': '',
        'text': '',
        # Light-weight, best-effort extractions for testable artifacts
        'algorithms': [],
        'code_snippets': [],
        'equations': [],
        'tables': [],
    }
    try:
        import fitz  # PyMuPDF
    except Exception as exc:
        logger.error("PyMuPDF not available for PDF info extraction: {}", exc)
        return info
    try:
        doc = fitz.open(stream=io.BytesIO(data).read(), filetype='pdf')
        info['page_count'] = int(doc.page_count)
        text_chunks: List[str] = []
        for i in range(min(max_pages, doc.page_count)):
            page = doc.load_page(i)
            text_chunks.append(page.get_text('text'))
        text = '\n'.join(text_chunks)
        info['text_excerpt'] = text[:1000]
        info['text'] = text
        ideas: List[str] = []
        for line in re.split(r"(?<=[.!?])\s+", text):
            low = line.lower()
            if any(k in low for k in ("we propose", "we present", "we introduce", "our method", "graph", "agent", "retrieval", "memory")):
                ideas.append(line.strip())
            if len(ideas) >= 6:
                break
        info['ideas'] = ideas
        # Heuristic: extract small "Algorithm" blocks and code-like runs
        # Keep extremely conservative (low false positives), limit counts
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        algos: List[str] = []
        code_snips: List[str] = []
        eqs: List[str] = []
        tables: List[str] = []
        # Simple table detector: lines with 'Table' headings, markdown-like '|' columns,
        # or >=3 columns separated by 2+ spaces across 3+ consecutive lines.
        def _count_cols(ln: str) -> int:
            if '|' in ln:
                return max(0, len([t for t in ln.split('|') if t.strip()]))
            parts = [p for p in re.split(r"\s{2,}", ln) if p]
            return len(parts)
        def _collect_block(start: int) -> Tuple[int, str]:
            block: List[str] = []
            j = start
            # Stop on blank lines or new section markers
            while j < len(lines) and len(block) < 12:
                ln2 = lines[j]
                if not ln2 or re.match(r"^(figure|algorithm|section)\b", ln2.lower()):
                    break
                # Require consistent columns if using whitespace separation
                c = _count_cols(ln2)
                if c >= 3 or '|' in ln2 or (j == start and ln2.lower().startswith('table')):
                    block.append(ln2)
                    j += 1
                else:
                    break
            return j, '\n'.join(block)
        i = 0
        while i < len(lines) and (len(algos) < 3 or len(code_snips) < 3 or len(tables) < 3):
            ln = lines[i]
            low = ln.lower()
            # Algorithm blocks often start with "Algorithm" and contain Input/Output lines
            if len(algos) < 3 and low.startswith('algorithm'):
                block = [ln]
                j = i + 1
                saw_io = False
                while j < len(lines) and len(block) < 12:
                    if re.match(r"^(figure|table)\b", lines[j].lower()):
                        break
                    block.append(lines[j])
                    if re.search(r"\b(input|output)\s*[:]", lines[j].lower()):
                        saw_io = True
                    j += 1
                if saw_io and len(block) >= 2:
                    algos.append('\n'.join(block))
                    i = j
                    continue
            # Code-like snippet: many braces/semicolons/keywords in a short window
            if len(code_snips) < 3:
                window = [lines[i]] + lines[i+1:i+6]
                joined = '\n'.join(window)
                sym_ratio = sum(ch in '{};()[]<>=:' for ch in joined) / max(1, len(joined))
                has_kw = any(k in joined for k in ('def ', 'class ', 'return ', 'for ', 'while ', 'function ', 'let ', 'const ', 'import '))
                if sym_ratio > 0.03 and has_kw:
                    code_snips.append('\n'.join(window[: min(10, len(window))]))
                    i += len(window)
                    continue
            # Equations (best-effort): lines with multiple math symbols/operators
            if len(eqs) < 3:
                ops = sum(ch in '±×÷≤≥∞∑∂∇λμθϕστπαβγδ_^=+−/*' for ch in ln)
                if ops >= 3 and any(op in ln for op in ('=', '∑', '^', '_')):
                    eqs.append(ln)
            # Tables: prefer markdown/piped or multi-space columns; include lines after heading
            if len(tables) < 3 and (low.startswith('table') or '|' in ln or _count_cols(ln) >= 3):
                j, blk = _collect_block(i)
                if blk and len(blk.splitlines()) >= 3:
                    tables.append(blk)
                    i = j
                    continue
            i += 1
        info['algorithms'] = algos
        info['code_snippets'] = code_snips
        info['equations'] = eqs
        info['tables'] = tables
    except Exception as exc:
        logger.error("PDF info extraction failed: {}", exc)
        return info
    return info


def _strong_extract_if_enabled(data: bytes) -> Dict[str, Any] | None:
    """Attempt high-fidelity extraction via Extractor pipeline when enabled.

    Controlled by ARXIV_STRONG_EXTRACT=1. Returns a dict with optional keys
    { text, tables } or None when unavailable. Any failure logs a memory_event
    and returns None to keep the Happy Path intact.
    """
    import os as _os, sys as _sys, tempfile as _tmp, subprocess as _sp, shutil as _sh
    if _os.getenv('ARXIV_STRONG_EXTRACT') not in ('1','true','TRUE'):
        return None
    # Optional CLI route (preferred for isolation)
    if _os.getenv('ARXIV_STRONG_USE_CLI') in ('1','true','TRUE'):
        mode = (_os.getenv('ARXIV_STRONG_MODE') or 'accurate').strip().lower()
        try:
            with _tmp.TemporaryDirectory() as td:
                pdf_path = _os.path.join(td, 'input.pdf')
                res_dir = _os.path.join(td, 'results')
                with open(pdf_path, 'wb') as f:
                    f.write(data)
                cmd = ['lessons-extractor', 'run', '--pdf', pdf_path, '--results', res_dir, '--mode', mode]
                # run and ignore non-zero (fallback below)
                _run_cmd_sync(cmd, timeout=int(_os.getenv('ARXIV_STRONG_TIMEOUT','90') or '90'), check=False)
                # try final_report.json then final_report.md
                jr = _os.path.join(res_dir, 'final_report.json')
                mr = _os.path.join(res_dir, 'final_report.md')
                out: Dict[str, Any] = {}
                if _os.path.exists(jr):
                    try:
                        with open(jr, 'r', encoding='utf-8', errors='ignore') as fh:
                            j = json.load(fh)
                            if isinstance(j, dict):
                                # Best-effort extraction
                                txt = j.get('text') or j.get('summary') or ''
                                if txt:
                                    out['text'] = txt
                                if isinstance(j.get('tables'), list):
                                    out['tables'] = j['tables'][:3]
                                if out:
                                    return out
                    except Exception as exc:
                        logger.error("Failed to parse final_report.json: {}", exc)
                if _os.path.exists(mr):
                    try:
                        with open(mr, 'r', encoding='utf-8', errors='ignore') as fh:
                            txt = fh.read()
                            if txt:
                                out['text'] = txt[:200000]
                                return out
                    except Exception as exc:
                        logger.error("Failed to read final_report.md: {}", exc)
        except Exception as exc:
            logger.error("Strong extract CLI route failed, falling back to import: {}", exc)
    try:
        # Allow overriding extractor root; otherwise try the default experiments/extractor path
        root = _os.getenv('EXTRACTOR_ROOT') or '/home/graham/workspace/experiments/extractor'
        if root and root not in _sys.path:
            _sys.path.insert(0, root)
        # Late import to avoid dependency costs
        from extractor.pipeline import steps as ex_steps  # type: ignore
    except Exception as exc:
        logger.error("Extractor pipeline not importable: {}", exc)
        try:
            db = get_db()
            log_event(db, 'strong_extract_unavailable', 'Extractor pipeline not importable', {})
        except Exception as exc2:
            logger.error("Failed to log strong_extract_unavailable event: {}", exc2)
        return None
    try:
        # Minimal adapter: call a hypothetical table/text step if present
        # This is intentionally defensive: if steps are missing, we fallback.
        res: Dict[str, Any] = {}
        if hasattr(ex_steps, 'extract_tables_from_pdf_bytes'):
            tables = ex_steps.extract_tables_from_pdf_bytes(data)  # type: ignore[attr-defined]
            if tables:
                res['tables'] = tables[:3]
        if hasattr(ex_steps, 'extract_text_from_pdf_bytes') and 'text' not in res:
            txt = ex_steps.extract_text_from_pdf_bytes(data, max_pages=8)  # type: ignore[attr-defined]
            if txt:
                res['text'] = txt
        return res if res else None
    except Exception as e:
        try:
            db = get_db()
            log_event(db, 'strong_extract_failed', 'Extractor pipeline failed', {'error': str(e)})
        except Exception as exc:
            logger.error("Failed to log strong_extract_failed event: {}", exc)
        return None


def _extract_references_from_pdf(data: bytes, max_pages: int = 8) -> List[str]:
    try:
        import fitz
    except Exception as exc:
        logger.error("PyMuPDF not available for reference extraction: {}", exc)
        return []
    try:
        doc = fitz.open(stream=io.BytesIO(data).read(), filetype='pdf')
        text_chunks: List[str] = []
        for i in range(min(max_pages, doc.page_count)):
            page = doc.load_page(i)
            text_chunks.append(page.get_text('text'))
        text = '\n'.join(text_chunks)
    except Exception as exc:
        logger.error("PDF text extraction failed in _extract_references_from_pdf: {}", exc)
        return []
    refs: List[str] = []
    for m in re.findall(r"arXiv[:\s]?([0-9]{4}\.[0-9]{4,5})(v\d+)?", text, flags=re.IGNORECASE):
        rid = m[0]
        if rid not in refs:
            refs.append(rid)
    for m in re.findall(r"10\.[0-9]{4,9}/[-._;()/:A-Z0-9]+", text, flags=re.IGNORECASE):
        doi = m.strip()
        if doi not in refs:
            refs.append(doi)
    return refs


@app.command()
def research(
    q: str = typer.Option(..., help="Topic query"),
    scope: str = typer.Option("research", help="Scope to write lessons"),
    max_results: int = typer.Option(10),
    pdf_top: int = typer.Option(3, help="How many top-ranked PDFs to download"),
    json_out: bool = typer.Option(True, "--json", help="Output JSON envelope"),
):
    ensure_collections_and_view()
    db = get_db()
    t0 = time.time()
    papers = _fetch_arxiv(q=q, max_results=max_results)
    ranked = _rank_papers(q, papers)
    selected = [p for _, p in ranked]
    results: List[Dict[str, Any]] = []
    ts = int(time.time())
    for i, p in enumerate(selected):
        lid = p.get('id') or ''
        title = p.get('title') or ''
        atitle = (f"ARXIV[{lid.split('/')[-1]}] " if lid else "ARXIV ") + title
        problem = p.get('summary') or ''
        cats = p.get('categories') or []
        authors = p.get('authors') or []
        link = p.get('pdf') or p.get('link') or ''
        ideas: List[str] = []
        refs: List[str] = []
        downloaded = False
        page_count = 0
        pdf_url = link
        # Procedural marker from title heuristics (how-to/fix/troubleshooting/stability)
        t_low = (title or '').strip().lower()
        procedural = t_low.startswith('how to') or t_low.startswith('fix') or ('troubleshooting' in t_low) or ('stability' in t_low)
        if i < max(0, int(pdf_top)):
            if not pdf_url or (not pdf_url.endswith('.pdf')):
                pdf_url = _derive_pdf_url(lid)
            if pdf_url:
                try:
                    data = _download_pdf(pdf_url)
                    downloaded = True
                    # Strong extract (operator-only) first, fallback to lightweight
                    strong = _strong_extract_if_enabled(data)
                    info = _extract_pdf_info(data, max_pages=3)
                    ideas = info.get('ideas') or []
                    page_count = int(info.get('page_count') or 0)
                    refs = _extract_references_from_pdf(data, max_pages=8)
                    # Optional: store additional extracted text in separate collection
                    import os as _os
                    if _os.getenv('ARXIV_STORE_TEXT', '1').lower() not in ('0','false','no'):
                        try:
                            # extract a few more pages for text store
                            max_pages_text = int(_os.getenv('ARXIV_TEXT_MAX_PAGES', '8'))
                            info2 = _extract_pdf_info(data, max_pages=max_pages_text)
                            text_full = (info2.get('text') or '')
                            max_chars = int(_os.getenv('ARXIV_TEXT_MAX_CHARS', '200000'))
                            text_full = text_full[:max_chars]
                        except Exception as exc:
                            logger.error("Failed to extract extended text for arxiv store: {}", exc)
                            text_full = ''
                    else:
                        text_full = ''
                    # Merge strong artifacts when present (no duplicates, small)
                    if strong:
                        try:
                            if strong.get('tables'):
                                doc.setdefault('tables', [])
                                doc['tables'] = (doc['tables'] or []) + list(strong['tables'])[:3]
                            if strong.get('text') and not locals().get('text_full'):
                                txt = str(strong.get('text'))
                                max_chars = int(_os.getenv('ARXIV_TEXT_MAX_CHARS', '200000'))
                                text_full = txt[:max_chars]
                        except Exception as exc:
                            logger.error("Failed to merge strong extract artifacts: {}", exc)
                except Exception as exc:
                    logger.error("PDF download/processing failed for {}: {}", pdf_url, exc)
                    downloaded = False
        playbook_lines = [f"Read: {link or lid}"]
        if ideas:
            playbook_lines.extend([f"Idea: {s}" for s in ideas[:6]])
        playbook = '- ' + '\n- '.join(playbook_lines)
        doc = {
            'title': atitle,
            'problem': problem,
            'playbook': playbook,
            'tags': ['arxiv'] + cats,
            'keywords': ' '.join(cats),
            'scope': scope,
            'status': 'active',
            'added_by': 'agent',
            'updated_at': ts,
            'demo': False,
            'source': 'arxiv',
            'downloaded_pdf': downloaded,
            'pdf_url': pdf_url or '',
            'pdf_page_count': page_count,
            'pdf_excerpt': (info.get('text_excerpt') if downloaded else '') if 'info' in locals() else '',
            'chunks': ideas,
            'procedural': procedural,
            'authors': authors,
            'references': refs,
            # New: testable artifact fields (kept small and optional)
            'algorithms': (info.get('algorithms') if downloaded else []) if 'info' in locals() else [],
            'code_snippets': (info.get('code_snippets') if downloaded else []) if 'info' in locals() else [],
            'equations': (info.get('equations') if downloaded else []) if 'info' in locals() else [],
            'tables': (info.get('tables') if downloaded else []) if 'info' in locals() else [],
        }
        res = list(db.aql.execute(
            "UPSERT { title:@t, scope:@s } INSERT @d UPDATE @d IN lessons_v2 RETURN NEW",
            bind_vars={'t': atitle, 's': scope, 'd': doc},
        ))
        if res:
            results.append({'arxiv_id': lid, 'lesson': res[0].get('_id'), 'downloaded_pdf': downloaded})
            # Store text if enabled
            try:
                if downloaded and (locals().get('text_full') or ''):
                    from .text_store import store_text
                    store_text(db, res[0].get('_id'), scope, locals().get('text_full') or '', page_count=page_count, source='arxiv')
            except Exception as exc:
                logger.error("Failed to store arxiv text for {}: {}", lid, exc)
    took = int((time.time() - t0) * 1000)
    out = {'meta': {'q': q, 'scope': scope, 'count': len(results), 'took_ms': took}, 'items': results, 'errors': []}
    print(json.dumps(out, ensure_ascii=False))

@app.command()
def text(
    q: str = typer.Option("", help="arXiv query or id; ignored if url is provided"),
    url: str = typer.Option("", help="arXiv URL (abs/pdf/html) or id"),
    max_results: int = typer.Option(1, help="How many candidates to consider before picking the best"),
    max_chars: int = typer.Option(0, "--max-chars", help="Clamp returned text length (0 = no clamp)"),
    json_out: bool = typer.Option(True, "--json", help="Output JSON envelope"),
):
    """Download the paper PDF and return full extracted text (no writes)."""
    t0 = time.time()
    errors: List[str] = []
    try:
        query = (url or q or "").strip()
        if not query:
            raise ValueError("missing q or url")
        papers = _fetch_arxiv(q=query, max_results=max_results)
        pick = None
        if papers:
            ranked = _rank_papers(q or "", papers)
            pick = (ranked[0][1] if ranked else papers[0])
        if not pick:
            raise RuntimeError("no results")
        lid = pick.get('id') or ''
        pdf_url = pick.get('pdf') or pick.get('link') or ''
        if not pdf_url or (not pdf_url.endswith('.pdf')):
            pdf_url = _derive_pdf_url(lid)
        if not pdf_url:
            raise RuntimeError("no pdf url")
        data = _download_pdf(pdf_url)
        try:
            import fitz  # PyMuPDF
        except Exception as e:
            raise RuntimeError(f"PyMuPDF not available: {e}")
        doc = fitz.open(stream=io.BytesIO(data).read(), filetype='pdf')
        chunks: List[str] = []
        for i in range(int(doc.page_count)):
            page = doc.load_page(i)
            chunks.append(page.get_text('text'))
        full_text = '\n'.join(chunks)
        if max_chars and max_chars > 0:
            full_text = full_text[:int(max_chars)]
        out = {
            'meta': { 'q': q, 'url': url, 'took_ms': int((time.time()-t0)*1000) },
            'items': [{ 'arxiv_id': (lid.split('/')[-1] if lid else ''), 'pdf_url': pdf_url, 'page_count': int(doc.page_count), 'text': full_text }],
            'errors': [],
        }
        if json_out:
            print(json.dumps(out, ensure_ascii=False))
        else:
            typer.echo(full_text)
    except Exception as e:
        errors.append(str(e))
        out = { 'meta': { 'q': q, 'url': url }, 'items': [], 'errors': errors }
        if json_out:
            print(json.dumps(out, ensure_ascii=False))
        else:
            typer.echo("error: " + "; ".join(errors))
