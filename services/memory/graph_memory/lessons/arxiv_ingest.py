from __future__ import annotations
import time
import json
import os
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError
from pathlib import Path
import xml.etree.ElementTree as ET
import typer

from ..arango_client import get_db
from ..setup_schema import ensure_collections_and_view
from loguru import logger

app = typer.Typer(add_completion=False)

_REQ_TIMES: list[float] = []

def _log_error(msg: str) -> None:
    try:
        logdir = Path('.artifacts/logs')
        logdir.mkdir(parents=True, exist_ok=True)
        with (logdir / 'arxiv.log').open('a', encoding='utf-8') as f:
            f.write(msg.rstrip() + "\n")
    except Exception as exc:
        logger.error("_log_error write failed: {exc}", exc=exc)


def _query_arxiv(search_query: str | None, start: int, max_results: int, sortBy: str | None = None, sortOrder: str | None = None, *, id_list: str | None = None) -> bytes:
    base = "https://export.arxiv.org/api/query"
    params = {
        "start": max(0, int(start)),
        "max_results": max(1, int(max_results)),
    }
    if id_list:
        params["id_list"] = id_list
    else:
        params["search_query"] = search_query or "all:"
    if sortBy:
        params["sortBy"] = sortBy
    if sortOrder:
        params["sortOrder"] = sortOrder
    url = base + "?" + urllib.parse.urlencode(params)
    # Basic rate limit: ARXIV_MAX_REQ_PER_MIN (default 30)
    try:
        import time
        max_rpm = int(os.environ.get("ARXIV_MAX_REQ_PER_MIN", "30") or "30")
        min_interval = 60.0 / max(1, max_rpm)
        now = time.time()
        if _REQ_TIMES and (now - _REQ_TIMES[-1]) < min_interval:
            time.sleep(min_interval - (now - _REQ_TIMES[-1]))
        req = urllib.request.Request(url, headers={
            "User-Agent": "GraphMemory/0.2 (+https://github.com/grahama1970/graph-memory-operator)",
            "Accept": "application/atom+xml"
        })
        attempt = 0
        while True:
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = resp.read()
                    _REQ_TIMES.append(time.time())
                    # keep last 100 timestamps
                    if len(_REQ_TIMES) > 100:
                        del _REQ_TIMES[:-100]
                    return data
            except HTTPError as he:
                if he.code in (429, 500, 502, 503, 504) and attempt < 3:
                    # exponential backoff with jitter
                    back = (0.5 * (2 ** attempt)) + (0.1 * attempt)
                    time.sleep(back)
                    attempt += 1
                    continue
                _log_error(f"HTTPError {he.code} for {url}")
                raise
            except URLError as ue:
                _log_error(f"URLError {ue.reason} for {url}")
                raise
    except Exception as e:
        _log_error(f"fetch error: {e} url={url}")
        raise


def _parse_atom(data: bytes) -> list[dict]:
    # Parse Atom feed
    ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    root = ET.fromstring(data)
    out: list[dict] = []
    for entry in root.findall("a:entry", ns):
        rid = (entry.findtext("a:id", default="", namespaces=ns) or "").strip()
        title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip().replace("\n", " ")
        summary = (entry.findtext("a:summary", default="", namespaces=ns) or "").strip()
        published = (entry.findtext("a:published", default="", namespaces=ns) or "").strip()
        updated = (entry.findtext("a:updated", default="", namespaces=ns) or "").strip()
        links = [
            (l.get("rel"), l.get("href"))
            for l in entry.findall("a:link", ns)
            if l.get("href")
        ]
        pdf = next((h for r, h in links if (r == "related" or r == "" or r is None) and h.endswith(".pdf")), "")
        alt = next((h for r, h in links if r == "alternate"), "")
        cats = [c.get("term") for c in entry.findall("a:category", ns) if c.get("term")]
        primary_category = cats[0] if cats else ""
        authors = []
        for au in entry.findall('a:author', ns):
            name = (au.findtext('a:name', default='', namespaces=ns) or '').strip()
            aff = (au.findtext('arxiv:affiliation', default='', namespaces=ns) or '').strip()
            authors.append({'name': name, 'affiliation': aff})
        out.append({
            "id": rid,
            "title": title,
            "summary": summary,
            "pdf": pdf,
            "link": alt or rid,
            "categories": cats,
            "primary_category": primary_category,
            "published": published,
            "updated": updated,
            "authors": authors,
        })
    return out


def _extract_arxiv_id(text: str) -> tuple[str | None, str | None]:
    """Return (id_no_version, id_with_version) if `text` contains an arXiv id or URL; else (None, None)."""
    s = (text or "").strip()
    try:
        # URL forms: /abs/<id>[vN], /pdf/<id>[vN].pdf, /html/<id>[vN]
        if s.startswith("http://") or s.startswith("https://"):
            tail = s.rstrip('/').split('/')[-1]
            # strip .pdf if present
            if tail.endswith('.pdf'):
                tail = tail[:-4]
        else:
            tail = s
        import re
        m = re.match(r"^(?P<base>[0-9]{4}\.[0-9]{4,5})(?P<v>v\d+)?$", tail)
        if not m:
            return None, None
        base = m.group('base')
        v = m.group('v')
        return base, (base + v) if v else base
    except Exception as exc:
        logger.error("_extract_arxiv_id caught error: {exc}", exc=exc)
        return None, None


def _fetch_arxiv(q: str, max_results: int = 10) -> list[dict]:
    # Try default all: query
    spans: list[dict] = []
    # Special-case exact id or URL with id: use id_list for precision (strip version for API)
    base_id, full_id = _extract_arxiv_id(q)
    if base_id:
        try:
            data = _query_arxiv(search_query=None, start=0, max_results=max_results, id_list=base_id)
            spans = _parse_atom(data)
            if spans:
                return spans
        except Exception as exc:
            logger.error("_fetch_arxiv caught error: {exc}", exc=exc)
            spans = []
    try:
        data = _query_arxiv(search_query=f"all:{q}", start=0, max_results=max_results)
        spans = _parse_atom(data)
        if spans:
            return spans
    except Exception as exc:
        logger.error("_fetch_arxiv caught error: {exc}", exc=exc)
        spans = []
    # Fallback 1: fielded title OR abstract
    try:
        sq = f"ti:{q} OR abs:{q}"
        data = _query_arxiv(search_query=sq, start=0, max_results=max_results, sortBy="submittedDate", sortOrder="descending")
        spans = _parse_atom(data)
        if spans:
            return spans
    except Exception as exc:
        logger.error("_fetch_arxiv caught error: {exc}", exc=exc)
        spans = []
    # Fallback 2: restrict to common CS categories and AND terms in abstract
    try:
        toks = [t for t in (q or "").replace('"', '').split() if t]
        if toks:
            abs_and = "+AND+".join([f"abs:{urllib.parse.quote_plus(t)}" for t in toks])
            cats = "+OR+".join(["cat:cs.CL","cat:cs.AI","cat:cs.IR","cat:cs.LG"])
            sq = f"({abs_and})+AND+({cats})"
            data = _query_arxiv(search_query=sq, start=0, max_results=max_results, sortBy="submittedDate", sortOrder="descending")
            spans = _parse_atom(data)
            if spans:
                return spans
    except Exception as exc:
        logger.error("_fetch_arxiv caught error: {exc}", exc=exc)
        spans = []
    # Fallback 3: broaden to title keywords without category filter
    try:
        toks = [t for t in (q or "").replace('"', '').split() if t]
        if toks:
            ti_or = "+OR+".join([f"ti:{urllib.parse.quote_plus(t)}" for t in toks])
            sq = f"({ti_or})"
            data = _query_arxiv(search_query=sq, start=0, max_results=max_results, sortBy="relevance", sortOrder="descending")
            spans = _parse_atom(data)
            if spans:
                return spans
    except Exception as exc:
        logger.error("_fetch_arxiv caught error: {exc}", exc=exc)
        spans = []
    return spans


@app.command()
def ingest(
    q: str = typer.Option("", help="arXiv search query or bare id (e.g., 2506.23286v1)"),
    url: str = typer.Option("", help="arXiv URL (abs/pdf/html) or bare id; takes precedence over q if provided"),
    scope: str = typer.Option("research", help="Scope to write lessons to"),
    max_results: int = typer.Option(10, help="Max results to ingest"),
    min_year: int = typer.Option(2024, "--min-year", "--from-year", help="Minimum year to include (set 0 to disable)"),
    allow_old: bool = typer.Option(False, help="Include older works regardless of min_year"),
    strict: bool = typer.Option(False, help="Exit non-zero when all queries fail due to errors"),
    field: str = typer.Option("", help="Optional fielded query prefix: ti|au|abs|cat (used only when id/url not detected)"),
    log_episode: bool = typer.Option(False, help="Record a memory_events 'papers' episode with fetched ids (traceability)"),
    append_papers_log: bool = typer.Option(False, help="Append bullets to docs/ideas/papers.md when present"),
    json_out: bool = typer.Option(True, "--json", help="Output JSON envelope"),
):
    db = None
    if not os.environ.get('ARXIV_DRY_RUN') and True:
        # we will use a proper flag below; this guard is for structure matching
        pass
    t0 = time.time()
    items = []
    errors = []
    query = url or q
    if not query:
        errors.append("missing q or url")
        papers = []
    else:
        try:
            papers = _fetch_arxiv(q=query, max_results=max_results, field=(field or None))
        except Exception as e:
            papers = []
            errors.append(f"fetch failed: {e}")
    ts = int(time.time())
    wrote = []
    # Filter by min_year if requested
    def _year(s: str) -> int:
        try:
            return int((s or "")[:4])
        except Exception as exc:
            logger.error("_year int parse failed: {exc}", exc=exc)
            return 0

    for p in papers:
        title = p["title"]
        if not title:
            continue
        # Prefix ID to ensure uniqueness across scopes and reruns
        lid = p.get("id") or ""
        # Normalize id/version
        base_id, full_id = _extract_arxiv_id(lid or p.get("link") or "")
        version = None
        if full_id and full_id.startswith(base_id or "") and len(full_id) > len(base_id or ""):
            version = full_id[len(base_id):]
        # Year policy
        yr = _year(p.get("updated") or p.get("published") or "")
        if not allow_old and min_year > 0 and yr and yr < min_year:
            continue
        atitle = (f"ARXIV[{(full_id or lid).split('/')[-1]}] " if (full_id or lid) else "ARXIV ") + title
        problem = p.get("summary") or ""
        link = p.get("pdf") or p.get("link") or ""
        cats = p.get("categories") or []
        playbook = f"- Read: {link}\n- Skim abstract; extract key claims and methods"
        doc = {
            "title": atitle,
            "problem": problem,
            "playbook": playbook,
            "tags": ["arxiv"] + cats,
            "keywords": " ".join(cats),
            "scope": scope,
            "status": "active",
            "added_by": "agent",
            "updated_at": ts,
            "demo": False,
            "arxiv_id": base_id or "",
            "arxiv_version": version or "",
            "primary_category": p.get("primary_category") or (cats[0] if cats else ""),
            "year": yr,
        }
        res = list(db.aql.execute(
            "UPSERT { title:@t, scope:@s } INSERT @d UPDATE @d IN lessons_v2 RETURN NEW",
            bind_vars={"t": atitle, "s": scope, "d": doc},
        ))
        if res:
            wrote.append({"arxiv_id": base_id or lid, "lesson": res[0].get("_id"), "link": link})
    took = int((time.time() - t0) * 1000)
    # Optional memory episode for traceability
    if (log_episode or bool(os.environ.get("ARXIV_LOG_EPISODE", "0") == "1")) and log_event and wrote:
        try:
            log_event(db, kind="papers", message=f"arxiv ingest {len(wrote)}", payload={
                "query": query, "scope": scope, "min_year": min_year, "allow_old": allow_old,
                "ids": [w.get("arxiv_id") for w in wrote], "links": [w.get("link") for w in wrote]
            })
        except Exception as e:
            _log_error(f"episode write failed: {e}")
            errors.append(f"episode: {e}")
    if append_papers_log and wrote:
        try:
            pfile = Path("docs/ideas/papers.md")
            pfile.parent.mkdir(parents=True, exist_ok=True)
            with pfile.open("a", encoding="utf-8") as f:
                for w in wrote:
                    link = w.get("link") or ""
                    aid = w.get("arxiv_id") or ""
                    f.write(f"- [{aid}]({link}) in {scope}\n")
        except Exception as e:
            _log_error(f"append papers.md failed: {e}")
            errors.append(f"papers_log: {e}")
    out = {"meta": {"q": query, "scope": scope, "count": len(wrote), "took_ms": took, "min_year": min_year, "allow_old": allow_old}, "items": wrote, "errors": errors}
    if json_out:
        print(json.dumps(out, ensure_ascii=False))
    else:
        typer.echo(f"Ingested {len(wrote)} papers in {took} ms")
    if strict and errors and not wrote:
        raise typer.Exit(code=1)
